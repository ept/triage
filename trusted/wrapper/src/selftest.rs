//! `email-triage --self-test`: checks on this machine that the web view is confined.
//!
//! The restrictions in `view.rs` and `proxy.rs` rest on how the system's web view behaves,
//! which differs between systems and versions. The self-test shows a page that plays a web
//! interface turned hostile (`selftest.js`): it tries to reach a second server, the canary, in
//! every way a page has. The page is shown in a web view set up exactly like the one for the
//! app. Afterwards the canary must have seen nothing.
//!
//! `email-triage --self-test-control` runs the same page in a web view without any of the
//! restrictions. There the canary must be reached, which shows that the test can fail.
//!
//! Neither uses mail, containers or any network beyond this machine, and the output is safe to
//! share.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream, UdpSocket};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::Value;
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder};
use tao::window::WindowBuilder;
use triage_wrapper::proxy::Proxy;
use triage_wrapper::view;
use wry::WebViewBuilder;

type Log = Arc<Mutex<Vec<String>>>;

const PAGE: &str = "<!doctype html><meta charset=utf-8><title>Self-test</title>\
                    <p id=status>running</p><script src=/selftest.js></script>";
const SCRIPT: &str = include_str!("selftest.js");
/// What a same-origin worker of the page does: fetch the address it is given.
const WORKER: &str = "const to = decodeURIComponent(location.search.slice(4));\n\
                      fetch(to, { mode: 'no-cors' }).then(() => postMessage('resolved'), \
                      (e) => postMessage('rejected: ' + e));\n";
/// The attempts, in the order of `selftest.js`.
const CHANNELS: &[&str] = &[
    "fetch",
    "xhr",
    "img",
    "css",
    "script",
    "iframe",
    "beacon",
    "ws",
    "es",
    "form",
    "open",
    "ablank",
    "refresh",
    "audio",
    "video",
    "poster",
    "srcset",
    "svg",
    "object",
    "embed",
    "cssimport",
    "cssbg",
    "font",
    "import",
    "worker",
    "preconnect",
    "prefetch",
    "preload",
    "aping",
    "webtransport",
    "aself",
    "toplevel",
];
/// The attempts among them that replace the page if they are not stopped.
const LEAVING: &[&str] = &["aself", "toplevel"];
const HOSTS: &[(&str, &str)] = &[
    ("ip", "this machine, by number"),
    ("name", "this machine, by name"),
    ("ext", "another machine"),
];

struct Request {
    line: String,
    body: Vec<u8>,
}

fn read_request(stream: &TcpStream) -> Option<Request> {
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line).ok()?;
    let mut length = 0;
    loop {
        let mut field = String::new();
        if reader.read_line(&mut field).ok()? == 0 || field.trim_end().is_empty() {
            break;
        }
        if let Some((name, value)) = field.split_once(':')
            && name.eq_ignore_ascii_case("content-length")
        {
            length = value.trim().parse().unwrap_or(0);
        }
    }
    let mut body = vec![0; length.min(1 << 20)];
    reader.read_exact(&mut body).ok()?;
    Some(Request {
        line: line.trim_end().to_string(),
        body,
    })
}

