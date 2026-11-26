//! Access to the user's mailboxes.
//!
//! This is the only place that speaks IMAP, and it only knows how to perform the operations that
//! the email sandbox is allowed to request. There is deliberately no code here that can delete a
//! message permanently, delete or rename a folder, change access control lists, or store a
//! message that the gateway did not compose itself.
//!
//! Message contents are treated as opaque bytes. The two exceptions, both needed for drafts that
//! are replies, are the envelope as parsed by the IMAP server and the `References` header.

use std::collections::{HashMap, HashSet};
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use async_imap::error::Error as ImapError;
use async_imap::imap_proto::rfc4315::UidSetMember;
use async_imap::imap_proto::{Response, ResponseCode, Status};
use async_imap::types::{Fetch, Flag, Mailbox, NameAttribute};
use bytes::Bytes;
use futures_util::TryStreamExt;
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::TcpStream;
use tokio::sync::{Mutex, mpsc};
use tokio::time::timeout;
use tokio_rustls::TlsConnector;
use tokio_rustls::client::TlsStream;

use crate::config::{AccountConfig, Auth, FolderOverrides, Secret, Security};
use crate::draft::{self, Draft, Recipient};
use crate::error::{ApiError, ApiResult};
use crate::oauth::{self, AccessToken, XOAuth2};
use crate::raw::{RawError, RawImap};
use crate::rules::{self, Query, Role};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
const OP_TIMEOUT: Duration = Duration::from_secs(120);
const FETCH_TIMEOUT: Duration = Duration::from_secs(1800);
const FETCH_ITEM_TIMEOUT: Duration = Duration::from_secs(120);
const NOOP_AFTER: Duration = Duration::from_secs(20);
const FOLDER_CACHE_TTL: Duration = Duration::from_secs(60);
/// Upper bound on the UIDs taken from a single COPYUID response.
const MAX_COPYUID: usize = 10_000;

// ---------------------------------------------------------------------------------------------
// Transport

/// A connection to an IMAP server, with or without TLS.
#[derive(Debug)]
pub enum Stream {
    Plain(TcpStream),
    Tls(Box<TlsStream<TcpStream>>),
}

impl AsyncRead for Stream {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        match self.get_mut() {
            Stream::Plain(s) => Pin::new(s).poll_read(cx, buf),
            Stream::Tls(s) => Pin::new(s.as_mut()).poll_read(cx, buf),
        }
    }
}

impl AsyncWrite for Stream {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        match self.get_mut() {
            Stream::Plain(s) => Pin::new(s).poll_write(cx, buf),
            Stream::Tls(s) => Pin::new(s.as_mut()).poll_write(cx, buf),
        }
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        match self.get_mut() {
            Stream::Plain(s) => Pin::new(s).poll_flush(cx),
            Stream::Tls(s) => Pin::new(s.as_mut()).poll_flush(cx),
        }
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        match self.get_mut() {
            Stream::Plain(s) => Pin::new(s).poll_shutdown(cx),
            Stream::Tls(s) => Pin::new(s.as_mut()).poll_shutdown(cx),
        }
    }
}

type Session = async_imap::Session<Stream>;

/// TLS configuration that trusts the Mozilla root certificates bundled with the gateway.
pub fn tls_config() -> Arc<rustls::ClientConfig> {
    let mut roots = rustls::RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    let config = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .expect("the default protocol versions are supported by ring")
    .with_root_certificates(roots)
    .with_no_client_auth();
    Arc::new(config)
}

/// An HTTP client for the gateway's own requests (OAuth token endpoints and the relays). It
/// uses the same trust anchors as the IMAP connections, ignores proxy settings from the
/// environment, and never follows redirects.
pub fn http_client(tls: &Arc<rustls::ClientConfig>) -> reqwest::Client {
    reqwest::Client::builder()
        .tls_backend_preconfigured(tls.as_ref().clone())
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(20))
        .build()
        .expect("the HTTP client configuration is valid")
}

// ---------------------------------------------------------------------------------------------
// Errors inside an operation

/// An error together with whether the connection can still be used afterwards.
struct Failure {
    error: ApiError,
    reset: bool,
}

type OpResult<T> = Result<T, Failure>;

impl Failure {
    fn reset(error: ApiError) -> Self {
        Failure { error, reset: true }
    }
}

impl From<ApiError> for Failure {
    fn from(error: ApiError) -> Self {
        Failure {
            error,
            reset: false,
        }
    }
}

impl From<ImapError> for Failure {
    fn from(error: ImapError) -> Self {
        match error {
            ImapError::No(text) | ImapError::Bad(text) => Failure {
                error: ApiError::imap(format!(
                    "the IMAP server refused the command: {}",
                    truncate(&text, 300)
                )),
                reset: false,
            },
            ImapError::Io(e) => Failure::reset(ApiError::unavailable(format!(
                "the connection to the IMAP server failed: {}",
                e.kind()
            ))),
            ImapError::ConnectionLost => Failure::reset(ApiError::unavailable(
                "the connection to the IMAP server was lost",
            )),
            // No details: a response that cannot be parsed may contain message content, and
            // this text ends up in the gateway's log.
            _ => Failure::reset(ApiError::imap(
                "the IMAP server sent a response that could not be understood",
            )),
        }
    }
}

impl From<std::io::Error> for Failure {
    fn from(error: std::io::Error) -> Self {
        Failure::reset(ApiError::unavailable(format!(
            "the connection to the IMAP server failed: {}",
            error.kind()
        )))
    }
}

fn raw_failure(error: RawError) -> Failure {
    Failure::reset(match error {
        RawError::Io(kind) => {
            ApiError::unavailable(format!("the connection to the IMAP server failed: {kind}"))
        }
        RawError::Protocol(what) => ApiError::imap(format!("the IMAP server misbehaved: {what}")),
        RawError::Refused(text) => {
            ApiError::unavailable(format!("the IMAP server refused: {}", truncate(&text, 200)))
        }
    })
}

/// Shortens a text from the server for use in an error message. The IMAP library reports the
/// server's words wrapped in debug formatting, which is removed here.
fn truncate(text: &str, max: usize) -> String {
    let quoted = text
        .split_once("information: Some(\"")
        .or_else(|| text.split_once("info: Some(\""));
    let text = match quoted {
        Some((_, rest)) => rest
            .rsplit_once("\")")
            .map_or(rest, |(inner, _)| inner)
            .replace("\\\"", "\""),
        None if text.contains("information: None") || text.contains("info: None") => String::new(),
        None => text.to_string(),
    };
    text.chars().filter(|c| !c.is_control()).take(max).collect()
}

// ---------------------------------------------------------------------------------------------
// Folders

/// A folder as the server lists it, before the gateway's rules are applied.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RawFolder {
    /// The name exactly as the server sent it (modified UTF-7).
    pub raw_name: String,
    pub delimiter: Option<String>,
    pub selectable: bool,
    /// Role announced by the server through a special-use attribute.
    pub special_use: Option<Role>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct Folder {
    pub name: String,
    #[serde(skip)]
    pub raw_name: String,
    pub delimiter: Option<String>,
    pub role: Option<Role>,
    pub selectable: bool,
    /// Messages cannot be moved into a protected folder.
    pub protected: bool,
}

