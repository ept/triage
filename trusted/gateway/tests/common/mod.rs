//! Test support: a throwaway Dovecot IMAP server, running as the current user in a temporary
//! directory, and helpers to put synthetic messages into it.
//!
//! The tests need the `dovecot` binary (Debian package `dovecot-imapd`). All mail in these tests
//! is synthetic.

#![allow(dead_code)]

use std::fs;
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use triage_gateway::config::AccountConfig;

pub const PASSWORD: &str = "correct-horse-battery-staple";

pub struct Dovecot {
    dir: tempfile::TempDir,
    child: Child,
    pub port: u16,
    counter: AtomicU32,
}

#[derive(Default)]
pub struct Options {
    /// Capabilities to hide from the capability list, e.g. `MOVE`.
    pub without_capabilities: Vec<&'static str>,
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .expect("bind to a free port")
        .local_addr()
        .unwrap()
        .port()
}

fn current(what: &str) -> String {
    let output = Command::new("id").arg(what).output().expect("run id");
    String::from_utf8(output.stdout).unwrap().trim().to_string()
}

impl Dovecot {
    pub fn start() -> Dovecot {
        Dovecot::start_with(Options::default())
    }

    pub fn start_with(options: Options) -> Dovecot {
        // Dovecot's Unix sockets need a short path.
        let dir = tempfile::Builder::new()
            .prefix("dv")
            .tempdir_in("/tmp")
            .expect("create temp dir");
        let d = dir.path().display().to_string();
        let port = free_port();
        let user = current("-un");
        let group = current("-gn");
        let uid = current("-u");

        let all_capabilities = "IMAP4rev1 SASL-IR LOGIN-REFERRALS ID ENABLE IDLE SORT \
            THREAD=REFERENCES MULTIAPPEND UNSELECT CHILDREN NAMESPACE UIDPLUS LIST-EXTENDED \
            I18NLEVEL=1 CONDSTORE QRESYNC ESEARCH ESORT SEARCHRES WITHIN CONTEXT=SEARCH \
            LIST-STATUS BINARY MOVE SPECIAL-USE LITERAL+";
        let capability_line = if options.without_capabilities.is_empty() {
            String::new()
        } else {
            let kept: Vec<&str> = all_capabilities
                .split_whitespace()
                .filter(|c| !options.without_capabilities.contains(c))
                .collect();
            format!("imap_capability = {}\n", kept.join(" "))
        };

        let config = format!(
            r#"
base_dir = {d}/run
state_dir = {d}/state
log_path = {d}/dovecot.log
protocols = imap
listen = 127.0.0.1
ssl = no
disable_plaintext_auth = no
auth_mechanisms = plain login
default_internal_user = {user}
default_internal_group = {group}
default_login_user = {user}
first_valid_uid = {uid}
mail_location = maildir:{d}/mail/%u
mail_plugins = acl
{capability_line}
protocol imap {{
  mail_plugins = acl imap_acl
}}
plugin {{
  acl = vfile
}}
passdb {{
  driver = static
  args = password={PASSWORD}
}}
userdb {{
  driver = static
  args = uid={user} gid={group} home={d}/home/%u
}}
service imap-login {{
  chroot =
  inet_listener imap {{
    port = {port}
  }}
  inet_listener imaps {{
    port = 0
  }}
}}
service anvil {{
  chroot =
}}
service stats {{
  chroot =
}}
namespace inbox {{
  inbox = yes
  separator = /
  mailbox Drafts {{
    special_use = \Drafts
    auto = subscribe
  }}
  mailbox Sent {{
    special_use = \Sent
    auto = subscribe
  }}
  mailbox Trash {{
    special_use = \Trash
    auto = subscribe
  }}
  mailbox Junk {{
    special_use = \Junk
    auto = subscribe
  }}
  mailbox Archive {{
    special_use = \Archive
    auto = subscribe
  }}
}}
namespace {{
  type = public
  separator = /
  prefix = Public/
  location = maildir:{d}/public
  subscriptions = no
  list = yes
}}
"#
        );
        let config_path = dir.path().join("dovecot.conf");
        fs::write(&config_path, config).unwrap();

        // A folder in the public namespace that every user may read and write.
        let announce = dir.path().join("public/.Announce");
        for sub in ["cur", "new", "tmp"] {
            fs::create_dir_all(announce.join(sub)).unwrap();
        }
        fs::write(announce.join("dovecot-acl"), "anyone lrwstipek\n").unwrap();

        let child = Command::new("dovecot")
            .arg("-F")
            .arg("-c")
            .arg(&config_path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("start dovecot; is the dovecot-imapd package installed?");

        let deadline = Instant::now() + Duration::from_secs(10);
        while TcpStream::connect(("127.0.0.1", port)).is_err() {
            if Instant::now() > deadline {
                let log = fs::read_to_string(dir.path().join("dovecot.log")).unwrap_or_default();
                panic!("dovecot did not start:\n{log}");
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        Dovecot {
            dir,
            child,
            port,
            counter: AtomicU32::new(1),
        }
    }

    pub fn path(&self) -> &Path {
        self.dir.path()
    }

    /// The configuration of an account on this server. Any user name works.
    pub fn account(&self, user: &str) -> AccountConfig {
        serde_json::from_value(serde_json::json!({
            "id": user,
            "address": format!("{user}@example.org"),
            "display_name": "Robin Example",
            "host": "127.0.0.1",
            "port": self.port,
            "security": "insecure",
            "username": user,
            "auth": {"type": "password", "password": PASSWORD}
        }))
        .unwrap()
    }

    fn maildir(&self, user: &str, folder: &str) -> PathBuf {
        let root = self.dir.path().join("mail").join(user);
        if folder.eq_ignore_ascii_case("INBOX") {
            root
        } else {
            root.join(format!(".{}", folder.replace('/', ".")))
        }
    }

    /// Puts a message into a folder, as if it had just been delivered. Messages get UIDs in the
    /// order in which they are delivered. `maildir_flags` are Maildir flag letters, e.g. `"S"`
    /// for seen or `"T"` for deleted.
    pub fn deliver(&self, user: &str, folder: &str, message: &str, maildir_flags: &str) {
        let maildir = self.maildir(user, folder);
        for sub in ["cur", "new", "tmp"] {
            fs::create_dir_all(maildir.join(sub)).unwrap();
        }
        let n = self.counter.fetch_add(1, Ordering::SeqCst);
        let name = format!("{}.M{n}P1.test", 1_700_000_000 + n);
        let content = message.replace("\r\n", "\n").replace('\n', "\r\n");
        let path = if maildir_flags.is_empty() {
            maildir.join("new").join(name)
        } else {
            maildir
                .join("cur")
                .join(format!("{name}:2,{maildir_flags}"))
        };
        fs::write(path, content).unwrap();
    }

    /// Raw contents of all messages in a folder, read straight from disk.
    pub fn messages_on_disk(&self, user: &str, folder: &str) -> Vec<String> {
        let maildir = self.maildir(user, folder);
        let mut messages = Vec::new();
        for sub in ["cur", "new"] {
            let Ok(entries) = fs::read_dir(maildir.join(sub)) else {
                continue;
            };
            for entry in entries {
                messages.push(fs::read_to_string(entry.unwrap().path()).unwrap());
            }
        }
        messages.sort();
        messages
    }

    /// Whether a folder exists on disk for a user.
    pub fn folder_exists(&self, user: &str, folder: &str) -> bool {
        self.maildir(user, folder).is_dir()
    }
}

impl Drop for Dovecot {
    fn drop(&mut self) {
        // SIGTERM lets the master process stop its children.
        Command::new("kill")
            .arg(self.child.id().to_string())
            .status()
            .ok();
        self.child.wait().ok();
    }
}

/// A synthetic message.
pub fn message(from: &str, to: &str, subject: &str, body: &str) -> String {
    let id = subject
        .to_lowercase()
        .replace(|c: char| !c.is_ascii_alphanumeric(), "-");
    format!(
        "From: {from}\nTo: {to}\nSubject: {subject}\nDate: Mon, 02 Nov 2026 10:00:00 +0000\n\
         Message-ID: <{id}@example.net>\nMIME-Version: 1.0\n\
         Content-Type: text/plain; charset=utf-8\n\n{body}\n"
    )
}

/// One message frame of a fetch response.
#[derive(Debug)]
pub struct Frame {
    pub header: serde_json::Value,
    pub body: Vec<u8>,
}

/// Parses the frames of a fetch response. Returns the message frames and the final frame.
pub fn parse_frames(mut data: &[u8]) -> (Vec<Frame>, serde_json::Value) {
    let mut frames = Vec::new();
    loop {
        let newline = data.iter().position(|b| *b == b'\n').expect("frame line");
        let header: serde_json::Value = serde_json::from_slice(&data[..newline]).unwrap();
        data = &data[newline + 1..];
        if header.get("uid").is_none() {
            assert!(data.is_empty(), "data after the final frame");
            return (frames, header);
        }
        let size = header["size"].as_u64().unwrap() as usize;
        let body = data[..size].to_vec();
        assert_eq!(data[size], b'\n');
        data = &data[size + 1..];
        frames.push(Frame { header, body });
    }
}

/// A TCP relay in front of a server, which lets a test cut the connections passing through it.
pub struct Relay {
    pub port: u16,
    cut: tokio::sync::watch::Sender<u32>,
}

impl Relay {
    pub async fn start(upstream_port: u16) -> Relay {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (cut, _) = tokio::sync::watch::channel(0);
        let subscriptions = cut.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut inbound, _)) = listener.accept().await else {
                    return;
                };
                // A new subscription only sees cuts that happen from now on.
                let mut cut_rx = subscriptions.subscribe();
                tokio::spawn(async move {
                    let Ok(mut outbound) =
                        tokio::net::TcpStream::connect(("127.0.0.1", upstream_port)).await
                    else {
                        return;
                    };
                    tokio::select! {
                        _ = tokio::io::copy_bidirectional(&mut inbound, &mut outbound) => {}
                        _ = cut_rx.changed() => {}
                    }
                });
            }
        });
        Relay { port, cut }
    }

    /// Closes all connections that are currently open.
    pub fn cut(&self) {
        self.cut.send_modify(|n| *n += 1);
    }
}
