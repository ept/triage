//! The gateway's rules, as pure functions.
//!
//! Everything the email sandbox sends is untrusted. Each value is validated here before any of it
//! is turned into an IMAP command, so that the rest of the gateway only ever handles values that
//! are safe to embed in commands. Nothing in this module performs I/O.

use chrono::NaiveDate;
use serde::{Deserialize, Serialize};

use crate::error::{ApiError, ApiResult};

pub const MAX_UIDS_PER_CHANGE: usize = 500;
pub const MAX_UIDS_PER_FETCH: usize = 100;
pub const MAX_LIST_LIMIT: usize = 2000;
pub const MAX_SEARCH_STRING: usize = 200;
pub const MAX_SEARCH_DEPTH: usize = 5;
pub const MAX_SEARCH_BRANCHES: usize = 20;
pub const MAX_SEARCH_COMMAND: usize = 4000;
pub const MAX_FOLDER_NAME: usize = 200;
pub const MAX_FOLDER_DEPTH: usize = 10;
pub const MAX_SUBJECT: usize = 500;
pub const MAX_BODY: usize = 100_000;
pub const MAX_RECIPIENTS: usize = 50;
pub const MAX_DISPLAY_NAME: usize = 200;
pub const MAX_CHANGE_REQUEST: usize = 4000;

// ---------------------------------------------------------------------------------------------
// Identifiers and addresses

pub fn validate_account_id(id: &str) -> ApiResult<()> {
    let ok = (1..=32).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
    if ok {
        Ok(())
    } else {
        Err(ApiError::bad_request(
            "an account id is 1 to 32 characters from a-z, 0-9 and '-'",
        ))
    }
}

pub fn validate_hostname(host: &str) -> ApiResult<()> {
    let label_ok = |label: &str| {
        (1..=63).contains(&label.len())
            && label
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
            && !label.starts_with('-')
            && !label.ends_with('-')
    };
    if (1..=253).contains(&host.len()) && host.split('.').all(label_ok) {
        Ok(())
    } else {
        Err(ApiError::bad_request("invalid host name"))
    }
}

