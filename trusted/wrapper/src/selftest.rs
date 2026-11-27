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
use std::net::{SocketAddr, TcpListener, TcpStream};
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
const CHANNELS: &[&str] = &[
    "fetch", "xhr", "img", "css", "script", "iframe", "beacon", "ws", "es", "form", "open",
    "ablank", "refresh", "audio", "toplevel",
];
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
    report: Arc<Mutex<Option<Value>>>,
    finished: Arc<AtomicBool>,
}

/// The stand-in for the app: serves the hostile page and takes its report.
fn start_app(canary_port: u16) -> std::io::Result<App> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let app = App {
        address: listener.local_addr()?,
        requests: Log::default(),
        report: Arc::default(),
        finished: Arc::default(),
    };
    let (requests, report, finished) = (
        app.requests.clone(),
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

/// The server that must not be reached. Remembers the first line of whatever arrives.
fn start_canary() -> std::io::Result<(u16, Log)> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    let seen = Log::default();
    let log = seen.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let line = read_request(&stream).map_or("(a connection)".to_string(), |r| r.line);
            log.lock().unwrap().push(line);
            respond(&stream, "text/plain", "canary");
        }
    });
    Ok((port, seen))
}

fn mentions(log: &[String], id: &str) -> bool {
    let path = format!("/c/{id}");
    let host = format!("{id}.canary.invalid");
    log.iter().any(|line| {
        line.contains(&host) || line.split([' ', '?']).any(|word| word.ends_with(&path))
    })
}

enum Finish {
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
    println!("(takes about 20 seconds; a window opens and closes by itself)\n");

    let event_loop = EventLoopBuilder::<Finish>::with_user_event().build();
    let window = WindowBuilder::new()
        .with_title("Email Triage self-test")
        .build(&event_loop)
        .expect("a window can be created");
    let record = stopped.clone();
    let builder = WebViewBuilder::new().with_url(format!("http://{}/", app.address));
    let builder = if confined {
        view::restrict(builder, app.address, proxy.port(), move |what, url| {
            record.lock().unwrap().push(format!("{what:?} {url}"))
        })
    } else {
        builder
    };
    let webview = match crate::attach(builder, &window) {
        Ok(webview) => webview,
        Err(error) => return failed_to_start(&error.to_string()),
    };

    let wake = event_loop.create_proxy();
    let finished = app.finished.clone();
    std::thread::spawn(move || {
        let start = Instant::now();
        while !finished.load(Ordering::SeqCst) && start.elapsed() < Duration::from_secs(60) {
            std::thread::sleep(Duration::from_millis(100));
        }
        std::thread::sleep(Duration::from_secs(2));
        wake.send_event(Finish::Done).ok();
    });

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        let done = matches!(
            event,
            Event::UserEvent(Finish::Done)
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

fn verdict(app: &App, canary: &Log, proxy: &Proxy, stopped: &Log, url: &str) -> i32 {
    let canary = canary.lock().unwrap().clone();
    let stopped = stopped.lock().unwrap().clone();
    let refused = proxy.refused();
    let report = app.report.lock().unwrap().clone();
    let mut problems: Vec<String> = Vec::new();

    println!("{:<10} {:<26} outcome", "attempt", "destination");
    let mut unseen = 0;
    for channel in CHANNELS {
        for (host, destination) in HOSTS {
            let id = format!("{channel}-{host}");
            let outcome = if mentions(&canary, &id) {
                problems.push(format!("{id} reached the other server"));
                "REACHED THE OTHER SERVER"
            } else if mentions(&refused, &id) {
                "refused by the proxy"
            } else if mentions(&stopped, &id) {
                "stopped by the web view's rules"
            } else {
                unseen += 1;
                "no request was made"
            };
            println!("{channel:<10} {destination:<26} {outcome}");
        }
    }

    println!();
    println!("requests that reached the other server: {}", canary.len());
    for line in canary.iter().take(20) {
        println!("  {line}");
    }
    if !canary.is_empty() && problems.is_empty() {
        problems.push("the other server was reached".into());
    }
    println!(
        "the test page's own requests: {} arrived, {} of them through the proxy",
        app.requests.lock().unwrap().len(),
        proxy.passed_on()
    );
    println!("attempts of which nothing was seen anywhere: {unseen}");
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
                    let direct = ["RTCPeerConnection", "webkitRTCPeerConnection"]
                        .iter()
                        .any(|name| found[name].as_str().is_some_and(|kind| kind != "undefined"));
                    if direct || !found.is_object() {
                        problems.push(format!(
                            "direct connections (WebRTC) are available in frame {frame}: {found}"
                        ));
                    }
                }
                println!("frames checked for direct connections: {}", frames.len());
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
