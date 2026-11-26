//! A deliberately tiny IMAP client for the two exchanges that the IMAP library cannot do:
//! upgrading a connection with STARTTLS while making sure that nothing was injected before the
//! handshake, and asking for the server's namespaces (whose response the library cannot parse).
//!
//! It only understands responses that fit on one line. If a server answers with a literal, the
//! exchange fails, which makes the caller refuse the connection.

use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};

use crate::config::Secret;

const MAX_LINE: usize = 16 * 1024;
const MAX_LINES: usize = 200;

#[derive(Debug, PartialEq, Eq)]
pub enum RawError {
    /// The connection failed or was closed.
    Io(std::io::ErrorKind),
    /// The server sent something this client does not understand.
    Protocol(&'static str),
    /// The server answered NO or BAD.
    Refused(String),
}

impl From<std::io::Error> for RawError {
    fn from(error: std::io::Error) -> Self {
        RawError::Io(error.kind())
    }
}

pub struct RawImap<S> {
    stream: BufReader<S>,
    next_tag: u32,
}

impl<S: AsyncRead + AsyncWrite + Unpin> RawImap<S> {
    pub fn new(stream: S) -> Self {
        RawImap {
            stream: BufReader::new(stream),
            next_tag: 1,
        }
    }

    /// Returns the underlying stream, provided that the server has not sent anything that was
    /// not consumed. (Data sent ahead of a TLS handshake would otherwise be mistaken for data
    /// that arrived through TLS.)
    pub fn into_inner(self) -> Result<S, RawError> {
        if !self.stream.buffer().is_empty() {
            return Err(RawError::Protocol("unexpected data from the server"));
        }
        Ok(self.stream.into_inner())
    }

    async fn read_line(&mut self) -> Result<String, RawError> {
        let mut line = Vec::new();
        loop {
            let available = self.stream.fill_buf().await?;
            if available.is_empty() {
                return Err(RawError::Io(std::io::ErrorKind::UnexpectedEof));
            }
            let (taken, done) = match available.iter().position(|b| *b == b'\n') {
                Some(index) => (index + 1, true),
                None => (available.len(), false),
            };
            line.extend_from_slice(&available[..taken]);
            self.stream.consume(taken);
            if line.len() > MAX_LINE {
                return Err(RawError::Protocol("response line too long"));
            }
            if done {
                break;
            }
        }
        let line =
            String::from_utf8(line).map_err(|_| RawError::Protocol("response is not UTF-8"))?;
        let line = line.trim_end_matches(['\r', '\n']).to_string();
        if line.ends_with('}') && line.contains('{') {
            return Err(RawError::Protocol("response contains a literal"));
        }
        Ok(line)
    }

    /// Reads the server's greeting.
    pub async fn greeting(&mut self) -> Result<(), RawError> {
        let line = self.read_line().await?;
        let upper = line.to_ascii_uppercase();
        if upper.starts_with("* OK") || upper.starts_with("* PREAUTH") {
            Ok(())
        } else {
            Err(RawError::Protocol("unexpected greeting"))
        }
    }

    async fn send(&mut self, line: &str) -> Result<(), RawError> {
        debug_assert!(!line.contains(['\r', '\n']));
        self.stream.get_mut().write_all(line.as_bytes()).await?;
        self.stream.get_mut().write_all(b"\r\n").await?;
        self.stream.get_mut().flush().await?;
        Ok(())
    }

    /// Sends a command and returns the untagged response lines. `continuations` are sent, one
    /// at a time, whenever the server asks for more data; once they are used up an empty line is
    /// sent instead.
    async fn exchange(
        &mut self,
        command: &str,
        mut continuations: Vec<String>,
    ) -> Result<Vec<String>, RawError> {
        if command.contains(['\r', '\n']) || continuations.iter().any(|c| c.contains(['\r', '\n']))
        {
            return Err(RawError::Protocol("line break in a command"));
        }
        let tag = format!("G{}", self.next_tag);
        self.next_tag += 1;
        self.send(&format!("{tag} {command}")).await?;
        continuations.reverse();
        let mut untagged = Vec::new();
        for _ in 0..MAX_LINES {
            let line = self.read_line().await?;
            if let Some(rest) = line.strip_prefix(&format!("{tag} ")) {
                return if rest.get(..2).is_some_and(|s| s.eq_ignore_ascii_case("OK")) {
                    Ok(untagged)
                } else {
                    let text: String = rest.chars().filter(|c| !c.is_control()).take(200).collect();
                    Err(RawError::Refused(text))
                };
            } else if line.starts_with('+') {
                let reply = continuations.pop().unwrap_or_default();
                self.send(&reply).await?;
            } else if line.starts_with("* ") {
                untagged.push(line);
            } else {
                return Err(RawError::Protocol("unexpected response"));
            }
        }
        Err(RawError::Protocol("too many response lines"))
    }

    pub async fn command(&mut self, command: &str) -> Result<Vec<String>, RawError> {
        self.exchange(command, Vec::new()).await
    }