impl Folder {
    fn leaf(&self) -> &str {
        leaf_name(&self.name, self.delimiter.as_deref())
    }
}

fn leaf_name<'a>(name: &'a str, delimiter: Option<&str>) -> &'a str {
    match delimiter {
        Some(d) if !d.is_empty() => name.rsplit(d).next().unwrap_or(name),
        _ => name,
    }
}

/// Applies the gateway's rules to the server's folder list: drops folders outside the user's
/// personal namespace, decodes names, assigns roles, and marks protected folders.
pub fn build_folders(
    raw: Vec<RawFolder>,
    foreign_prefixes: &[String],
    overrides: &FolderOverrides,
) -> Vec<Folder> {
    let mut folders: Vec<Folder> = Vec::new();
    let mut seen = HashSet::new();
    for entry in raw {
        let raw_name = entry.raw_name;
        let printable =
            !raw_name.is_empty() && raw_name.bytes().all(|b| (0x20..=0x7e).contains(&b));
        if !printable || is_foreign(&raw_name, entry.delimiter.as_deref(), foreign_prefixes) {
            continue;
        }
        let is_inbox = raw_name.eq_ignore_ascii_case("INBOX");
        let name = if is_inbox {
            "INBOX".to_string()
        } else {
            utf7_imap::decode_utf7_imap(raw_name.clone())
        };
        if name.chars().any(|c| c.is_control()) || !seen.insert(name.clone()) {
            continue;
        }
        folders.push(Folder {
            name,
            raw_name,
            delimiter: entry.delimiter,
            role: if is_inbox {
                Some(Role::Inbox)
            } else {
                entry.special_use
            },
            selectable: entry.selectable,
            protected: false,
        });
    }

    // The user's choices override what the server announces.
    let chosen = [
        (Role::Drafts, &overrides.drafts),
        (Role::Sent, &overrides.sent),
        (Role::Trash, &overrides.trash),
        (Role::Junk, &overrides.junk),
        (Role::Archive, &overrides.archive),
    ];
    for (role, name) in chosen {
        let Some(name) = name else { continue };
        if folders.iter().any(|f| &f.name == name) {
            for folder in folders.iter_mut() {
                if folder.role == Some(role) {
                    folder.role = None;
                }
                if &folder.name == name {
                    folder.role = Some(role);
                }
            }
        }
    }

    // For roles that are still unassigned, guess from the folder names.
    for role in [
        Role::Drafts,
        Role::Sent,
        Role::Trash,
        Role::Junk,
        Role::Archive,
    ] {
        if folders.iter().any(|f| f.role == Some(role)) {
            continue;
        }
        let guess = folders
            .iter_mut()
            .filter(|f| f.role.is_none() && rules::role_from_name(f.leaf()) == Some(role))
            .min_by_key(|f| f.name.len());
        if let Some(folder) = guess {
            folder.role = Some(role);
        }
    }

    for folder in folders.iter_mut() {
        folder.protected = matches!(folder.role, Some(Role::Drafts) | Some(Role::Sent))
            || rules::is_protected_name(folder.leaf())
            || overrides.protected.contains(&folder.name);
    }
    folders
}

/// Whether a folder lies in a namespace that belongs to other users or is shared.
fn is_foreign(raw_name: &str, delimiter: Option<&str>, foreign_prefixes: &[String]) -> bool {
    foreign_prefixes.iter().any(|prefix| {
        if prefix.is_empty() {
            return false;
        }
        let root = delimiter
            .and_then(|d| prefix.strip_suffix(d))
            .unwrap_or(prefix);
        raw_name.starts_with(prefix.as_str()) || raw_name == root
    })
}

/// Parses the prefixes of the "other users" and "shared" namespaces out of a NAMESPACE response
/// line such as `* NAMESPACE (("" "/")) (("Other/" "/")) (("Public/" "/")("#news." "."))`.
///
/// Returns `None` if the line cannot be understood, in which case the caller must fail closed.
pub fn parse_foreign_namespaces(line: &str) -> Option<Vec<String>> {
    let rest = line.trim();
    let rest = rest.strip_prefix('*')?.trim_start();
    if !rest.get(..9)?.eq_ignore_ascii_case("NAMESPACE") {
        return None;
    }
    let mut chars = rest[9..].chars().peekable();
    let mut groups: Vec<Vec<String>> = Vec::new();
    while groups.len() < 3 {
        while chars.peek() == Some(&' ') {
            chars.next();
        }
        match chars.peek()? {
            'N' | 'n' => {
                for expected in ['N', 'I', 'L'] {
                    if !chars.next()?.eq_ignore_ascii_case(&expected) {
                        return None;
                    }
                }
                groups.push(Vec::new());
            }
            '(' => {
                chars.next();
                let mut prefixes = Vec::new();
                loop {
                    match chars.next()? {
                        ')' => break,
                        '(' => {
                            // ("prefix" "delimiter" extensions...)
                            if chars.next()? != '"' {
                                return None;
                            }
                            prefixes.push(read_quoted(&mut chars)?);
                            let mut depth = 1;
                            while depth > 0 {
                                match chars.next()? {
                                    '"' => {
                                        read_quoted(&mut chars)?;
                                    }
                                    '(' => depth += 1,
                                    ')' => depth -= 1,
                                    _ => {}
                                }
                            }
                        }
                        ' ' => {}
                        _ => return None,
                    }
                }
                groups.push(prefixes);
            }
            _ => return None,
        }
    }
    let mut foreign = groups.swap_remove(1);
    foreign.extend(groups.pop()?);
    Some(foreign)
}

