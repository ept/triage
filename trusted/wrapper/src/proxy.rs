//! The only way out of the web view: a proxy that forwards requests to the app, and refuses
//! everything else.
//!
//! The web view is configured to send all its traffic through this proxy, which listens on
//! the loopback interface. Whatever a page asks for (an image, a script, a `fetch`, a
//! navigation, a WebSocket) arrives here as a request that names its destination. Requests for
//! the app's own origin are passed on to that address and to no other; all others are answered
//! with 403 and never leave this process. This holds whatever the app's own code does, which
//! is the point: the app runs in a sandbox without network access, and the web view that
//! shows it must not become its way out.
//!
//! Each connection carries one request (`Connection: close` is forced), so that every request
//! is checked. Bytes are copied as they arrive, which keeps streamed responses streaming.
//!
//! Some web views (WKWebView on macOS) do not send plain requests to a proxy but ask it for a
//! tunnel (`CONNECT host:port`) and speak to the destination through that. A tunnel is opened
//! to the app's address and to nothing else; whatever is sent through it can only reach the
//! app.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{Ipv4Addr, Shutdown, SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// Longest request head (request line and header fields) that is accepted.
const MAX_HEAD: usize = 64 * 1024;
/// How many refused destinations are remembered for [`Proxy::refused`].
const MAX_REMEMBERED: usize = 200;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const HEAD_TIMEOUT: Duration = Duration::from_secs(30);

/// A running proxy. It runs until the process ends.
pub struct Proxy {
    port: u16,
    refused: Arc<Mutex<Vec<String>>>,
    passed_on: Arc<AtomicUsize>,
}

impl Proxy {
    /// Starts a proxy on a free port of the loopback interface. `app` is the one address to
    /// which requests are forwarded: the port on which the gateway serves the app's interface.
    pub fn start(app: SocketAddr) -> io::Result<Proxy> {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
        let port = listener.local_addr()?.port();
        let refused: Arc<Mutex<Vec<String>>> = Arc::default();
        let passed_on: Arc<AtomicUsize> = Arc::default();
        let (log, count) = (refused.clone(), passed_on.clone());
        std::thread::Builder::new()
            .name("proxy".into())
            .spawn(move || {
                for client in listener.incoming().flatten() {
                    let (log, count) = (log.clone(), count.clone());
                    std::thread::spawn(move || {
                        // A failure concerns one request of one page; there is nobody to tell.
                        serve(client, app, &log, &count).ok();
                    });
                }
            })?;
        Ok(Proxy {
            port,
            refused,
            passed_on,
        })
    }

    /// The address to give to the web view as its proxy.
    pub fn url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    /// How many requests and tunnels were passed on to the app so far.
    pub fn passed_on(&self) -> usize {
        self.passed_on.load(Ordering::SeqCst)
    }

    /// The destinations that were refused so far, oldest first (method and target as the web
    /// view sent them, shortened). For diagnosis and for tests.
    pub fn refused(&self) -> Vec<String> {
        self.refused.lock().expect("not poisoned").clone()
    }
}

/// What a request asks for, once its first line has been understood.
#[derive(Debug, PartialEq, Eq)]
enum Target {
    /// A request for the app. The path (with query) is what is passed on.
    App { method: String, path: String },
    /// A tunnel to the app.
    Tunnel,
    /// Anything else.
    Refused,
}

/// Decides where a request may go, from its first line. Only plain HTTP requests whose
/// destination is written exactly as the app's address are for the app: not another name for
/// the same machine, not another port, not a tunnel.
fn classify(request_line: &str, app: SocketAddr) -> Target {
    let mut parts = request_line.split(' ');
    let (Some(method), Some(target), Some(version), None) =
        (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return Target::Refused;
    };
    let method_ok =
        !method.is_empty() && method.len() <= 16 && method.bytes().all(|b| b.is_ascii_uppercase());
    if !method_ok || !matches!(version, "HTTP/1.1" | "HTTP/1.0") {
        return Target::Refused;
    }
    if method == "CONNECT" {
        // A tunnel names its destination as host:port, without a scheme.
        return if target == app.to_string() {
            Target::Tunnel
        } else {
            Target::Refused
        };
    }
    let Some(rest) = target.strip_prefix("http://") else {
        return Target::Refused;
    };
    let (authority, path) = match rest.find(['/', '?', '#']) {
        Some(index) => rest.split_at(index),
        None => (rest, "/"),
    };
    // No user names, no other spellings of the host, and the port must be stated.
    if authority != app.to_string() {
        return Target::Refused;
    }
    let path = if path.starts_with('/') {
        path.to_string()
    } else {
        format!("/{path}")
    };
    if path.bytes().any(|b| b <= b' ' || b == 0x7f) {
        return Target::Refused;
    }
    Target::App {
        method: method.to_string(),
        path,
    }
}