    pub async fn login(&mut self, username: &str, password: &Secret) -> Result<(), RawError> {
        let quote = |s: &str| format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""));
        self.exchange(
            &format!("LOGIN {} {}", quote(username), quote(password.expose())),
            Vec::new(),
        )
        .await
        .map(|_| ())
    }

    pub async fn authenticate_xoauth2(
        &mut self,
        username: &str,
        token: &Secret,
    ) -> Result<(), RawError> {
        use base64::Engine;
        let initial = format!("user={username}\x01auth=Bearer {}\x01\x01", token.expose());
        let encoded = base64::engine::general_purpose::STANDARD.encode(initial);
        self.exchange("AUTHENTICATE XOAUTH2", vec![encoded])
            .await
            .map(|_| ())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, DuplexStream, duplex};

    /// Plays the server side: for each step, expects a line from the client and answers.
    async fn server(mut stream: DuplexStream, greeting: &str, steps: Vec<(&str, &str)>) {
        stream.write_all(greeting.as_bytes()).await.unwrap();
        let mut reader = BufReader::new(&mut stream);
        for (expected, answer) in steps {
            let mut line = String::new();
            reader.read_line(&mut line).await.unwrap();
            assert_eq!(line.trim_end(), expected);
            reader.get_mut().write_all(answer.as_bytes()).await.unwrap();
        }
        let mut rest = Vec::new();
        reader.read_to_end(&mut rest).await.unwrap();
    }

    #[tokio::test]
    async fn login_and_namespace() {
        let (client, remote) = duplex(4096);
        let task = tokio::spawn(server(
            remote,
            "* OK ready\r\n",
            vec![
                (
                    "G1 LOGIN \"us\\\"er\" \"pa\\\\ss\"",
                    "* CAPABILITY IMAP4rev1\r\nG1 OK done\r\n",
                ),
                (
                    "G2 NAMESPACE",
                    "* NAMESPACE ((\"\" \"/\")) NIL NIL\r\nG2 OK done\r\n",
                ),
            ],
        ));
        let mut imap = RawImap::new(client);
        imap.greeting().await.unwrap();
        imap.login("us\"er", &Secret::new("pa\\ss")).await.unwrap();
        let lines = imap.command("NAMESPACE").await.unwrap();
        assert_eq!(lines, vec!["* NAMESPACE ((\"\" \"/\")) NIL NIL"]);
        drop(imap);
        task.await.unwrap();
    }

    #[tokio::test]
    async fn xoauth2_success_and_failure() {
        let (client, remote) = duplex(4096);
        let task = tokio::spawn(server(
            remote,
            "* OK ready\r\n",
            vec![
                ("G1 AUTHENTICATE XOAUTH2", "+ \r\n"),
                ("dXNlcj11AWF1dGg9QmVhcmVyIHQBAQ==", "G1 OK done\r\n"),
                ("G2 AUTHENTICATE XOAUTH2", "+ \r\n"),
                (
                    "dXNlcj11AWF1dGg9QmVhcmVyIHQBAQ==",
                    "+ eyJzdGF0dXMiOiI0MDEifQ==\r\n",
                ),
                ("", "G2 NO failed\r\n"),
            ],
        ));
        let mut imap = RawImap::new(client);
        imap.greeting().await.unwrap();
        imap.authenticate_xoauth2("u", &Secret::new("t"))
            .await
            .unwrap();
        assert_eq!(
            imap.authenticate_xoauth2("u", &Secret::new("t")).await,
            Err(RawError::Refused("NO failed".into()))
        );
        drop(imap);
        task.await.unwrap();
    }

    #[tokio::test]
    async fn refuses_what_it_does_not_understand() {
        for (greeting, answer, expected) in [
            (
                "* BYE go away\r\n",
                "",
                RawError::Protocol("unexpected greeting"),
            ),
            (
                "* OK\r\n",
                "* X {5}\r\n",
                RawError::Protocol("response contains a literal"),
            ),
            (
                "* OK\r\n",
                "garbage\r\n",
                RawError::Protocol("unexpected response"),
            ),
            (
                "* OK\r\n",
                "G1 BAD nope\r\n",
                RawError::Refused("BAD nope".into()),
            ),
            (
                "* OK\r\n",
                "",
                RawError::Io(std::io::ErrorKind::UnexpectedEof),
            ),
        ] {
            let (client, mut remote) = duplex(4096);
            remote.write_all(greeting.as_bytes()).await.unwrap();
            remote.write_all(answer.as_bytes()).await.unwrap();
            remote.shutdown().await.unwrap();
            let mut imap = RawImap::new(client);
            let result = match imap.greeting().await {
                Ok(()) => imap.command("NAMESPACE").await.map(|_| ()),
                Err(e) => Err(e),
            };
            assert_eq!(result, Err(expected), "{greeting:?} {answer:?}");
        }
    }

    #[tokio::test]
    async fn rejects_line_breaks_and_overlong_lines() {
        let (client, _remote) = duplex(4096);
        let mut imap = RawImap::new(client);
        assert_eq!(
            imap.command("NOOP\r\nG9 LOGOUT").await,
            Err(RawError::Protocol("line break in a command"))
        );
        assert!(imap.login("a\r\nb", &Secret::new("x")).await.is_err());

        let (client, mut remote) = duplex(64 * 1024);
        remote.write_all(&vec![b'x'; MAX_LINE + 10]).await.unwrap();
        let mut imap = RawImap::new(client);
        assert_eq!(
            imap.greeting().await,
            Err(RawError::Protocol("response line too long"))
        );
    }

    #[tokio::test]
    async fn data_ahead_of_a_handshake_is_detected() {
        let (client, mut remote) = duplex(4096);
        remote
            .write_all(b"* OK ready\r\nG1 OK begin TLS\r\n* injected\r\n")
            .await
            .unwrap();
        let mut imap = RawImap::new(client);
        imap.greeting().await.unwrap();
        imap.command("STARTTLS").await.unwrap();
        assert!(matches!(
            imap.into_inner(),
            Err(RawError::Protocol("unexpected data from the server"))
        ));
    }
}