/// Reads the rest of a quoted string whose opening quote has been consumed.
fn read_quoted(chars: &mut std::iter::Peekable<std::str::Chars<'_>>) -> Option<String> {
    let mut out = String::new();
    loop {
        match chars.next()? {
            '"' => return Some(out),
            '\\' => out.push(chars.next()?),
            c => out.push(c),
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Results

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct FolderStatus {
    pub uid_validity: u32,
    pub uid_next: Option<u32>,
    pub messages: u32,
    pub unseen: Option<u32>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct MessageMeta {
    pub uid: u32,
    pub flags: Vec<String>,
    pub size: u32,
    pub internal_date: Option<String>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct MessageList {
    pub uid_validity: u32,
    pub messages: Vec<MessageMeta>,
    pub next_uid: Option<u32>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct SearchResult {
    pub uid_validity: u32,
    pub uids: Vec<u32>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Moved {
    pub uid: u32,
    pub new_uid: Option<u32>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct MoveResult {
    pub destination_uid_validity: Option<u32>,
    pub moved: Vec<Moved>,
    /// Role of the destination, so that the caller can count deletions.
    #[serde(skip)]
    pub destination_role: Option<Role>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MessageRef {
    pub folder: String,
    pub uid_validity: u32,
    pub uid: u32,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftRequest {
    pub to: Vec<Recipient>,
    #[serde(default)]
    pub cc: Vec<Recipient>,
    pub subject: String,
    pub body: String,
    #[serde(default)]
    pub in_reply_to: Option<MessageRef>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct DraftResult {
    pub folder: String,
    pub message_id: String,
}

/// One chunk of a fetch response stream, or the error that ended it.
pub type FetchItem = Result<Bytes, ApiError>;

// ---------------------------------------------------------------------------------------------
// Accounts and connections

struct Capabilities {
    r#move: bool,
    uidplus: bool,
    namespace: bool,
}

struct Conn {
    session: Session,
    caps: Capabilities,
    foreign_prefixes: Vec<String>,
    folders: Option<(Instant, Arc<Vec<Folder>>)>,
    last_used: Instant,
}

#[derive(Default)]
struct State {
    conn: Option<Conn>,
    access_token: Option<AccessToken>,
    /// A refresh token that replaced the configured one during this run.
    rotated_refresh_token: Option<Secret>,
    /// Prefixes of the namespaces that are not the user's own; asked for once per run.
    foreign_prefixes: Option<Vec<String>>,
    last_error: Option<String>,
}

/// One of the user's IMAP accounts. All operations on an account are serialised over a single
/// connection, which is opened on demand and reopened when it breaks.
pub struct Account {
    pub config: AccountConfig,
    tls: Arc<rustls::ClientConfig>,
    http: reqwest::Client,
    state: Mutex<State>,
}

impl Account {
    pub fn new(
        config: AccountConfig,
        tls: Arc<rustls::ClientConfig>,
        http: reqwest::Client,
    ) -> Self {
        Account {
            config,
            tls,
            http,
            state: Mutex::new(State::default()),
        }
    }

    /// The error of the most recent operation, if it failed.
    pub async fn last_error(&self) -> Option<String> {
        self.state.lock().await.last_error.clone()
    }

    async fn run<T>(
        &self,
        limit: Duration,
        op: impl AsyncFnOnce(&Account, &mut Conn) -> OpResult<T>,
    ) -> ApiResult<T> {
        let mut state = self.state.lock().await;
        let outcome = timeout(limit, async {
            self.ensure_connected(&mut state).await?;
            let conn = state.conn.as_mut().expect("ensure_connected succeeded");
            let result = op(self, conn).await;
            conn.last_used = Instant::now();
            result
        })
        .await;
        match outcome {
            Ok(Ok(value)) => {
                state.last_error = None;
                Ok(value)
            }
            Ok(Err(failure)) => {
                if failure.reset {
                    state.conn = None;
                }
                if failure.error.code == "unavailable" || failure.error.code == "imap_error" {
                    state.last_error = Some(failure.error.message.clone());
                }
                Err(failure.error)
            }
            Err(_) => {
                state.conn = None;
                let error = ApiError::unavailable("the IMAP server did not answer in time");
                state.last_error = Some(error.message.clone());
                Err(error)
            }
        }
    }

    async fn ensure_connected(&self, state: &mut State) -> OpResult<()> {
        if let Some(conn) = state.conn.as_mut() {
            if conn.last_used.elapsed() < NOOP_AFTER {
                return Ok(());
            }
            if conn.session.noop().await.is_ok() {
                conn.last_used = Instant::now();
                return Ok(());
            }
            state.conn = None;
        }
        let conn = timeout(CONNECT_TIMEOUT, self.connect(state))
            .await
            .map_err(|_| {
                Failure::reset(ApiError::unavailable(
                    "timed out connecting to the IMAP server",
                ))
            })??;
        state.conn = Some(conn);
        Ok(())
    }

    /// Opens a connection to the server, with TLS unless the account is configured otherwise.
    /// The second value says whether the server's greeting is still to be read.
    async fn open_stream(&self) -> OpResult<(Stream, bool)> {
        let config = &self.config;
        let tcp = TcpStream::connect((config.host.as_str(), config.port))
            .await
            .map_err(|e| {
                Failure::reset(ApiError::unavailable(format!(
                    "could not connect to {}:{}: {}",
                    config.host,
                    config.port,
                    e.kind()
                )))
            })?;
        tcp.set_nodelay(true).ok();
        match config.security {
            Security::Tls => Ok((self.start_tls(tcp).await?, true)),
            Security::Starttls => {
                let mut raw = RawImap::new(tcp);
                raw.greeting().await.map_err(raw_failure)?;
                raw.command("STARTTLS").await.map_err(raw_failure)?;
                // Fails if the server sent anything between its reply and the handshake.
                let tcp = raw.into_inner().map_err(raw_failure)?;
                Ok((self.start_tls(tcp).await?, false))
            }
            Security::Insecure => Ok((Stream::Plain(tcp), true)),
        }
    }

    async fn connect(&self, state: &mut State) -> OpResult<Conn> {
        let config = &self.config;
        let (stream, greeting_pending) = self.open_stream().await?;
        let mut client = async_imap::Client::new(stream);
        if greeting_pending {
            read_greeting(&mut client).await?;
        }

        let login_failed = |error: ImapError| match error {
            ImapError::No(text) | ImapError::Bad(text) => Failure::reset(ApiError::unavailable(
                format!("login failed: {}", truncate(&text, 200)),
            )),
            other => Failure::from(other),
        };
        let mut session = match &config.auth {
            Auth::Password { password } => client
                .login(&config.username, password.expose())
                .await
                .map_err(|(e, _)| login_failed(e))?,
            Auth::Oauth2 { .. } => {
                let token = self.access_token(state).await?;
                client
                    .authenticate("XOAUTH2", XOAuth2::new(&config.username, token))
                    .await
                    .map_err(|(e, _)| login_failed(e))?
            }
        };

        let capabilities = session.capabilities().await?;
        let caps = Capabilities {
            r#move: capabilities.has_str("MOVE"),
            uidplus: capabilities.has_str("UIDPLUS"),
            namespace: capabilities.has_str("NAMESPACE"),
        };
        if caps.namespace && state.foreign_prefixes.is_none() {
            state.foreign_prefixes = Some(self.foreign_namespaces(state).await?);
        }
        let foreign_prefixes = state.foreign_prefixes.clone().unwrap_or_default();
        Ok(Conn {
            session,
            caps,
            foreign_prefixes,
            folders: None,
            last_used: Instant::now(),
        })
    }

    /// Asks the server for its namespaces and returns the prefixes of those that do not belong
    /// to the user. This needs a short-lived second connection, because the IMAP library cannot
    /// parse the response. If the answer cannot be understood, connecting fails, rather than
    /// risking that a shared folder is treated as one of the user's own.
    async fn foreign_namespaces(&self, state: &mut State) -> OpResult<Vec<String>> {
        let (stream, greeting_pending) = self.open_stream().await?;
        let mut raw = RawImap::new(stream);
        if greeting_pending {
            raw.greeting().await.map_err(raw_failure)?;
        }
        match &self.config.auth {
            Auth::Password { password } => raw.login(&self.config.username, password).await,
            Auth::Oauth2 { .. } => {
                let token = self.access_token(state).await?;
                raw.authenticate_xoauth2(&self.config.username, &token)
                    .await
            }
        }
        .map_err(raw_failure)?;
        let lines = raw.command("NAMESPACE").await.map_err(raw_failure)?;
        raw.command("LOGOUT").await.ok();
        lines
            .iter()
            .find_map(|line| parse_foreign_namespaces(line))
            .ok_or_else(|| {
                Failure::reset(ApiError::imap(
                    "the IMAP server's description of its namespaces could not be understood",
                ))
            })
    }

    async fn start_tls(&self, tcp: TcpStream) -> OpResult<Stream> {
        let name = rustls::pki_types::ServerName::try_from(self.config.host.clone())
            .map_err(|_| Failure::reset(ApiError::unavailable("invalid server name")))?;
        let stream = TlsConnector::from(self.tls.clone())
            .connect(name, tcp)
            .await
            .map_err(|e| {
                Failure::reset(ApiError::unavailable(format!(
                    "TLS handshake with {} failed: {e}",
                    self.config.host
                )))
            })?;
        Ok(Stream::Tls(Box::new(stream)))
    }

    async fn access_token(&self, state: &mut State) -> OpResult<Secret> {
        let Auth::Oauth2 {
            token_url,
            client_id,
            client_secret,
            refresh_token,
        } = &self.config.auth
        else {
            unreachable!("only called for OAuth accounts");
        };
        if let Some(access) = state.access_token.as_ref().filter(|a| a.is_fresh()) {
            return Ok(access.token.clone());
        }
        let current = state
            .rotated_refresh_token
            .as_ref()
            .unwrap_or(refresh_token);
        let refreshed = oauth::refresh(
            &self.http,
            token_url,
            client_id,
            client_secret.as_ref(),
            current,
        )
        .await
        .map_err(Failure::reset)?;
        if let Some(rotated) = refreshed.refresh_token {
            state.rotated_refresh_token = Some(rotated);
        }
        let token = refreshed.access.token.clone();
        state.access_token = Some(refreshed.access);
        Ok(token)
    }

    // -----------------------------------------------------------------------------------------
    // Operations

    pub async fn list_folders(&self) -> ApiResult<Vec<Folder>> {
        self.run(OP_TIMEOUT, async |account, conn| {
            let folders = refresh_folders(account, conn).await?;
            Ok(folders.as_ref().clone())
        })
        .await
    }

    pub async fn create_folder(&self, name: &str) -> ApiResult<()> {
        self.run(OP_TIMEOUT, async |account, conn| {
            let folders = refresh_folders(account, conn).await?;
            if folders.iter().any(|f| f.name == name) {
                return Err(ApiError::bad_request("the folder exists already").into());
            }
            // Use the hierarchy delimiter of the personal namespace.
            let delimiter = folders
                .iter()
                .find(|f| f.role == Some(Role::Inbox))
                .or(folders.first())
                .and_then(|f| f.delimiter.clone());
            rules::validate_new_folder_name(name, delimiter.as_deref())?;
            let raw_name = utf7_imap::encode_utf7_imap(name.to_string());
            if is_foreign(&raw_name, delimiter.as_deref(), &conn.foreign_prefixes) {
                return Err(ApiError::forbidden(
                    "folders can only be created among the user's own folders",
                )
                .into());
            }
            conn.session.create(&raw_name).await?;
            conn.folders = None;
            Ok(())
        })
        .await
    }

    pub async fn status(&self, folder: &str) -> ApiResult<FolderStatus> {
        self.run(OP_TIMEOUT, async |account, conn| {
            let folder = resolve(account, conn, folder).await?;
            require_selectable(&folder)?;
            let mailbox = conn
                .session
                .status(&folder.raw_name, "(MESSAGES UNSEEN UIDNEXT UIDVALIDITY)")
                .await?;
            Ok(FolderStatus {
                uid_validity: uid_validity(&mailbox)?,
                uid_next: mailbox.uid_next,
                messages: mailbox.exists,
                unseen: mailbox.unseen,
            })
        })
        .await
    }

    pub async fn list_messages(
        &self,
        folder: &str,
        uid_min: u32,
        uid_max: Option<u32>,
        limit: usize,
    ) -> ApiResult<MessageList> {
        if uid_min == 0 || uid_max.is_some_and(|max| max < uid_min) {
            return Err(ApiError::bad_request("invalid UID range"));
        }
        if limit == 0 || limit > rules::MAX_LIST_LIMIT {
            return Err(ApiError::bad_request(format!(
                "limit must be between 1 and {}",
                rules::MAX_LIST_LIMIT
            )));
        }
        self.run(OP_TIMEOUT, async |account, conn| {
            let folder = resolve(account, conn, folder).await?;
            let mailbox = examine(conn, &folder).await?;
            let uid_validity = uid_validity(&mailbox)?;
            let mut uids: Vec<u32> = if mailbox.exists == 0 {
                Vec::new()
            } else {
                let range = match uid_max {
                    Some(max) => format!("UID {uid_min}:{max}"),
                    None => format!("UID {uid_min}:*"),
                };
                // `n:*` always matches the last message, even if its UID is below n.
                conn.session
                    .uid_search(&range)
                    .await?
                    .into_iter()
                    .filter(|uid| *uid >= uid_min && uid_max.is_none_or(|max| *uid <= max))
                    .collect()
            };
            uids.sort_unstable();
            let next_uid = (uids.len() > limit).then(|| uids[limit]);
            uids.truncate(limit);

            let wanted: HashSet<u32> = uids.iter().copied().collect();
            let mut found: HashMap<u32, MessageMeta> = HashMap::new();
            for set in rules::chunk_uid_sets(&uids) {
                let mut fetches = conn
                    .session
                    .uid_fetch(&set, "(UID FLAGS RFC822.SIZE INTERNALDATE)")
                    .await?;
                while let Some(fetch) = fetches.try_next().await? {
                    // Skip unsolicited flag updates for other messages.
                    if let (Some(uid), Some(size)) = (fetch.uid, fetch.size)
                        && wanted.contains(&uid)
                    {
                        found.insert(uid, meta(&fetch, uid, size));
                    }
                }
            }
            let mut messages: Vec<MessageMeta> = found.into_values().collect();
            messages.sort_unstable_by_key(|m| m.uid);
            Ok(MessageList {
                uid_validity,
                messages,
                next_uid,
            })
        })
        .await
    }

    /// Fetches raw messages and sends them to `tx` as frames (see `docs/gateway-api.md`). If
    /// anything fails, the error is sent as the last item.
    pub async fn fetch_messages(&self, folder: &str, uids: &[u32], tx: mpsc::Sender<FetchItem>) {
        let result = self.fetch_frames(folder, uids, &tx).await;
        let last = match result {
            Ok(uid_validity) => Ok(frame_line(&serde_json::json!({
                "end": true,
                "uid_validity": uid_validity,
            }))),
            Err(error) => Err(error),
        };
        tx.send(last).await.ok();
    }

    async fn fetch_frames(
        &self,
        folder: &str,
        uids: &[u32],
        tx: &mpsc::Sender<FetchItem>,
    ) -> ApiResult<u32> {
        if uids.len() > rules::MAX_UIDS_PER_FETCH {
            return Err(ApiError::bad_request(format!(
                "at most {} messages can be fetched at once",
                rules::MAX_UIDS_PER_FETCH
            )));
        }
        let sets = if uids.is_empty() {
            Vec::new()
        } else {
            rules::format_uid_set(uids)?; // validates the UIDs
            rules::chunk_uid_sets(uids)
        };
        let wanted: HashSet<u32> = uids.iter().copied().collect();
        self.run(FETCH_TIMEOUT, async |account, conn| {
            let folder = resolve(account, conn, folder).await?;
            let mailbox = examine(conn, &folder).await?;
            let uid_validity = uid_validity(&mailbox)?;
            let gone = || {
                // The stream of responses has not been read to its end, so the connection
                // cannot be reused.
                Failure::reset(ApiError::unavailable("the caller went away"))
            };
            for set in &sets {
                let mut fetches = conn
                    .session
                    .uid_fetch(set, "(UID FLAGS INTERNALDATE BODY.PEEK[])")
                    .await?;
                loop {
                    let next = timeout(FETCH_ITEM_TIMEOUT, fetches.try_next())
                        .await
                        .map_err(|_| {
                            Failure::reset(ApiError::unavailable(
                                "the IMAP server did not answer in time",
                            ))
                        })??;
                    let Some(fetch) = next else { break };
                    let (Some(uid), Some(body)) = (fetch.uid, fetch.body()) else {
                        continue;
                    };
                    if !wanted.contains(&uid) {
                        continue;
                    }
                    let header = meta(&fetch, uid, body.len() as u32);
                    let header = serde_json::to_value(&header).expect("serializable");
                    let mut payload = Vec::with_capacity(body.len() + 1);
                    payload.extend_from_slice(body);
                    payload.push(b'\n');
                    tx.send(Ok(frame_line(&header))).await.map_err(|_| gone())?;
                    tx.send(Ok(Bytes::from(payload)))
                        .await
                        .map_err(|_| gone())?;
                }
            }
            Ok(uid_validity)
        })
        .await
    }

    pub async fn search(&self, folder: &str, query: &Query) -> ApiResult<SearchResult> {
        let query = query.to_imap()?;
        self.run(OP_TIMEOUT, async |account, conn| {
            let folder = resolve(account, conn, folder).await?;
            let mailbox = examine(conn, &folder).await?;
            let mut uids: Vec<u32> = conn.session.uid_search(&query).await?.into_iter().collect();
            uids.sort_unstable();
            Ok(SearchResult {
                uid_validity: uid_validity(&mailbox)?,
                uids,
            })
        })
        .await
    }

    /// Adds and removes flags. Returns the flags in canonical spelling.
    pub async fn store_flags(
        &self,
        folder: &str,
        expected_uid_validity: u32,
        uids: &[u32],
        add: &[String],
        remove: &[String],
    ) -> ApiResult<(Vec<String>, Vec<String>)> {
        let canonical = |flags: &[String]| -> ApiResult<Vec<String>> {
            flags
                .iter()
                .map(|flag| rules::validate_flag(flag))
                .collect()
        };
        let add = canonical(add)?;
        let remove = canonical(remove)?;
        if add.is_empty() && remove.is_empty() {
            return Err(ApiError::bad_request("no flags given"));
        }
        if add.len() + remove.len() > 20 {
            return Err(ApiError::bad_request("too many flags"));
        }
        let sets = change_sets(uids)?;
        self.run(OP_TIMEOUT, async |account, conn| {
            let folder = resolve(account, conn, folder).await?;
            select(conn, &folder, expected_uid_validity).await?;
            for set in &sets {
                for (sign, flags) in [("+", &add), ("-", &remove)] {
                    if flags.is_empty() {
                        continue;
                    }
                    let item = format!("{sign}FLAGS.SILENT ({})", flags.join(" "));
                    let mut updates = conn.session.uid_store(set, &item).await?;
                    while updates.try_next().await?.is_some() {}
                }
            }
            Ok(())
        })
        .await?;
        Ok((add, remove))
    }

    pub async fn move_messages(
        &self,
        folder: &str,
        expected_uid_validity: u32,
        uids: &[u32],
        destination: &str,
    ) -> ApiResult<MoveResult> {
        let sets = change_sets(uids)?;
        let mut requested = uids.to_vec();
        requested.sort_unstable();
        requested.dedup();
        self.run(OP_TIMEOUT, async |account, conn| {
            let source = resolve(account, conn, folder).await?;
            let target = resolve(account, conn, destination).await?;
            require_selectable(&target)?;
            if target.protected {
                return Err(ApiError::forbidden(format!(
                    "messages cannot be moved into {:?}",
                    target.name
                ))
                .into());
            }
            if target.raw_name == source.raw_name {
                return Err(ApiError::bad_request("source and destination are the same").into());
            }
            if !conn.caps.r#move && !conn.caps.uidplus {
                return Err(ApiError::unsupported(
                    "the IMAP server supports neither MOVE nor UIDPLUS, \
                     so messages cannot be moved safely",
                )
                .into());
            }
            select(conn, &source, expected_uid_validity).await?;

            let quoted_target = quote(&target.raw_name)?;
            let mut destination_uid_validity = None;
            let mut mapping: HashMap<u32, u32> = HashMap::new();
            let mut reported = true;
            for set in &sets {
                let copied = if conn.caps.r#move {
                    command_with_copyuid(conn, format!("UID MOVE {set} {quoted_target}")).await?
                } else {
                    // Without MOVE: copy, mark exactly these messages as deleted, and expunge
                    // exactly these messages (UID EXPUNGE leaves other deleted messages alone).
                    let copied =
                        command_with_copyuid(conn, format!("UID COPY {set} {quoted_target}"))
                            .await?;
                    let mut updates = conn
                        .session
                        .uid_store(set, "+FLAGS.SILENT (\\Deleted)")
                        .await?;
                    while updates.try_next().await?.is_some() {}
                    drop(updates);
                    let expunged = conn.session.uid_expunge(set).await?;
                    futures_util::pin_mut!(expunged);
                    while expunged.try_next().await?.is_some() {}
                    copied
                };
                match copied {
                    Some((validity, pairs)) => {
                        destination_uid_validity = Some(validity);
                        mapping.extend(pairs);
                    }
                    None => reported = false,
                }
            }
            // If the server reported what it copied, only those messages were moved; otherwise
            // assume that all requested messages were.
            let moved = if reported {
                requested
                    .iter()
                    .filter_map(|uid| {
                        mapping.get(uid).map(|new_uid| Moved {
                            uid: *uid,
                            new_uid: Some(*new_uid),
                        })
                    })
                    .collect()
            } else {
                destination_uid_validity = None;
                requested
                    .iter()
                    .map(|uid| Moved {
                        uid: *uid,
                        new_uid: mapping.get(uid).copied(),
                    })
                    .collect()
            };
            Ok(MoveResult {
                destination_uid_validity,
                moved,
                destination_role: target.role,
            })
        })
        .await
    }

    pub async fn create_draft(&self, request: &DraftRequest) -> ApiResult<DraftResult> {
        let body = draft::validate(&request.to, &request.cc, &request.subject, &request.body)?;
        self.run(OP_TIMEOUT, async |account, conn| {
            let folders = folders(account, conn).await?;
            let drafts = folders
                .iter()
                .find(|f| f.role == Some(Role::Drafts) && f.selectable)
                .cloned()
                .ok_or_else(|| {
                    ApiError::unsupported(
                        "the account has no drafts folder; choose one in the account settings",
                    )
                })?;

            let mut in_reply_to = None;
            let mut references = Vec::new();
            if let Some(parent) = &request.in_reply_to {
                let original = fetch_original(account, conn, parent).await?;
                for recipient in request.to.iter().chain(&request.cc) {
                    let address = rules::normalise_address(&recipient.address);
                    if !original.participants.contains(&address) {
                        return Err(ApiError::forbidden(format!(
                            "{} is not a sender or recipient of the message being replied to",
                            recipient.address
                        ))
                        .into());
                    }
                }
                in_reply_to = original.message_id;
                references = original.references;
            }

            let config = &account.config;
            let (message, message_id) = draft::compose(&Draft {
                from_name: config.display_name.clone(),
                from_address: config.address.clone(),
                to: request.to.clone(),
                cc: request.cc.clone(),
                subject: request.subject.clone(),
                body,
                in_reply_to,
                references,
            })?;
            conn.session
                .append(&drafts.raw_name, Some("(\\Draft \\Seen)"), None, &message)
                .await?;
            Ok(DraftResult {
                folder: drafts.name,
                message_id,
            })
        })
        .await
    }
}

// ---------------------------------------------------------------------------------------------
// Helpers working on a connection

async fn read_greeting(client: &mut async_imap::Client<Stream>) -> OpResult<()> {
    match client.read_response().await {
        Ok(Some(_)) => Ok(()),
        Ok(None) => Err(Failure::reset(ApiError::unavailable(
            "the IMAP server closed the connection",
        ))),
        Err(e) => Err(e.into()),
    }
}

async fn refresh_folders(account: &Account, conn: &mut Conn) -> OpResult<Arc<Vec<Folder>>> {
    let names: Vec<_> = {
        let listing = conn.session.list(Some(""), Some("*")).await?;
        listing.try_collect().await?
    };
    let raw = names
        .iter()
        .map(|name| {
            let mut selectable = true;
            let mut special_use = None;
            for attribute in name.attributes() {
                match attribute {
                    NameAttribute::NoSelect => selectable = false,
                    NameAttribute::All => special_use = Some(Role::All),
                    NameAttribute::Archive => special_use = Some(Role::Archive),
                    NameAttribute::Drafts => special_use = Some(Role::Drafts),
                    NameAttribute::Flagged => special_use = Some(Role::Flagged),
                    NameAttribute::Junk => special_use = Some(Role::Junk),
                    NameAttribute::Sent => special_use = Some(Role::Sent),
                    NameAttribute::Trash => special_use = Some(Role::Trash),
                    NameAttribute::Extension(other)
                        if other.eq_ignore_ascii_case("\\NonExistent") =>
                    {
                        selectable = false;
                    }
                    _ => {}
                }
            }
            RawFolder {
                raw_name: name.name().to_string(),
                delimiter: name.delimiter().map(str::to_string),
                selectable,
                special_use,
            }
        })
        .collect();
    let folders = Arc::new(build_folders(
        raw,
        &conn.foreign_prefixes,
        &account.config.folders,
    ));
    conn.folders = Some((Instant::now(), folders.clone()));
    Ok(folders)
}

async fn folders(account: &Account, conn: &mut Conn) -> OpResult<Arc<Vec<Folder>>> {
    match &conn.folders {
        Some((fetched, folders)) if fetched.elapsed() < FOLDER_CACHE_TTL => Ok(folders.clone()),
        _ => refresh_folders(account, conn).await,
    }
}

/// Looks up a folder by its name. Only folders from the account's (filtered) folder list can be
/// addressed, which is what keeps arbitrary strings out of IMAP commands.
async fn resolve(account: &Account, conn: &mut Conn, name: &str) -> OpResult<Folder> {
    let find = |folders: &[Folder]| folders.iter().find(|f| f.name == name).cloned();
    let cached_at = conn.folders.as_ref().map(|(fetched, _)| *fetched);
    if let Some(folder) = find(&folders(account, conn).await?) {
        return Ok(folder);
    }
    // The folder may have been created since the list was cached.
    if cached_at.is_some_and(|fetched| fetched.elapsed() > Duration::from_secs(2))
        && let Some(folder) = find(&refresh_folders(account, conn).await?)
    {
        return Ok(folder);
    }
    Err(ApiError::not_found("no such folder").into())
}

fn require_selectable(folder: &Folder) -> OpResult<()> {
    if folder.selectable {
        Ok(())
    } else {
        Err(ApiError::bad_request(format!("{:?} cannot hold messages", folder.name)).into())
    }
}

fn uid_validity(mailbox: &Mailbox) -> OpResult<u32> {
    mailbox.uid_validity.ok_or_else(|| {
        Failure::from(ApiError::imap(
            "the IMAP server did not report a UIDVALIDITY",
        ))
    })
}

/// Opens a folder read-only.
async fn examine(conn: &mut Conn, folder: &Folder) -> OpResult<Mailbox> {
    require_selectable(folder)?;
    Ok(conn.session.examine(&folder.raw_name).await?)
}

/// Opens a folder for changes, and checks that it is still the folder the caller knows.
async fn select(conn: &mut Conn, folder: &Folder, expected_uid_validity: u32) -> OpResult<()> {
    require_selectable(folder)?;
    let mailbox = conn.session.select(&folder.raw_name).await?;
    let actual = uid_validity(&mailbox)?;
    if actual != expected_uid_validity {
        return Err(ApiError::uid_validity_changed(expected_uid_validity, actual).into());
    }
    Ok(())
}

fn change_sets(uids: &[u32]) -> ApiResult<Vec<String>> {
    if uids.len() > rules::MAX_UIDS_PER_CHANGE {
        return Err(ApiError::bad_request(format!(
            "at most {} messages can be changed at once",
            rules::MAX_UIDS_PER_CHANGE
        )));
    }
    rules::format_uid_set(uids)?; // validates the UIDs
    Ok(rules::chunk_uid_sets(uids))
}

/// Quotes a folder name for use in a command. Raw folder names come from the server's folder
/// list and are printable ASCII (see [`build_folders`]).
fn quote(raw_name: &str) -> OpResult<String> {
    if !raw_name.bytes().all(|b| (0x20..=0x7e).contains(&b)) {
        return Err(ApiError::bad_request("invalid folder name").into());
    }
    Ok(format!(
        "\"{}\"",
        raw_name.replace('\\', "\\\\").replace('"', "\\\"")
    ))
}

fn flag_name(flag: &Flag<'_>) -> String {
    match flag {
        Flag::Seen => "\\Seen".into(),
        Flag::Answered => "\\Answered".into(),
        Flag::Flagged => "\\Flagged".into(),
        Flag::Deleted => "\\Deleted".into(),
        Flag::Draft => "\\Draft".into(),
        Flag::Recent => "\\Recent".into(),
        Flag::MayCreate => "\\*".into(),
        Flag::Custom(name) => name.to_string(),
    }
}

fn meta(fetch: &Fetch, uid: u32, size: u32) -> MessageMeta {
    let mut flags: Vec<String> = fetch
        .flags()
        .map(|flag| flag_name(&flag))
        .filter(|flag| flag != "\\Recent")
        .collect();
    flags.sort();
    MessageMeta {
        uid,
        flags,
        size,
        internal_date: fetch.internal_date().map(|date| date.to_rfc3339()),
    }
}

fn frame_line(value: &serde_json::Value) -> Bytes {
    let mut line = serde_json::to_vec(value).expect("serializable");
    line.push(b'\n');
    Bytes::from(line)
}

type CopyUid = (u32, Vec<(u32, u32)>);

/// Runs a `UID MOVE` or `UID COPY` command and returns the UIDVALIDITY of the destination and
/// the old and new UIDs, if the server reports them (RFC 4315).
async fn command_with_copyuid(conn: &mut Conn, command: String) -> OpResult<Option<CopyUid>> {
    let id = conn.session.run_command(&command).await?;
    let mut copied = None;
    loop {
        let response = conn.session.read_response().await?.ok_or_else(|| {
            Failure::reset(ApiError::unavailable(
                "the connection to the IMAP server was lost",
            ))
        })?;
        match response.parsed() {
            Response::Done {
                tag,
                status,
                outcome,
            } if tag == &id => {
                if let Some(code) = &outcome.code {
                    copied = copy_uid(code).or(copied);
                }
                let information = &outcome.information;
                return match status {
                    Status::Ok => Ok(copied),
                    Status::No | Status::Bad => Err(ApiError::imap(format!(
                        "the IMAP server refused the command: {}",
                        truncate(information.as_deref().unwrap_or(""), 300)
                    ))
                    .into()),
                    _ => Err(Failure::reset(ApiError::unavailable(
                        "the IMAP server ended the session",
                    ))),
                };
            }
            Response::Data {
                status: Status::Ok,
                outcome,
            } => {
                if let Some(code) = &outcome.code {
                    copied = copy_uid(code).or(copied);
                }
            }
            _ => {}
        }
    }
}

fn copy_uid(code: &ResponseCode<'_>) -> Option<CopyUid> {
    let ResponseCode::CopyUid(validity, source, destination) = code else {
        return None;
    };
    let expand = |members: &[UidSetMember]| -> Option<Vec<u32>> {
        let mut uids = Vec::new();
        for member in members {
            match member {
                UidSetMember::Uid(uid) => uids.push(*uid),
                UidSetMember::UidRange(range) => {
                    let (lo, hi) = (
                        *range.start().min(range.end()),
                        *range.start().max(range.end()),
                    );
                    if (hi - lo) as usize >= MAX_COPYUID {
                        return None;
                    }
                    // A range may be written in either direction; the order matters, since
                    // source and destination correspond position by position.
                    if range.start() <= range.end() {
                        uids.extend(lo..=hi);
                    } else {
                        uids.extend((lo..=hi).rev());
                    }
                }
            }
            if uids.len() > MAX_COPYUID {
                return None;
            }
        }
        Some(uids)
    };
    let source = expand(source)?;
    let destination = expand(destination)?;
    if source.len() != destination.len() {
        return None;
    }
    Some((*validity, source.into_iter().zip(destination).collect()))
}

struct Original {
    /// Normalised addresses of everybody who sent or received the message.
    participants: HashSet<String>,
    message_id: Option<String>,
    references: Vec<String>,
}

/// Looks up the message that a draft replies to.
async fn fetch_original(
    account: &Account,
    conn: &mut Conn,
    parent: &MessageRef,
) -> OpResult<Original> {
    if parent.uid == 0 {
        return Err(ApiError::bad_request("0 is not a valid UID").into());
    }
    let folder = resolve(account, conn, &parent.folder).await?;
    let mailbox = examine(conn, &folder).await?;
    let actual = uid_validity(&mailbox)?;
    if actual != parent.uid_validity {
        return Err(ApiError::uid_validity_changed(parent.uid_validity, actual).into());
    }
    let mut original = None;
    {
        let mut fetches = conn
            .session
            .uid_fetch(parent.uid.to_string(), "(UID ENVELOPE BODY.PEEK[HEADER])")
            .await?;
        while let Some(fetch) = fetches.try_next().await? {
            if fetch.uid != Some(parent.uid) {
                continue;
            }
            let Some(envelope) = fetch.envelope() else {
                continue;
            };
            let mut participants = HashSet::new();
            let lists = [
                &envelope.from,
                &envelope.sender,
                &envelope.reply_to,
                &envelope.to,
                &envelope.cc,
            ];
            for address in lists.into_iter().flatten().flatten() {
                if let (Some(mailbox), Some(host)) = (&address.mailbox, &address.host) {
                    let address = format!(
                        "{}@{}",
                        String::from_utf8_lossy(mailbox),
                        String::from_utf8_lossy(host)
                    );
                    participants.insert(rules::normalise_address(&address));
                }
            }
            let message_id = envelope
                .message_id
                .as_ref()
                .and_then(|id| draft::parse_message_ids(&String::from_utf8_lossy(id)).pop());
            original = Some(Original {
                participants,
                message_id,
                references: fetch
                    .header()
                    .map(draft::references_from_header)
                    .unwrap_or_default(),
            });
        }
    }
    original
        .ok_or_else(|| ApiError::not_found("the message being replied to does not exist").into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raw(name: &str, special_use: Option<Role>) -> RawFolder {
        RawFolder {
            raw_name: name.to_string(),
            delimiter: Some("/".to_string()),
            selectable: true,
            special_use,
        }
    }

    fn by_name<'a>(folders: &'a [Folder], name: &str) -> &'a Folder {
        folders
            .iter()
            .find(|f| f.name == name)
            .unwrap_or_else(|| panic!("no folder {name}"))
    }

    #[test]
    fn roles_from_special_use_attributes() {
        let folders = build_folders(
            vec![
                raw("INBOX", None),
                raw("[Gmail]/Drafts", Some(Role::Drafts)),
                raw("[Gmail]/Sent Mail", Some(Role::Sent)),
                raw("[Gmail]/Bin", Some(Role::Trash)),
                raw("[Gmail]/All Mail", Some(Role::All)),
                raw("Projects", None),
            ],
            &[],
            &FolderOverrides::default(),
        );
        assert_eq!(by_name(&folders, "INBOX").role, Some(Role::Inbox));
        assert!(by_name(&folders, "[Gmail]/Drafts").protected);
        assert!(by_name(&folders, "[Gmail]/Sent Mail").protected);
        assert!(!by_name(&folders, "[Gmail]/Bin").protected);
        assert_eq!(by_name(&folders, "[Gmail]/Bin").role, Some(Role::Trash));
        assert_eq!(by_name(&folders, "Projects").role, None);
        assert!(!by_name(&folders, "Projects").protected);
    }

    #[test]
    fn roles_guessed_from_names_when_not_announced() {
        let folders = build_folders(
            vec![
                raw("inbox", None),
                raw("Drafts", None),
                raw("Sent Items", None),
                raw("Deleted Items", None),
                raw("Junk E-mail", None),
                raw("Old/Drafts", None),
                raw("Outbox", None),
            ],
            &[],
            &FolderOverrides::default(),
        );
        assert_eq!(by_name(&folders, "INBOX").role, Some(Role::Inbox));
        assert_eq!(by_name(&folders, "Drafts").role, Some(Role::Drafts));
        assert_eq!(by_name(&folders, "Sent Items").role, Some(Role::Sent));
        assert_eq!(by_name(&folders, "Deleted Items").role, Some(Role::Trash));
        assert_eq!(by_name(&folders, "Junk E-mail").role, Some(Role::Junk));
        // Not the drafts folder, but still not a place the app may move messages to.
        assert_eq!(by_name(&folders, "Old/Drafts").role, None);
        assert!(by_name(&folders, "Old/Drafts").protected);
        assert!(by_name(&folders, "Outbox").protected);
    }

    #[test]
    fn user_overrides_take_precedence() {
        let overrides = FolderOverrides {
            drafts: Some("Entwurf".into()),
            trash: Some("No such folder".into()),
            protected: vec!["Projects".into()],
            ..FolderOverrides::default()
        };
        let folders = build_folders(
            vec![
                raw("INBOX", None),
                raw("Drafts", Some(Role::Drafts)),
                raw("Entwurf", None),
                raw("Trash", Some(Role::Trash)),
                raw("Projects", None),
            ],
            &[],
            &overrides,
        );
        assert_eq!(by_name(&folders, "Entwurf").role, Some(Role::Drafts));
        assert!(by_name(&folders, "Entwurf").protected);
        assert_eq!(by_name(&folders, "Drafts").role, None);
        assert!(
            by_name(&folders, "Drafts").protected,
            "still protected by its name"
        );
        assert_eq!(by_name(&folders, "Trash").role, Some(Role::Trash));
        assert!(by_name(&folders, "Projects").protected);
    }

    #[test]
    fn foreign_namespaces_and_odd_names_are_dropped() {
        let folders = build_folders(
            vec![
                raw("INBOX", None),
                raw("Public", None),
                raw("Public/Announce", None),
                raw("Publicity", None),
                raw("user/other/INBOX", None),
                raw("B&APw-ro", None),
                raw("bad\u{7f}name", None),
                raw("caf\u{e9}", None),
                raw("", None),
                raw("B&APw-ro", None),
            ],
            &["Public/".to_string(), "user/".to_string(), String::new()],
            &FolderOverrides::default(),
        );
        let names: Vec<&str> = folders.iter().map(|f| f.name.as_str()).collect();
        assert_eq!(names, vec!["INBOX", "Publicity", "Büro"]);
        assert_eq!(by_name(&folders, "Büro").raw_name, "B&APw-ro");
    }

    #[test]
    fn namespace_parsing() {
        assert_eq!(
            parse_foreign_namespaces(r#"* NAMESPACE (("" "/")) NIL (("Public/" "/"))"#).unwrap(),
            vec!["Public/"]
        );
        assert_eq!(
            parse_foreign_namespaces(
                r##"* NAMESPACE (("INBOX." ".")) (("user." ".")) (("#shared." ".")("#news." "."))"##
            )
            .unwrap(),
            vec!["user.", "#shared.", "#news."]
        );
        assert_eq!(
            parse_foreign_namespaces(r#"* NAMESPACE (("" "/")) NIL NIL"#).unwrap(),
            Vec::<String>::new()
        );
        assert_eq!(
            parse_foreign_namespaces(
                r#"* NAMESPACE (("" "/" "X-PARAM" ("FLAG1" "FLAG2"))) (("Other \"Users\"/" "/")) NIL"#
            )
            .unwrap(),
            vec!["Other \"Users\"/"]
        );
        for bad in [
            "",
            "* LIST () \"/\" INBOX",
            "* NAMESPACE",
            "* NAMESPACE ((\"\" \"/\")) NIL",
            "* NAMESPACE ((\"\" \"/\")) NIL ((\"Public/",
            "* NAMESPACE garbage garbage garbage",
        ] {
            assert!(parse_foreign_namespaces(bad).is_none(), "{bad:?}");
        }
    }

    #[test]
    fn copyuid_pairs_old_and_new_uids() {
        let code = ResponseCode::CopyUid(
            7,
            vec![UidSetMember::UidRange(3..=5), UidSetMember::Uid(9)],
            vec![UidSetMember::UidRange(20..=23)],
        );
        let (validity, pairs) = copy_uid(&code).unwrap();
        assert_eq!(validity, 7);
        assert_eq!(pairs, vec![(3, 20), (4, 21), (5, 22), (9, 23)]);

        let mismatched = ResponseCode::CopyUid(
            7,
            vec![UidSetMember::UidRange(3..=5)],
            vec![UidSetMember::Uid(20)],
        );
        assert!(copy_uid(&mismatched).is_none());

        let huge = ResponseCode::CopyUid(
            7,
            vec![UidSetMember::UidRange(1..=u32::MAX)],
            vec![UidSetMember::UidRange(1..=u32::MAX)],
        );
        assert!(copy_uid(&huge).is_none());
        assert!(copy_uid(&ResponseCode::ReadOnly).is_none());
    }

    #[test]
    fn server_texts_are_cleaned_up() {
        assert_eq!(truncate("code: None, info: Some(\"x\")", 100), "x");
        assert_eq!(truncate("plain text", 100), "plain text");
        assert_eq!(
            truncate(
                "outcome: Outcome { code: None, information: Some(\"[NOPERM] Not \\\"allowed\\\"\") }",
                100
            ),
            "[NOPERM] Not \"allowed\""
        );
        assert_eq!(
            truncate("outcome: Outcome { code: None, information: None }", 100),
            ""
        );
        assert_eq!(truncate("a\r\nb", 2), "ab");
    }

    #[test]
    fn folder_names_are_quoted_for_commands() {
        assert_eq!(quote("Archive").ok().unwrap(), "\"Archive\"");
        assert_eq!(quote("a\"b\\c").ok().unwrap(), "\"a\\\"b\\\\c\"");
        assert!(quote("a\r\nb").is_err());
    }
}