/// Header fields that concern the hop between the web view and this proxy only.
fn is_hop_header(name: &str) -> bool {
    [
        "connection",
        "proxy-connection",
        "proxy-authorization",
        "keep-alive",
    ]
    .iter()
    .any(|hop| name.eq_ignore_ascii_case(hop))
}

fn refuse(mut client: &TcpStream, log: &Mutex<Vec<String>>, what: &str) -> io::Result<()> {
    {
        let mut log = log.lock().expect("not poisoned");
        if log.len() < MAX_REMEMBERED {
            log.push(what.chars().take(200).collect());
        }
    }
    let body = "This window shows the Email Triage app only; other addresses are not loaded.\n";
    write!(
        client,
        "HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain; charset=utf-8\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )?;
    client.shutdown(Shutdown::Write)
}

fn serve(
    client: TcpStream,
    app: SocketAddr,
    log: &Mutex<Vec<String>>,
    passed_on: &AtomicUsize,
) -> io::Result<()> {
    client.set_read_timeout(Some(HEAD_TIMEOUT))?;
    let mut reader = BufReader::new(client.try_clone()?);

    // The head: the request line, then header fields up to an empty line.
    let mut head: Vec<String> = Vec::new();
    let mut size = 0;
    loop {
        let mut line = Vec::new();
        let read = reader
            .by_ref()
            .take((MAX_HEAD - size + 1) as u64)
            .read_until(b'\n', &mut line)?;
        size += read;
        if read == 0 || size > MAX_HEAD || !line.ends_with(b"\n") {
            return refuse(&client, log, "(a request that is too long or cut short)");
        }
        let Ok(line) = String::from_utf8(line) else {
            return refuse(&client, log, "(a request that is not text)");
        };
        let line = line.trim_end_matches(['\r', '\n']);
        if line.is_empty() && !head.is_empty() {
            break;
        }
        head.push(line.to_string());
    }

    // What is sent to the app first: the request with the app as its direct destination, or
    // nothing for a tunnel. And what is sent to the web view first: for a tunnel, the news
    // that it is open.
    let (request, greeting) = match classify(&head[0], app) {
        Target::Refused => return refuse(&client, log, &head[0]),
        Target::Tunnel => (String::new(), "HTTP/1.1 200 Connection established\r\n\r\n"),
        Target::App { method, path } => {
            let mut request = format!("{method} {path} HTTP/1.1\r\n");
            for field in &head[1..] {
                let name = field.split(':').next().unwrap_or("");
                if !is_hop_header(name) {
                    request.push_str(field);
                    request.push_str("\r\n");
                }
            }
            request.push_str("Connection: close\r\n\r\n");
            (request, "")
        }
    };

    let mut upstream = match TcpStream::connect_timeout(&app, CONNECT_TIMEOUT) {
        Ok(upstream) => upstream,
        Err(_) => {
            let mut client = &client;
            write!(
                client,
                "HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )?;
            return Ok(());
        }
    };
    passed_on.fetch_add(1, Ordering::SeqCst);
    upstream.write_all(request.as_bytes())?;
    (&client).write_all(greeting.as_bytes())?;
    client.set_read_timeout(None)?;

    // The rest of the request (its body) goes up, the response comes down, both as they
    // arrive. Both directions only ever connect the web view and the app.
    let mut up = upstream.try_clone()?;
    let body = std::thread::spawn(move || {
        io::copy(&mut reader, &mut up).ok();
        up.shutdown(Shutdown::Write).ok();
    });
    let mut down = client.try_clone()?;
    io::copy(&mut upstream, &mut down).ok();
    down.shutdown(Shutdown::Both).ok();
    upstream.shutdown(Shutdown::Both).ok();
    body.join().ok();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn app() -> SocketAddr {
        "127.0.0.1:8102".parse().unwrap()
    }

    fn to_app(method: &str, path: &str) -> Target {
        Target::App {
            method: method.into(),
            path: path.into(),
        }
    }

    #[test]
    fn requests_for_the_app_are_recognised() {
        let line = "GET http://127.0.0.1:8102/ HTTP/1.1";
        assert_eq!(classify(line, app()), to_app("GET", "/"));
        let line = "POST http://127.0.0.1:8102/api/chat?x=1&y=http://evil.example/ HTTP/1.1";
        assert_eq!(
            classify(line, app()),
            to_app("POST", "/api/chat?x=1&y=http://evil.example/")
        );
        let line = "GET http://127.0.0.1:8102 HTTP/1.0";
        assert_eq!(classify(line, app()), to_app("GET", "/"));
        let line = "GET http://127.0.0.1:8102?session=abc HTTP/1.1";
        assert_eq!(classify(line, app()), to_app("GET", "/?session=abc"));
    }

    #[test]
    fn a_tunnel_is_opened_to_the_app_only() {
        let line = "CONNECT 127.0.0.1:8102 HTTP/1.1";
        assert_eq!(classify(line, app()), Target::Tunnel);

        let app = echo_server();
        let proxy = Proxy::start(app).unwrap();
        let mut stream = TcpStream::connect(("127.0.0.1", proxy.port())).unwrap();
        write!(stream, "CONNECT {app} HTTP/1.1\r\nHost: {app}\r\n\r\n").unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        assert_eq!(line, "HTTP/1.1 200 Connection established\r\n");
        reader.read_line(&mut line).unwrap();
        // Through the tunnel, the web view speaks to the app as if directly.
        write!(stream, "GET /through HTTP/1.1\r\nHost: {app}\r\n\r\n").unwrap();
        let mut answer = String::new();
        reader.read_to_string(&mut answer).unwrap();
        assert!(answer.starts_with("HTTP/1.1 200 OK\r\n"), "{answer}");
        assert!(answer.contains("GET /through HTTP/1.1"));
        assert_eq!(proxy.passed_on(), 1);
    }

    #[test]
    fn everything_else_is_refused() {
        for line in [
            "GET http://example.org/ HTTP/1.1",
            "GET http://127.0.0.1:8103/ HTTP/1.1",
            "GET http://127.0.0.1/ HTTP/1.1",
            "GET http://127.0.0.1:81020/ HTTP/1.1",
            "GET http://localhost:8102/ HTTP/1.1",
            "GET http://127.1:8102/ HTTP/1.1",
            "GET http://2130706433:8102/ HTTP/1.1",
            "GET http://[::1]:8102/ HTTP/1.1",
            "GET http://127.0.0.1:8102@evil.example/ HTTP/1.1",
            "GET http://evil.example@127.0.0.1:8102/ HTTP/1.1",
            "GET http://127.0.0.1:8102.evil.example/ HTTP/1.1",
            "GET http://127.0.0.1:8102\\@evil.example/ HTTP/1.1",
            "GET https://127.0.0.1:8102/ HTTP/1.1",
            "GET HTTP://127.0.0.1:8102/ HTTP/1.1",
            "GET ftp://127.0.0.1:8102/ HTTP/1.1",
            "GET ws://127.0.0.1:8102/ HTTP/1.1",
            "GET / HTTP/1.1",
            "GET //evil.example/ HTTP/1.1",
            "CONNECT 127.0.0.1:8103 HTTP/1.1",
            "CONNECT localhost:8102 HTTP/1.1",
            "CONNECT 127.0.0.1:8102/ HTTP/1.1",
            "CONNECT evil.example:443 HTTP/1.1",
            "CONNECT http://127.0.0.1:8102/ HTTP/1.1",
            "get http://127.0.0.1:8102/ HTTP/1.1",
            "GET http://127.0.0.1:8102/ HTTP/2.0",
            "GET http://127.0.0.1:8102/ HTTP/1.1 extra",
            "GET  http://127.0.0.1:8102/ HTTP/1.1",
            "GET http://127.0.0.1:8102/a\tb HTTP/1.1",
            "GET http://127.0.0.1:8102/",
            "",
        ] {
            assert_eq!(classify(line, app()), Target::Refused, "{line:?}");
        }
    }

    /// A stand-in for the app: answers every request with a description of what it received.
    fn echo_server() -> SocketAddr {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        std::thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let mut reader = BufReader::new(stream);
                let mut head = String::new();
                let mut length = 0;
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap() == 0 || line == "\r\n" {
                        break;
                    }
                    if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                        length = value.trim().parse().unwrap();
                    }
                    head.push_str(&line);
                }
                let mut body = vec![0; length];
                reader.read_exact(&mut body).unwrap();
                let answer = format!("{head}\n{}", String::from_utf8_lossy(&body));
                write!(
                    reader.get_mut(),
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{answer}",
                    answer.len()
                )
                .unwrap();
            }
        });
        address
    }

    /// A server that must never be reached. Counts the connections it gets.
    fn canary() -> (SocketAddr, Arc<Mutex<u32>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let hits: Arc<Mutex<u32>> = Arc::default();
        let counter = hits.clone();
        std::thread::spawn(move || {
            for _ in listener.incoming() {
                *counter.lock().unwrap() += 1;
            }
        });
        (address, hits)
    }

    fn exchange(proxy: &Proxy, request: &str) -> String {
        let mut stream = TcpStream::connect(("127.0.0.1", proxy.port())).unwrap();
        stream.write_all(request.as_bytes()).unwrap();
        stream.shutdown(Shutdown::Write).unwrap();
        let mut answer = String::new();
        stream.read_to_string(&mut answer).unwrap();
        answer
    }

    #[test]
    fn a_request_for_the_app_is_passed_on_without_its_proxy_fields() {
        let app = echo_server();
        let proxy = Proxy::start(app).unwrap();
        let answer = exchange(
            &proxy,
            &format!(
                "POST http://{app}/api/x?y=1 HTTP/1.1\r\nHost: {app}\r\nCookie: s=1\r\n\
                 Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\n\
                 Content-Length: 5\r\n\r\nhello"
            ),
        );
        assert!(answer.starts_with("HTTP/1.1 200 OK\r\n"), "{answer}");
        let received = answer.split("\r\n\r\n").nth(1).unwrap();
        assert_eq!(
            received,
            format!(
                "POST /api/x?y=1 HTTP/1.1\r\nHost: {app}\r\nCookie: s=1\r\n\
                 Content-Length: 5\r\nConnection: close\r\n\nhello"
            )
        );
        assert!(proxy.refused().is_empty());
    }

    #[test]
    fn nothing_else_is_ever_contacted() {
        let app = echo_server();
        let (other, hits) = canary();
        let proxy = Proxy::start(app).unwrap();
        for request in [
            format!("GET http://{other}/leak?data=secret HTTP/1.1\r\nHost: {other}\r\n\r\n"),
            format!("CONNECT {other} HTTP/1.1\r\nHost: {other}\r\n\r\n"),
            // The right Host field does not make up for the wrong destination ...
            format!("GET http://{other}/ HTTP/1.1\r\nHost: {app}\r\n\r\n"),
            // ... and a request that names no destination is not guessed at.
            format!("GET /leak HTTP/1.1\r\nHost: {other}\r\n\r\n"),
            "\r\n\r\n".to_string(),
            format!(
                "GET http://{other}/ HTTP/1.1\r\nX: {}\r\n\r\n",
                "y".repeat(MAX_HEAD)
            ),
            "GET http://example.invalid/ HTTP/1.1".to_string(),
        ] {
            let answer = exchange(&proxy, &request);
            assert!(
                answer.starts_with("HTTP/1.1 403 Forbidden\r\n"),
                "{request:?}: {answer}"
            );
        }
        // Once a connection has been passed on to the app, whatever follows on it still only
        // reaches the app: there is one request per connection.
        let answer = exchange(
            &proxy,
            &format!(
                "GET http://{app}/one HTTP/1.1\r\nHost: {app}\r\n\r\n\
                 GET http://{other}/two HTTP/1.1\r\nHost: {other}\r\n\r\n"
            ),
        );
        assert!(answer.starts_with("HTTP/1.1 200 OK\r\n"));
        assert_eq!(answer.matches("HTTP/1.1 200 OK").count(), 1);

        std::thread::sleep(Duration::from_millis(200));
        assert_eq!(
            *hits.lock().unwrap(),
            0,
            "the other server was never contacted"
        );
        assert_eq!(proxy.refused().len(), 7);
        assert!(proxy.refused()[0].contains("/leak?data=secret"));
    }

    #[test]
    fn a_streamed_response_arrives_piece_by_piece() {
        // An app that sends the start of a response, and the rest only when told to.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let app = listener.local_addr().unwrap();
        let (go, wait) = std::sync::mpsc::channel::<()>();
        std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut line = String::new();
            while reader.read_line(&mut line).unwrap() > 2 {
                line.clear();
            }
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\nfirst\n")
                .unwrap();
            wait.recv().unwrap();
            stream.write_all(b"second\n").unwrap();
        });
        let proxy = Proxy::start(app).unwrap();
        let mut stream = TcpStream::connect(("127.0.0.1", proxy.port())).unwrap();
        write!(
            stream,
            "GET http://{app}/events HTTP/1.1\r\nHost: {app}\r\n\r\n"
        )
        .unwrap();
        let mut reader = BufReader::new(stream);
        let mut lines = Vec::new();
        for _ in 0..4 {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            lines.push(line);
        }
        assert_eq!(
            lines[3], "first\n",
            "it arrives before the response is complete"
        );
        go.send(()).unwrap();
        let mut rest = String::new();
        reader.read_to_string(&mut rest).unwrap();
        assert_eq!(rest, "second\n");
    }

    #[test]
    fn an_app_that_is_not_running_gives_an_error_page() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let app = listener.local_addr().unwrap();
        drop(listener);
        let proxy = Proxy::start(app).unwrap();
        let answer = exchange(&proxy, &format!("GET http://{app}/ HTTP/1.1\r\n\r\n"));
        assert!(
            answer.starts_with("HTTP/1.1 502 Bad Gateway\r\n"),
            "{answer}"
        );
    }
}