fn respond(mut stream: &TcpStream, content_type: &str, body: &str) {
    write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\n\
         Cache-Control: no-store\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
    .ok();
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let hex = bytes
            .get(index + 1..index + 3)
            .and_then(|pair| u8::from_str_radix(std::str::from_utf8(pair).ok()?, 16).ok());
        match (bytes[index], hex) {
            (b'%', Some(byte)) => {
                decoded.push(byte);
                index += 3;
            }
            (byte, _) => {
                decoded.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&decoded).into_owned()
}

struct App {
    address: SocketAddr,
    requests: Log,
    /// When the page announced each attempt.
    marks: Arc<Mutex<Vec<(Instant, String)>>>,
    report: Arc<Mutex<Option<Value>>>,
    finished: Arc<AtomicBool>,
}

/// The stand-in for the app: serves the hostile page and takes its report.
fn start_app(canary_port: u16) -> std::io::Result<App> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let app = App {
        address: listener.local_addr()?,
        requests: Log::default(),
        marks: Arc::default(),
        report: Arc::default(),
        finished: Arc::default(),
    };
    let (requests, marks, report, finished) = (
        app.requests.clone(),
        app.marks.clone(),
        app.report.clone(),
        app.finished.clone(),
    );
    let script = SCRIPT
        .replace("__CANARY_PORT__", &canary_port.to_string())
        .replace("__GAP_MS__", "150");
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let Some(request) = read_request(&stream) else {
                continue;
            };
            requests.lock().unwrap().push(request.line.clone());
            let path = request.line.split(' ').nth(1).unwrap_or("");
            // Through a proxy that does not tunnel, the path arrives as it is; be lenient.
            let path = path.strip_prefix("http://").map_or(path, |rest| {
                rest.find('/').map_or("/", |index| &rest[index..])
            });
            if path == "/selftest.js" {
                respond(&stream, "text/javascript", &script);
            } else if path.starts_with("/worker.js") {
                respond(&stream, "text/javascript", WORKER);
            } else if let Some(id) = path.strip_prefix("/mark/") {
                marks.lock().unwrap().push((Instant::now(), id.to_string()));
                respond(&stream, "text/plain", "ok");
            } else if path == "/report" {
                *report.lock().unwrap() = serde_json::from_slice(&request.body).ok();
                respond(&stream, "text/plain", "ok");
            } else if path == "/finished" {
                finished.store(true, Ordering::SeqCst);
                respond(&stream, "text/plain", "ok");
            } else if let Some(rest) = path.strip_prefix("/refresh/") {
                let to = rest.split_once("?to=").map_or("", |(_, to)| to);
                let to = percent_decode(to).replace(['"', '<', '>'], "");
                let page = format!(
                    "<!doctype html><meta http-equiv=\"refresh\" content=\"0;url={to}\"><p>…</p>"
                );
                respond(&stream, "text/html", &page);
            } else if path == "/blank" {
                respond(&stream, "text/html", "<!doctype html><p>blank</p>");
            } else {
                respond(&stream, "text/html", PAGE);
            }
        }
    });
    Ok(app)
}

/// Something that arrived at the server that must not be reached.
#[derive(Clone)]
struct Arrival {
    at: Instant,
    /// The first line of the request, if one was sent over the connection.
    line: Option<String>,
    /// What arrived if no request did.
    kind: &'static str,
}

type Arrivals = Arc<Mutex<Vec<Arrival>>>;

/// The server that must not be reached. Remembers every connection from the moment it is
/// made (WebKit opens some over which it then sends nothing), the first line of whatever
/// arrives over it, and every datagram to the same port number.
fn start_canary() -> std::io::Result<(u16, Arrivals)> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    let seen = Arrivals::default();
    let log = seen.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let index = {
                let mut log = log.lock().unwrap();
                log.push(Arrival {
                    at: Instant::now(),
                    line: None,
                    kind: "a connection without a request",
                });
                log.len() - 1
            };
            let log = log.clone();
            std::thread::spawn(move || {
                if let Some(request) = read_request(&stream) {
                    log.lock().unwrap()[index].line = Some(request.line);
                    respond(&stream, "text/plain", "canary");
                }
            });
        }
    });
    // Best effort: if the same number is taken for datagrams, they go unobserved.
    if let Ok(socket) = UdpSocket::bind(("127.0.0.1", port)) {
        let log = seen.clone();
        std::thread::spawn(move || {
            let mut buffer = [0; 2048];
            while socket.recv_from(&mut buffer).is_ok() {
                log.lock().unwrap().push(Arrival {
                    at: Instant::now(),
                    line: None,
                    kind: "a datagram",
                });
            }
        });
    }
    Ok((port, seen))
}

/// Whether one of the lines is about the attempt `id`: its address is `/c/<id>` on this
/// machine, or anything on the host `<id>.canary.invalid`.
fn mentions<'a>(log: impl IntoIterator<Item = &'a String>, id: &str) -> bool {
    let path = format!("/c/{id}");
    let host = format!("{id}.canary.invalid");
    log.into_iter().any(|line| {
        // The host may be followed by a port number, as in a request for a tunnel.
        line.split([' ', '/'])
            .any(|word| word.split(':').next() == Some(host.as_str()))
            || line.split([' ', '?']).any(|word| word.ends_with(&path))
    })
}

/// The attempt that the page had announced last when something arrived at `at`.
fn attempt_at(marks: &[(Instant, String)], at: Instant) -> Option<&str> {
    marks
        .iter()
        .rev()
        .find(|(marked, _)| *marked <= at)
        .map(|(_, id)| id.as_str())
}