/// Accepts the common form of email address only: an unquoted local part and a host name. This
/// is deliberately stricter than RFC 5322, because the address ends up in a message header.
pub fn validate_address(address: &str) -> ApiResult<()> {
    let invalid = || ApiError::bad_request(format!("invalid email address {address:?}"));
    let (local, domain) = address.rsplit_once('@').ok_or_else(invalid)?;
    let local_ok = (1..=64).contains(&local.len())
        && local
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+/=?^_`{|}~.-".contains(&b))
        && !local.starts_with('.')
        && !local.ends_with('.')
        && !local.contains("..");
    if local_ok && domain.contains('.') && validate_hostname(domain).is_ok() {
        Ok(())
    } else {
        Err(invalid())
    }
}

/// Canonical form of an address for comparisons.
pub fn normalise_address(address: &str) -> String {
    address.trim().to_ascii_lowercase()
}

pub fn validate_display_name(name: &str) -> ApiResult<()> {
    if name.chars().count() > MAX_DISPLAY_NAME || name.chars().any(|c| c.is_control()) {
        return Err(ApiError::bad_request("invalid display name"));
    }
    validate_visible_text(name, Visibility::Lenient)
}

// ---------------------------------------------------------------------------------------------
// Text that a person is going to review

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Visibility {
    /// No invisible characters at all. Used for change requests, whose exact text the user
    /// approves before it leaves the machine.
    Strict,
    /// Single zero-width joiners/non-joiners and emoji variation selectors are tolerated, since
    /// several scripts and emoji need them, but not runs of them.
    Lenient,
}

/// Characters that take up no space, or that change how the surrounding text is displayed. They
/// could be used to hide data in a text that the user believes to have read in full.
fn is_invisible(c: char) -> bool {
    matches!(c,
        '\u{00AD}'                  // soft hyphen
        | '\u{034F}'                // combining grapheme joiner
        | '\u{061C}'                // Arabic letter mark
        | '\u{115F}' | '\u{1160}'   // Hangul fillers
        | '\u{17B4}' | '\u{17B5}'
        | '\u{180B}'..='\u{180F}'
        | '\u{200B}'..='\u{200F}'   // zero-width characters, direction marks
        | '\u{2028}'..='\u{202E}'   // line/paragraph separators, bidi embeddings and overrides
        | '\u{2060}'..='\u{206F}'   // word joiner, invisible operators, bidi isolates
        | '\u{3164}'
        | '\u{FE00}'..='\u{FE0F}'   // variation selectors
        | '\u{FEFF}'
        | '\u{FFA0}'
        | '\u{FFF0}'..='\u{FFFB}'
        | '\u{1BCA0}'..='\u{1BCA3}'
        | '\u{1D173}'..='\u{1D17A}'
        | '\u{E000}'..='\u{F8FF}'   // private use
        | '\u{E0000}'..='\u{E0FFF}' // tags, variation selectors supplement
        | '\u{F0000}'..='\u{10FFFF}' // private use planes
    )
}

fn is_tolerated_joiner(c: char) -> bool {
    matches!(c, '\u{200C}' | '\u{200D}' | '\u{FE0F}')
}

pub fn validate_visible_text(text: &str, visibility: Visibility) -> ApiResult<()> {
    let mut previous_invisible = false;
    for c in text.chars() {
        let invisible = is_invisible(c);
        let allowed = !invisible
            || (visibility == Visibility::Lenient && is_tolerated_joiner(c) && !previous_invisible);
        if !allowed {
            return Err(ApiError::forbidden(format!(
                "the text contains the invisible character U+{:04X}",
                c as u32
            )));
        }
        previous_invisible = invisible;
    }
    Ok(())
}

/// The text of a message to the coding agent. Returns the text with line endings normalised.
pub fn validate_change_request_text(text: &str) -> ApiResult<String> {
    let text = text.replace("\r\n", "\n");
    let length = text.chars().count();
    if text.trim().is_empty() {
        return Err(ApiError::bad_request("the text is empty"));
    }
    if length > MAX_CHANGE_REQUEST {
        return Err(ApiError::bad_request(format!(
            "the text has {length} characters; the limit is {MAX_CHANGE_REQUEST}"
        )));
    }
    if text
        .chars()
        .any(|c| c.is_control() && c != '\n' && c != '\t')
    {
        return Err(ApiError::bad_request(
            "the text contains control characters",
        ));
    }
    validate_visible_text(&text, Visibility::Strict)?;
    Ok(text)
}

pub fn validate_subject(subject: &str) -> ApiResult<()> {
    if subject.chars().count() > MAX_SUBJECT {
        return Err(ApiError::bad_request("the subject is too long"));
    }
    if subject.chars().any(|c| c.is_control()) {
        return Err(ApiError::bad_request(
            "the subject contains control characters",
        ));
    }
    validate_visible_text(subject, Visibility::Lenient)
}

/// The body of a draft. Returns the body with CRLF line endings.
pub fn validate_body(body: &str) -> ApiResult<String> {
    if body.chars().count() > MAX_BODY {
        return Err(ApiError::bad_request("the body is too long"));
    }
    if body
        .chars()
        .any(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t'))
    {
        return Err(ApiError::bad_request(
            "the body contains control characters",
        ));
    }
    validate_visible_text(body, Visibility::Lenient)?;
    let unix = body.replace("\r\n", "\n").replace('\r', "\n");
    Ok(unix.replace('\n', "\r\n"))
}

// ---------------------------------------------------------------------------------------------
// Flags

/// Checks a flag that the app wants to add or remove, and returns it in canonical spelling.
pub fn validate_flag(flag: &str) -> ApiResult<String> {
    if let Some(system) = flag.strip_prefix('\\') {
        return match system.to_ascii_lowercase().as_str() {
            "seen" => Ok("\\Seen".into()),
            "flagged" => Ok("\\Flagged".into()),
            "answered" => Ok("\\Answered".into()),
            "deleted" => Err(ApiError::forbidden(
                "\\Deleted cannot be set: messages are deleted by moving them to the trash",
            )),
            "draft" => Err(ApiError::forbidden("\\Draft cannot be changed")),
            _ => Err(ApiError::forbidden(format!(
                "flag {flag:?} cannot be changed"
            ))),
        };
    }
    validate_keyword(flag)?;
    Ok(flag.to_string())
}

pub fn validate_keyword(keyword: &str) -> ApiResult<()> {
    let mut bytes = keyword.bytes();
    let first_ok = bytes
        .next()
        .is_some_and(|b| b.is_ascii_alphabetic() || b == b'$');
    let rest_ok = bytes.all(|b| b.is_ascii_alphanumeric() || b"$_.-".contains(&b));
    if first_ok && rest_ok && keyword.len() <= 64 {
        Ok(())
    } else {
        Err(ApiError::bad_request(format!(
            "invalid keyword {keyword:?}"
        )))
    }
}

// ---------------------------------------------------------------------------------------------
// UID sets

/// Formats UIDs as an IMAP sequence set such as `1:3,7,9:10`.
pub fn format_uid_set(uids: &[u32]) -> ApiResult<String> {
    if uids.is_empty() {
        return Err(ApiError::bad_request("no UIDs given"));
    }
    if uids.contains(&0) {
        return Err(ApiError::bad_request("0 is not a valid UID"));
    }
    let mut sorted = uids.to_vec();
    sorted.sort_unstable();
    sorted.dedup();

    let mut out = String::new();
    let mut start = sorted[0];
    let mut end = sorted[0];
    let flush = |start: u32, end: u32, out: &mut String| {
        if !out.is_empty() {
            out.push(',');
        }
        if start == end {
            out.push_str(&start.to_string());
        } else {
            out.push_str(&format!("{start}:{end}"));
        }
    };
    for &uid in &sorted[1..] {
        if uid == end + 1 {
            end = uid;
        } else {
            flush(start, end, &mut out);
            start = uid;
            end = uid;
        }
    }
    flush(start, end, &mut out);
    Ok(out)
}

/// Splits UIDs into sequence sets that are each short enough for a single command line.
pub fn chunk_uid_sets(uids: &[u32]) -> Vec<String> {
    const MAX_SET_LENGTH: usize = 1000;
    let mut sorted = uids.to_vec();
    sorted.sort_unstable();
    sorted.dedup();
    let mut sets = Vec::new();
    let mut current: Vec<u32> = Vec::new();
    let mut length = 0;
    for uid in sorted {
        let digits = uid.to_string().len() + 1;
        if length + digits > MAX_SET_LENGTH && !current.is_empty() {
            sets.push(format_uid_set(&current).expect("non-empty set of non-zero UIDs"));
            current.clear();
            length = 0;
        }
        current.push(uid);
        length += digits;
    }
    if !current.is_empty() {
        sets.push(format_uid_set(&current).expect("non-empty set of non-zero UIDs"));
    }
    sets
}

// ---------------------------------------------------------------------------------------------
// Search

/// A search query as the app sends it; see `docs/gateway-api.md`.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum Query {
    All(bool),
    And(Vec<Query>),
    Or(Vec<Query>),
    Not(Box<Query>),
    Since(NaiveDate),
    Before(NaiveDate),
    SentSince(NaiveDate),
    SentBefore(NaiveDate),
    From(String),
    To(String),
    Cc(String),
    Subject(String),
    Body(String),
    Text(String),
    Header(String, String),
    Seen(bool),
    Flagged(bool),
    Answered(bool),
    Draft(bool),
    Keyword(String),
    Larger(u32),
    Smaller(u32),
    Uid(u32, u32),
}

impl Query {
    /// Renders the query as the argument of an IMAP `UID SEARCH` command.
    pub fn to_imap(&self) -> ApiResult<String> {
        let rendered = self.render(1)?;
        if rendered.len() > MAX_SEARCH_COMMAND {
            return Err(ApiError::bad_request("the search query is too long"));
        }
        Ok(rendered)
    }

    fn render(&self, depth: usize) -> ApiResult<String> {
        if depth > MAX_SEARCH_DEPTH {
            return Err(ApiError::bad_request(
                "the search query is nested too deeply",
            ));
        }
        let branches = |queries: &[Query]| -> ApiResult<Vec<String>> {
            if queries.is_empty() || queries.len() > MAX_SEARCH_BRANCHES {
                return Err(ApiError::bad_request(format!(
                    "\"and\"/\"or\" take 1 to {MAX_SEARCH_BRANCHES} subqueries"
                )));
            }
            queries.iter().map(|q| q.render(depth + 1)).collect()
        };
        let flag = |set: bool, name: &str| {
            if set {
                name.to_string()
            } else {
                format!("UN{name}")
            }
        };
        let date = |d: &NaiveDate| d.format("%-d-%b-%Y").to_string();

        Ok(match self {
            Query::All(true) => "ALL".into(),
            Query::All(false) => {
                return Err(ApiError::bad_request("\"all\" must be true"));
            }
            Query::And(queries) => format!("({})", branches(queries)?.join(" ")),
            Query::Or(queries) => {
                // IMAP's OR takes exactly two keys, so nest: OR a OR b c
                let mut rendered = branches(queries)?;
                let mut out = rendered.pop().expect("branches() rejects empty lists");
                while let Some(previous) = rendered.pop() {
                    out = format!("OR {previous} {out}");
                }
                out
            }
            Query::Not(query) => format!("NOT {}", query.render(depth + 1)?),
            Query::Since(d) => format!("SINCE {}", date(d)),
            Query::Before(d) => format!("BEFORE {}", date(d)),
            Query::SentSince(d) => format!("SENTSINCE {}", date(d)),
            Query::SentBefore(d) => format!("SENTBEFORE {}", date(d)),
            Query::From(s) => format!("FROM {}", quote_search_string(s)?),
            Query::To(s) => format!("TO {}", quote_search_string(s)?),
            Query::Cc(s) => format!("CC {}", quote_search_string(s)?),
            Query::Subject(s) => format!("SUBJECT {}", quote_search_string(s)?),
            Query::Body(s) => format!("BODY {}", quote_search_string(s)?),
            Query::Text(s) => format!("TEXT {}", quote_search_string(s)?),
            Query::Header(name, s) => {
                let name_ok = (1..=64).contains(&name.len())
                    && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-');
                if !name_ok {
                    return Err(ApiError::bad_request("invalid header name"));
                }
                format!("HEADER {name} {}", quote_search_string(s)?)
            }
            Query::Seen(b) => flag(*b, "SEEN"),
            Query::Flagged(b) => flag(*b, "FLAGGED"),
            Query::Answered(b) => flag(*b, "ANSWERED"),
            Query::Draft(b) => flag(*b, "DRAFT"),
            Query::Keyword(k) => {
                validate_keyword(k)?;
                format!("KEYWORD {k}")
            }
            Query::Larger(n) => format!("LARGER {n}"),
            Query::Smaller(n) => format!("SMALLER {n}"),
            Query::Uid(lo, hi) => {
                if *lo == 0 || lo > hi {
                    return Err(ApiError::bad_request("invalid UID range"));
                }
                format!("UID {lo}:{hi}")
            }
        })
    }
}

/// Renders a search string as an IMAP quoted string. Only printable ASCII is accepted for now:
/// anything else would have to be sent as a literal with a CHARSET argument.
fn quote_search_string(s: &str) -> ApiResult<String> {
    if s.is_empty() || s.len() > MAX_SEARCH_STRING {
        return Err(ApiError::bad_request(format!(
            "a search string has 1 to {MAX_SEARCH_STRING} characters"
        )));
    }
    if !s.bytes().all(|b| (0x20..=0x7e).contains(&b)) {
        return Err(ApiError::unsupported(
            "search strings must be printable ASCII for now",
        ));
    }
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        if c == '"' || c == '\\' {
            out.push('\\');
        }
        out.push(c);
    }
    out.push('"');
    Ok(out)
}

// ---------------------------------------------------------------------------------------------
// Folders

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Inbox,
    Drafts,
    Sent,
    Trash,
    Junk,
    Archive,
    All,
    Flagged,
}

fn normalise_leaf(leaf: &str) -> String {
    leaf.trim().to_lowercase()
}

/// Guesses the role of a folder from the last component of its name, for servers that do not
/// announce special-use folders. The user can override the result in the account settings.
pub fn role_from_name(leaf: &str) -> Option<Role> {
    let leaf = normalise_leaf(leaf);
    let is = |names: &[&str]| names.contains(&leaf.as_str());
    if is(DRAFTS_NAMES) {
        Some(Role::Drafts)
    } else if is(SENT_NAMES) {
        Some(Role::Sent)
    } else if is(TRASH_NAMES) {
        Some(Role::Trash)
    } else if is(JUNK_NAMES) {
        Some(Role::Junk)
    } else if is(ARCHIVE_NAMES) {
        Some(Role::Archive)
    } else {
        None
    }
}

/// Whether a folder with this leaf name must be treated as one from which messages may be sent,
/// or in which a message would be mistaken for something the user wrote, whatever the server
/// says about its role.
pub fn is_protected_name(leaf: &str) -> bool {
    let leaf = normalise_leaf(leaf);
    [DRAFTS_NAMES, SENT_NAMES, OUTBOX_NAMES]
        .iter()
        .any(|names| names.contains(&leaf.as_str()))
}

const DRAFTS_NAMES: &[&str] = &[
    "drafts",
    "draft",
    "entwürfe",
    "brouillons",
    "borradores",
    "bozze",
    "concepten",
    "rascunhos",
    "utkast",
    "kladder",
];
const SENT_NAMES: &[&str] = &[
    "sent",
    "sent items",
    "sent mail",
    "sent messages",
    "gesendet",
    "gesendete elemente",
    "gesendete objekte",
    "envoyés",
    "éléments envoyés",
    "messages envoyés",
    "enviados",
    "elementos enviados",
    "inviati",
    "posta inviata",
    "verzonden",
    "verzonden items",
    "itens enviados",
    "skickat",
    "sendt",
];
const OUTBOX_NAMES: &[&str] = &[
    "outbox",
    "postausgang",
    "boîte d'envoi",
    "bandeja de salida",
    "posta in uscita",
    "postvak uit",
    "caixa de saída",
    "templates",
    "vorlagen",
    "modèles",
    "scheduled",
    "send later",
    "queue",
];
const TRASH_NAMES: &[&str] = &[
    "trash",
    "bin",
    "deleted items",
    "deleted messages",
    "deleted",
    "papierkorb",
    "gelöschte elemente",
    "gelöschte objekte",
    "corbeille",
    "éléments supprimés",
    "papelera",
    "elementos eliminados",
    "cestino",
    "posta eliminata",
    "prullenbak",
    "verwijderde items",
    "lixo",
    "itens excluídos",
];
const JUNK_NAMES: &[&str] = &[
    "junk",
    "spam",
    "junk e-mail",
    "junk email",
    "junk mail",
    "bulk mail",
    "courrier indésirable",
    "correo no deseado",
    "posta indesiderata",
    "ongewenste e-mail",
];
const ARCHIVE_NAMES: &[&str] = &["archive", "archives", "archiv", "archivo", "archivio"];

/// Checks the name of a folder that the app wants to create.
pub fn validate_new_folder_name(name: &str, delimiter: Option<&str>) -> ApiResult<()> {
    let invalid = |why: &str| ApiError::bad_request(format!("invalid folder name: {why}"));
    if name.is_empty() || name.chars().count() > MAX_FOLDER_NAME {
        return Err(invalid("it must have 1 to 200 characters"));
    }
    if name.chars().any(|c| c.is_control())
        || validate_visible_text(name, Visibility::Strict).is_err()
    {
        return Err(invalid("control and invisible characters are not allowed"));
    }
    if name.contains(['*', '%', '"', '\\']) {
        return Err(invalid("the characters * % \" \\ are not allowed"));
    }
    if name.starts_with(['#', '~']) {
        return Err(invalid("it must not start with # or ~"));
    }
    if name.eq_ignore_ascii_case("inbox") {
        return Err(invalid("the inbox exists already"));
    }
    let components: Vec<&str> = match delimiter {
        Some(delimiter) if !delimiter.is_empty() => name.split(delimiter).collect(),
        _ => vec![name],
    };
    if components.len() > MAX_FOLDER_DEPTH {
        return Err(invalid("it is nested too deeply"));
    }
    for component in components {
        if component.is_empty() || component.trim() != component {
            return Err(invalid(
                "components must be non-empty, without surrounding spaces",
            ));
        }
        if is_protected_name(component) {
            return Err(ApiError::forbidden(format!(
                "a folder named {component:?} could be mistaken for a drafts, sent or outbox folder"
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::StatusCode;

    fn query(json: serde_json::Value) -> ApiResult<String> {
        let query: Query = serde_json::from_value(json).expect("query should deserialize");
        query.to_imap()
    }

    #[test]
    fn account_ids() {
        validate_account_id("work-2").unwrap();
        for bad in ["", "Work", "a b", "a/b", &"a".repeat(33)] {
            assert!(validate_account_id(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn hostnames() {
        for good in ["imap.example.org", "localhost", "127.0.0.1", "a-b.example"] {
            validate_hostname(good).unwrap();
        }
        for bad in [
            "",
            "imap..example.org",
            "-a.example",
            "a.example/",
            "a example",
            "a.example:993",
            "exa\r\nmple.org",
        ] {
            assert!(validate_hostname(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn addresses() {
        for good in ["user@example.org", "first.last+tag@mail.example.org"] {
            validate_address(good).unwrap();
        }
        for bad in [
            "",
            "user",
            "user@localhost",
            "@example.org",
            "a b@example.org",
            "user@exa mple.org",
            "user@example.org\r\nBcc: x@example.net",
            "\"quoted\"@example.org",
            "user@example.org, other@example.net",
            "<user@example.org>",
            ".user@example.org",
            "us..er@example.org",
        ] {
            assert!(validate_address(bad).is_err(), "{bad:?}");
        }
        assert_eq!(normalise_address(" User@Example.ORG "), "user@example.org");
    }

    #[test]
    fn flags() {
        assert_eq!(validate_flag("\\seen").unwrap(), "\\Seen");
        assert_eq!(validate_flag("\\Flagged").unwrap(), "\\Flagged");
        assert_eq!(validate_flag("$Triage-Done").unwrap(), "$Triage-Done");
        assert_eq!(
            validate_flag("\\Deleted").unwrap_err().status,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            validate_flag("\\DELETED").unwrap_err().status,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            validate_flag("\\Draft").unwrap_err().status,
            StatusCode::FORBIDDEN
        );
        assert!(validate_flag("\\Recent").is_err());
        for bad in [
            "",
            "a b",
            "a)",
            "(a",
            "a\r\nb",
            "1abc",
            "a{3}",
            "a\"b",
            "a%",
            "a*",
            "ü",
            &"a".repeat(65),
        ] {
            assert!(validate_flag(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn uid_sets() {
        assert_eq!(format_uid_set(&[5]).unwrap(), "5");
        assert_eq!(format_uid_set(&[3, 1, 2]).unwrap(), "1:3");
        assert_eq!(
            format_uid_set(&[1, 2, 3, 7, 9, 10, 9]).unwrap(),
            "1:3,7,9:10"
        );
        assert_eq!(
            format_uid_set(&[u32::MAX, u32::MAX - 1]).unwrap(),
            "4294967294:4294967295"
        );
        assert!(format_uid_set(&[]).is_err());
        assert!(format_uid_set(&[0, 1]).is_err());
    }

    #[test]
    fn uid_sets_are_chunked_to_fit_a_command_line() {
        assert!(chunk_uid_sets(&[]).is_empty());
        assert_eq!(chunk_uid_sets(&[3, 1, 2]), vec!["1:3"]);
        // Worst case: no two UIDs are adjacent.
        let sparse: Vec<u32> = (0..500).map(|i| 4_000_000_000 + i * 2).collect();
        let sets = chunk_uid_sets(&sparse);
        assert!(sets.len() > 1);
        assert!(sets.iter().all(|set| set.len() <= 1000));
        let total: usize = sets.iter().map(|set| set.split(',').count()).sum();
        assert_eq!(total, 500);
    }

    #[test]
    fn search_rendering() {
        use serde_json::json;
        assert_eq!(query(json!({"all": true})).unwrap(), "ALL");
        assert_eq!(
            query(json!({"and": [{"since": "2026-08-01"}, {"from": "alice"}, {"seen": false}]}))
                .unwrap(),
            "(SINCE 1-Aug-2026 FROM \"alice\" UNSEEN)"
        );
        assert_eq!(
            query(json!({"or": [{"flagged": true}, {"keyword": "$Todo"}, {"larger": 1000}]}))
                .unwrap(),
            "OR FLAGGED OR KEYWORD $Todo LARGER 1000"
        );
        assert_eq!(
            query(json!({"not": {"header": ["List-Id", "announce"]}})).unwrap(),
            "NOT HEADER List-Id \"announce\""
        );
        assert_eq!(query(json!({"uid": [5, 9]})).unwrap(), "UID 5:9");
        assert_eq!(
            query(json!({"sent_before": "2026-12-25"})).unwrap(),
            "SENTBEFORE 25-Dec-2026"
        );
        assert_eq!(query(json!({"or": [{"draft": false}]})).unwrap(), "UNDRAFT");
    }

    #[test]
    fn search_strings_cannot_break_out_of_their_quotes() {
        use serde_json::json;
        assert_eq!(
            query(json!({"subject": "a\" DELETED \\"})).unwrap(),
            "SUBJECT \"a\\\" DELETED \\\\\""
        );
        for bad in ["line\r\nbreak", "nul\0", "tab\there", ""] {
            assert!(query(json!({"subject": bad})).is_err(), "{bad:?}");
        }
        let non_ascii = query(json!({"subject": "caf\u{e9}"})).unwrap_err();
        assert_eq!(non_ascii.status, StatusCode::NOT_IMPLEMENTED);
        assert!(query(json!({"subject": "x".repeat(201)})).is_err());
        assert!(query(json!({"header": ["Bad Name", "x"]})).is_err());
        assert!(query(json!({"header": ["X\r\nA1 LOGOUT", "x"]})).is_err());
        assert!(query(json!({"keyword": "a b"})).is_err());
    }

    #[test]
    fn search_limits() {
        use serde_json::json;
        assert!(query(json!({"and": []})).is_err());
        assert!(query(json!({"all": false})).is_err());
        assert!(query(json!({"uid": [0, 5]})).is_err());
        assert!(query(json!({"uid": [9, 5]})).is_err());

        let mut nested = json!({"all": true});
        for _ in 0..MAX_SEARCH_DEPTH {
            nested = json!({"not": nested});
        }
        assert!(query(nested).is_err());

        let wide: Vec<_> = (0..21).map(|_| json!({"seen": true})).collect();
        assert!(query(json!({"or": wide})).is_err());

        let long: Vec<_> = (0..20).map(|_| json!({"text": "x".repeat(200)})).collect();
        let long2: Vec<_> = (0..20).map(|_| json!({"and": long.clone()})).collect();
        assert!(query(json!({"and": long2})).is_err());

        let unknown: Result<Query, _> = serde_json::from_value(json!({"deleted": true}));
        assert!(unknown.is_err());
    }

    #[test]
    fn folder_roles_from_names() {
        assert_eq!(role_from_name("Drafts"), Some(Role::Drafts));
        assert_eq!(role_from_name("Sent Items"), Some(Role::Sent));
        assert_eq!(role_from_name(" Gelöschte Elemente "), Some(Role::Trash));
        assert_eq!(role_from_name("SPAM"), Some(Role::Junk));
        assert_eq!(role_from_name("Archive"), Some(Role::Archive));
        assert_eq!(role_from_name("Projects"), None);
        assert!(is_protected_name("OUTBOX"));
        assert!(is_protected_name("Entwürfe"));
        assert!(is_protected_name("Templates"));
        assert!(!is_protected_name("Trash"));
        assert!(!is_protected_name("Archive"));
    }

    #[test]
    fn new_folder_names() {
        validate_new_folder_name("Triage/Newsletters", Some("/")).unwrap();
        validate_new_folder_name("Büro & Verwaltung", Some(".")).unwrap();
        validate_new_folder_name("flat", None).unwrap();
        for bad in [
            "",
            "INBOX",
            "inbox",
            "a//b",
            "/a",
            "a/",
            "a/ b",
            "a*",
            "a%b",
            "a\"b",
            "a\\b",
            "#shared/x",
            "~other/x",
            "a\r\nb",
            "a\u{200b}b",
            "a/b/c/d/e/f/g/h/i/j/k",
        ] {
            assert!(validate_new_folder_name(bad, Some("/")).is_err(), "{bad:?}");
        }
        for protected in ["Drafts", "Triage/Sent", "outbox", "Triage/Templates/x"] {
            let error = validate_new_folder_name(protected, Some("/")).unwrap_err();
            assert_eq!(error.status, StatusCode::FORBIDDEN, "{protected}");
        }
        assert!(validate_new_folder_name(&"x".repeat(201), Some("/")).is_err());
    }

    #[test]
    fn change_request_text() {
        assert_eq!(
            validate_change_request_text("Please add\r\na button").unwrap(),
            "Please add\na button"
        );
        assert!(validate_change_request_text("   \n").is_err());
        assert!(validate_change_request_text(&"x".repeat(4001)).is_err());
        validate_change_request_text(&"x".repeat(4000)).unwrap();
        assert!(validate_change_request_text("bell\u{7}").is_err());
        assert!(validate_change_request_text("lone\rreturn").is_err());
        // Invisible characters could smuggle data past the person approving the text.
        for hidden in [
            "zero\u{200b}width",
            "tag\u{e0041}",
            "bidi\u{202e}override",
            "joiner\u{200d}",
            "selector\u{fe0f}",
            "private\u{e000}",
            "bom\u{feff}",
        ] {
            let error = validate_change_request_text(hidden).unwrap_err();
            assert_eq!(error.status, StatusCode::FORBIDDEN, "{hidden:?}");
        }
        validate_change_request_text("Größe, 日本語 and emoji 🙂 are fine").unwrap();
    }

    #[test]
    fn draft_text() {
        validate_subject("Re: Lunch – Größe 日本語").unwrap();
        assert!(validate_subject("a\nBcc: x@example.org").is_err());
        assert!(validate_subject(&"x".repeat(501)).is_err());
        assert_eq!(validate_body("a\nb\r\nc\rd").unwrap(), "a\r\nb\r\nc\r\nd");
        assert!(validate_body("nul\0").is_err());
        assert!(validate_body(&"x".repeat(MAX_BODY + 1)).is_err());
        // A single joiner is needed by some scripts and emoji; a run of them hides data.
        validate_body("می\u{200c}خواهم and 👨\u{200d}👩\u{200d}👧 and ❤\u{fe0f}").unwrap();
        assert!(validate_body("a\u{200c}\u{200d}b").is_err());
        assert!(validate_body("a\u{200b}b").is_err());
        assert!(validate_body("a\u{e0041}b").is_err());
        assert!(validate_body("a\u{202e}b").is_err());
    }
}