enum Step {
    /// The web view is confined (or could not be) and may load the test page.
    Confined(Result<(), String>),
    Done,
}

/// Runs the self-test and returns the exit code: 0 if the web view is confined. With
/// `confined` false, runs the control instead: 0 if the unrestricted web view got out.
pub fn run(confined: bool) -> i32 {
    let (canary_port, canary) = match start_canary() {
        Ok(canary) => canary,
        Err(error) => return failed_to_start(&error.to_string()),
    };
    let app = match start_app(canary_port) {
        Ok(app) => app,
        Err(error) => return failed_to_start(&error.to_string()),
    };
    let proxy = match Proxy::start(app.address) {
        Ok(proxy) => proxy,
        Err(error) => return failed_to_start(&error.to_string()),
    };
    let stopped = Log::default();
    if confined {
        println!("Self-test of the web view's confinement");
    } else {
        println!("Control for the self-test: a web view WITHOUT the restrictions");
    }
    println!("web view: {:?}", wry::webview_version());
    println!("(takes about half a minute; a window opens and closes by itself)\n");

    let event_loop = EventLoopBuilder::<Step>::with_user_event().build();
    let window = WindowBuilder::new()
        .with_title("Email Triage self-test")
        .build(&event_loop)
        .expect("a window can be created");
    let record = stopped.clone();
    // As for the app: the web view starts empty and loads the page once it is confined.
    let builder = if confined {
        let restricted = view::restrict(
            WebViewBuilder::new(),
            app.address,
            proxy.port(),
            move |what, url| record.lock().unwrap().push(format!("{what:?} {url}")),
        );
        match restricted {
            Ok(builder) => builder,
            Err(reason) => {
                println!("FAILED: the web view could not be confined. {reason}");
                return 1;
            }
        }
    } else {
        WebViewBuilder::new()
    };
    let webview = match crate::attach(builder, &window) {
        Ok(webview) => webview,
        Err(error) => return failed_to_start(&error.to_string()),
    };
    let wake = event_loop.create_proxy();
    if confined {
        let wake = wake.clone();
        view::block_other_addresses(&webview, app.address, move |result| {
            wake.send_event(Step::Confined(result)).ok();
        });
    } else {
        wake.send_event(Step::Confined(Ok(()))).ok();
    }

    let (finished, arrivals) = (app.finished.clone(), canary.clone());
    std::thread::spawn(move || {
        let start = Instant::now();
        // The page says when it has finished; if it got away, it cannot.
        let left = || {
            let lines: Vec<String> = arrivals
                .lock()
                .unwrap()
                .iter()
                .filter_map(|a| a.line.clone())
                .collect();
            LEAVING.iter().any(|channel| {
                HOSTS
                    .iter()
                    .any(|(host, _)| mentions(&lines, &format!("{channel}-{host}")))
            })
        };
        while !finished.load(Ordering::SeqCst)
            && !left()
            && start.elapsed() < Duration::from_secs(90)
        {
            std::thread::sleep(Duration::from_millis(100));
        }
        std::thread::sleep(Duration::from_secs(2));
        wake.send_event(Step::Done).ok();
    });

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match &event {
            Event::UserEvent(Step::Confined(Ok(()))) => {
                if let Err(error) = webview.load_url(&format!("http://{}/", app.address)) {
                    std::process::exit(failed_to_start(&error.to_string()));
                }
            }
            Event::UserEvent(Step::Confined(Err(reason))) => {
                println!("FAILED: the web view could not be confined. {reason}");
                std::process::exit(1);
            }
            _ => {}
        }
        let done = matches!(
            event,
            Event::UserEvent(Step::Done)
                | Event::WindowEvent {
                    event: WindowEvent::CloseRequested,
                    ..
                }
        );
        if !done {
            return;
        }
        let url = webview
            .url()
            .unwrap_or_else(|error| format!("(unknown: {error})"));
        let code = verdict(&app, &canary, &proxy, &stopped, &url);
        let code = if confined {
            code
        } else if canary.lock().unwrap().is_empty() {
            println!("\nThe control did NOT work: even without restrictions nothing got out.");
            println!("The self-test proves nothing on this machine.");
            1
        } else {
            println!("\nThe control worked: without the restrictions, the page got out.");
            println!("(The FAILED above is the expected result of the control.)");
            0
        };
        // The window and web view are owned by this closure and go away with the process.
        std::process::exit(code);
    })
}

fn failed_to_start(error: &str) -> i32 {
    println!("The self-test could not be started: {error}");
    2
}

fn verdict(app: &App, canary: &Arrivals, proxy: &Proxy, stopped: &Log, url: &str) -> i32 {
    let canary = canary.lock().unwrap().clone();
    let marks = app.marks.lock().unwrap().clone();
    let stopped = stopped.lock().unwrap().clone();
    let refused = proxy.refused();
    let report = app.report.lock().unwrap().clone();
    let mut problems: Vec<String> = Vec::new();

    let requests: Vec<String> = canary.iter().filter_map(|a| a.line.clone()).collect();
    // What arrived without a request, with the attempt during which it arrived.
    let silent: Vec<(&'static str, Option<&str>)> = canary
        .iter()
        .filter(|arrival| arrival.line.is_none())
        .map(|arrival| (arrival.kind, attempt_at(&marks, arrival.at)))
        .collect();

    println!("{:<12} {:<26} outcome", "attempt", "destination");
    let mut unseen = 0;
    for channel in CHANNELS {
        for (host, destination) in HOSTS {
            let id = format!("{channel}-{host}");
            let during = silent
                .iter()
                .find(|(_, attempt)| *attempt == Some(id.as_str()));
            let outcome = if mentions(&requests, &id) {
                problems.push(format!("{id} reached the other server"));
                "REACHED THE OTHER SERVER".to_string()
            } else if let Some((kind, _)) = during {
                problems.push(format!("{id}: {kind} reached the other server"));
                format!("REACHED THE OTHER SERVER with {kind}")
            } else if mentions(&refused, &id) {
                "refused by the proxy".to_string()
            } else if mentions(&stopped, &id) {
                "stopped by the web view's rules".to_string()
            } else {
                unseen += 1;
                "blocked before any request was made".to_string()
            };
            println!("{channel:<12} {destination:<26} {outcome}");
        }
    }

    println!();
    println!("what reached the other server: {}", canary.len());
    for line in requests.iter().take(20) {
        println!("  {line}");
    }
    if requests.len() > 20 {
        println!("  … and {} more requests", requests.len() - 20);
    }
    for (kind, attempt) in &silent {
        let known = attempt.is_some_and(|id| {
            CHANNELS.iter().any(|channel| {
                id.strip_prefix(channel)
                    .is_some_and(|rest| rest.starts_with('-'))
            })
        });
        match attempt {
            Some(id) if known => println!("  {kind}, during {id}"),
            _ => {
                println!("  {kind}, outside any attempt");
                problems.push(format!(
                    "{kind} reached the other server outside any attempt"
                ));
            }
        }
    }
    if !canary.is_empty() && problems.is_empty() {
        problems.push("the other server was reached".into());
    }
    println!(
        "the test page's own requests: {} arrived, {} of them through the proxy",
        app.requests.lock().unwrap().len(),
        proxy.passed_on()
    );
    println!("attempts that were blocked before any request was made: {unseen}");
    let at_home = url
        .strip_prefix(&format!("http://{}", app.address))
        .is_some_and(|rest| rest.is_empty() || rest.starts_with(['/', '?', '#']));
    println!("the window still shows the test page: {at_home}");
    if !at_home {
        problems.push(format!("the window navigated away, to {url}"));
    }
    match &report {
        None => problems.push("the test page did not report (did it run at all?)".into()),
        Some(report) => {
            println!(
                "media capture: {}",
                report["capture"].as_str().unwrap_or("?")
            );
            if report["capture"] == "GRANTED" {
                problems.push("the page was allowed to use the microphone".into());
            }
            if let Some(frames) = report["frames"].as_object() {
                for (frame, found) in frames {
                    let direct = [
                        "RTCPeerConnection",
                        "webkitRTCPeerConnection",
                        "WebTransport",
                    ]
                    .iter()
                    .any(|name| found[name].as_str().is_some_and(|kind| kind != "undefined"));
                    if direct || !found.is_object() {
                        problems.push(format!(
                            "interfaces that bypass proxies are available in frame {frame}: {found}"
                        ));
                    }
                }
                println!("frames checked for ways around the proxy: {}", frames.len());
            }
            println!(
                "web view identifies as: {}",
                report["userAgent"].as_str().unwrap_or("?")
            );
        }
    }

    println!();
    if problems.is_empty() {
        println!("PASSED: the page could not reach anything but its own server.");
        0
    } else {
        println!("FAILED:");
        for problem in &problems {
            println!("  - {problem}");
        }
        println!("If this is the self-test and not its control, do not use the wrapper app");
        println!("on this machine until this is resolved.");
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lines(lines: &[&str]) -> Vec<String> {
        lines.iter().map(|line| line.to_string()).collect()
    }

    #[test]
    fn a_line_is_put_down_to_the_attempt_it_names_and_no_other() {
        let log = lines(&[
            "GET /c/cssimport-ip HTTP/1.1",
            "CONNECT ablank-ext.canary.invalid:80 HTTP/1.1",
            "Navigation http://aself-ext.canary.invalid/c/aself-ext",
            "GET /refresh/name?to=http%3A%2F%2Flocalhost%3A1%2Fc%2Frefresh-name HTTP/1.1",
        ]);
        for id in ["cssimport-ip", "ablank-ext", "aself-ext"] {
            assert!(mentions(&log, id), "{id}");
        }
        // Names that are the end of another name, and an address that is only quoted.
        for id in [
            "import-ip",
            "blank-ext",
            "self-ext",
            "css-ip",
            "refresh-name",
            "fetch-ip",
        ] {
            assert!(!mentions(&log, id), "{id}");
        }
    }

    #[test]
    fn a_silent_arrival_belongs_to_the_attempt_announced_before_it() {
        let start = Instant::now();
        let at = |ms| start + Duration::from_millis(ms);
        let marks = vec![
            (at(10), "fetch-ip".to_string()),
            (at(20), "ablank-ip".to_string()),
        ];
        assert_eq!(attempt_at(&marks, at(5)), None);
        assert_eq!(attempt_at(&marks, at(10)), Some("fetch-ip"));
        assert_eq!(attempt_at(&marks, at(19)), Some("fetch-ip"));
        assert_eq!(attempt_at(&marks, at(500)), Some("ablank-ip"));
    }

    #[test]
    fn a_silent_connection_alone_fails_the_test() {
        let app = start_app(1).unwrap();
        let proxy = Proxy::start(app.address).unwrap();
        *app.report.lock().unwrap() = Some(serde_json::json!({
            "capture": "not available",
            "userAgent": "test",
            "frames": {"main": {"RTCPeerConnection": "undefined"}},
        }));
        let start = Instant::now();
        app.marks
            .lock()
            .unwrap()
            .push((start, "ablank-ip".to_string()));
        let (canary, stopped) = (Arrivals::default(), Log::default());
        let url = format!("http://{}/", app.address);
        assert_eq!(verdict(&app, &canary, &proxy, &stopped, &url), 0);

        canary.lock().unwrap().push(Arrival {
            at: start + Duration::from_millis(3),
            line: None,
            kind: "a connection without a request",
        });
        assert_eq!(verdict(&app, &canary, &proxy, &stopped, &url), 1);
    }

    #[test]
    fn the_canary_notices_connections_over_which_nothing_is_sent() {
        let (port, seen) = start_canary().unwrap();
        let wait_for = |count: usize, with_line: usize| {
            let start = Instant::now();
            loop {
                let arrivals = seen.lock().unwrap().clone();
                let lines = arrivals.iter().filter(|a| a.line.is_some()).count();
                if arrivals.len() == count && lines == with_line {
                    return arrivals;
                }
                assert!(
                    start.elapsed() < Duration::from_secs(5),
                    "{} arrivals",
                    arrivals.len()
                );
                std::thread::sleep(Duration::from_millis(10));
            }
        };

        // Two silent connections at once: neither hides the other, and neither has to end.
        let _first = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let _second = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let arrivals = wait_for(2, 0);
        assert!(
            arrivals
                .iter()
                .all(|a| a.kind == "a connection without a request")
        );

        // A request is still read while they are open.
        let mut third = TcpStream::connect(("127.0.0.1", port)).unwrap();
        third
            .write_all(b"GET /c/fetch-ip HTTP/1.1\r\nHost: x\r\n\r\n")
            .unwrap();
        let arrivals = wait_for(3, 1);
        assert!(mentions(
            arrivals.iter().filter_map(|a| a.line.as_ref()),
            "fetch-ip"
        ));

        // A datagram to the same port number.
        let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        socket.send_to(b"x", ("127.0.0.1", port)).unwrap();
        let arrivals = wait_for(4, 1);
        assert_eq!(arrivals[3].kind, "a datagram");
    }
}
