//! Email Triage: the wrapper app.
//!
//! Opens a window that shows the app's web interface and nothing else (see `view.rs`), starts
//! the sandboxes like `triage up` does, and asks in a window of its own before any message goes
//! to the coding agent. Quitting stops the sandboxes.
//!
//! This is part of the trusted base: build it from a fresh clone of the repository at a commit
//! you have reviewed (see README.md). `email-triage --self-test` checks on this machine that
//! the web view is confined.

mod selftest;

use std::collections::HashMap;
use std::net::{Ipv4Addr, SocketAddr};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;

use tao::dpi::LogicalSize;
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy, EventLoopWindowTarget};
use tao::window::{Window, WindowBuilder, WindowId};
use triage_host::docker::Cli as DockerCli;
use triage_host::{run, settings};
use triage_wrapper::bridge::{Bridge, Request};
use triage_wrapper::pages::{self, Decision};
use triage_wrapper::proxy::Proxy;
use triage_wrapper::view;
use wry::{WebView, WebViewBuilder};

const NAME: &str = "Email Triage";

/// What other threads (and the handlers of the web views) tell the window thread.
enum Message {
    /// Something to show or ask, from the thread that supervises the sandboxes.
    Ui(Request),
    /// Everything is running; the app's interface is at this address, served on this port.
    Started { url: String, ui_port: u16 },
    /// The app's window is confined to the app (or could not be), and may now load it.
    Confined(Result<(), String>),
    /// Supervision has ended, with an error unless the user asked for it.
    Ended(Result<(), String>),
    /// The user chose an answer in the approval window with this id.
    Decided(WindowId, Decision),
    /// The user wants to quit (from the menu, which only macOS has).
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    Quit,
    /// The sandboxes have been stopped after the user quit.
    Stopped,
}

/// A window and the web view in it. The web view must be dropped before its window.
struct Pane {
    webview: WebView,
    window: Window,
}

/// Puts a web view into a window.
pub(crate) fn attach(builder: WebViewBuilder<'_>, window: &Window) -> wry::Result<WebView> {
    #[cfg(target_os = "linux")]
    {
        use tao::platform::unix::WindowExtUnix;
        use wry::WebViewBuilderExtUnix;
        builder.build_gtk(window.default_vbox().expect("the window has a container"))
    }
    #[cfg(not(target_os = "linux"))]
    {
        builder.build(window)
    }
}

fn open(
    target: &EventLoopWindowTarget<Message>,
    title: &str,
    size: (f64, f64),
    builder: impl FnOnce(&Window) -> WebViewBuilder<'static>,
) -> Result<Pane, String> {
    let window = WindowBuilder::new()
        .with_title(title)
        .with_inner_size(LogicalSize::new(size.0, size.1))
        .build(target)
        .map_err(|error| format!("could not open a window: {error}"))?;
    let webview = attach(builder(&window), &window)
        .map_err(|error| format!("could not create the web view: {error}"))?;
    Ok(Pane { webview, window })
}

/// A window with one of the wrapper's own pages.
fn open_own(
    target: &EventLoopWindowTarget<Message>,
    title: &str,
    size: (f64, f64),
    html: String,
    wake: &EventLoopProxy<Message>,
) -> Result<Pane, String> {
    open(target, title, size, |window| {
        let (id, wake) = (window.id(), wake.clone());
        view::own_page(html, move |url| {
            if let Some(decision) = pages::decision_from_url(url) {
                wake.send_event(Message::Decided(id, decision)).ok();
            }
        })
    })
}

/// Starts the sandboxes and supervises them, on a thread of its own. Sets `done` at the end.
fn supervise(wake: EventLoopProxy<Message>, stop: Arc<AtomicBool>, done: Arc<AtomicBool>) {
    let deliver = {
        let wake = wake.clone();
        move |request| {
            wake.send_event(Message::Ui(request)).ok();
        }
    };
    let frontend = Bridge::new(deliver, stop);
    let docker = DockerCli;
    let result = (|| {
        let data = settings::data_dir()?;
        let mut running = run::start(&data, &docker, false, &frontend)?;
        wake.send_event(Message::Started {
            url: running.control.session_url(),
            ui_port: running.config.ui_port,
        })
        .ok();
        run::supervise(&docker, &mut running, &frontend)
    })();
    done.store(true, Ordering::SeqCst);
    wake.send_event(Message::Ended(result)).ok();
}

/// The menu bar. On macOS, copy and paste only work in a window if the menu has them.
#[cfg(target_os = "macos")]
fn menu_bar(wake: EventLoopProxy<Message>) -> Option<muda::Menu> {
    use muda::accelerator::{Accelerator, Code, Modifiers};
    use muda::{Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};

    let menu = Menu::new();
    let app = Submenu::new(NAME, true);
    let quit = MenuItem::with_id(
        "quit",
        format!("Quit {NAME}"),
        true,
        Some(Accelerator::new(Modifiers::META, Code::KeyQ)),
    );
    app.append(&quit).ok()?;
    let edit = Submenu::new("Edit", true);
    edit.append_items(&[
        &PredefinedMenuItem::undo(None),
        &PredefinedMenuItem::redo(None),
        &PredefinedMenuItem::separator(),
        &PredefinedMenuItem::cut(None),
        &PredefinedMenuItem::copy(None),
        &PredefinedMenuItem::paste(None),
        &PredefinedMenuItem::select_all(None),
    ])
    .ok()?;
    menu.append_items(&[&app, &edit]).ok()?;
    menu.init_for_nsapp();
    MenuEvent::set_event_handler(Some(move |event: MenuEvent| {
        if event.id() == "quit" {
            wake.send_event(Message::Quit).ok();
        }
    }));
    Some(menu)
}

/// Everything the window thread keeps track of.
struct Windows {
    wake: EventLoopProxy<Message>,
    stop: Arc<AtomicBool>,
    /// Set when the supervising thread has ended.
    done: Arc<AtomicBool>,
    /// Progress while starting, and the reason if something has failed.
    status: Option<Pane>,
    log: Vec<String>,
    error: Option<String>,
    /// The app's interface, once it is running, and the proxy it reaches the app through.
    app: Option<Pane>,
    proxy: Option<Proxy>,
    /// The address to load in the app's window as soon as the window is confined.
    waiting_to_load: Option<String>,
    /// Open questions: the window, and where the answer goes. Dropping an entry answers no.
    questions: HashMap<WindowId, (Pane, mpsc::Sender<bool>)>,
    warnings: HashMap<WindowId, Pane>,
    quitting: bool,
}

impl Windows {
    fn show_status(&mut self, target: &EventLoopWindowTarget<Message>) {
        let html = pages::status_page(&self.log, self.error.as_deref());
        match &self.status {
            Some(pane) => {
                pane.webview.load_html(&html).ok();
            }
            None => {
                self.status = open_own(target, NAME, (640.0, 420.0), html, &self.wake).ok();
            }
        }
    }

    fn handle(&mut self, message: Message, target: &EventLoopWindowTarget<Message>) -> bool {
        match message {
            Message::Ui(Request::Say(line)) => {
                self.log.push(line);
                if self.app.is_none() && !self.quitting {
                    self.show_status(target);
                }
            }
            Message::Ui(Request::Warn(text)) => {
                let html = pages::warning_page(&text);
                if let Ok(pane) = open_own(target, "Warning", (520.0, 260.0), html, &self.wake) {
                    pane.window.set_focus();
                    self.warnings.insert(pane.window.id(), pane);
                }
            }
            Message::Ui(Request::Approve { what, text, answer }) => {
                if self.quitting {
                    return false; // dropping `answer` says no
                }
                let html = pages::approval_page(&what, &text);
                let title = "Send this to the coding agent?";
                // If the window cannot be opened, `answer` is dropped: not sent.
                if let Ok(pane) = open_own(target, title, (720.0, 560.0), html, &self.wake) {
                    pane.window.set_focus();
                    self.questions.insert(pane.window.id(), (pane, answer));
                }
            }
            Message::Decided(id, decision) => {
                if let Some((_pane, answer)) = self.questions.remove(&id) {
                    answer.send(decision == Decision::Send).ok();
                }
            }
            Message::Started { url, ui_port } => {
                if self.quitting {
                    return false;
                }
                if let Err(error) = self.show_app(target, &url, ui_port) {
                    self.error = Some(error);
                    self.show_status(target);
                }
            }
            Message::Confined(result) => {
                let url = self.waiting_to_load.take();
                match (result, &self.app, url) {
                    (Ok(()), Some(pane), Some(url)) => {
                        if let Err(error) = pane.webview.load_url(&url) {
                            self.error = Some(format!("The app could not be shown: {error}"));
                        }
                    }
                    (Err(reason), _, _) => {
                        // Never show the app in a window that is not confined.
                        self.app = None;
                        self.error = Some(format!(
                            "The window could not be confined to the app, so the app is not \
                             shown. {reason}"
                        ));
                    }
                    _ => {}
                }
                if self.error.is_some() && !self.quitting {
                    self.app = None;
                    self.show_status(target);
                }
            }
            Message::Ended(result) => {
                if self.quitting {
                    return false; // the sandboxes are being stopped; wait for that
                }
                self.error = Some(result.err().unwrap_or_else(|| "It has stopped.".into()));
                self.app = None;
                self.show_status(target);
            }
            Message::Quit => self.quit(),
            Message::Stopped => return true,
        }
        false
    }

    fn show_app(
        &mut self,
        target: &EventLoopWindowTarget<Message>,
        url: &str,
        ui_port: u16,
    ) -> Result<(), String> {
        let app = SocketAddr::from((Ipv4Addr::LOCALHOST, ui_port));
        let proxy = Proxy::start(app).map_err(|error| format!("no proxy: {error}"))?;
        let proxy_port = proxy.port();
        // What is stopped is not reported anywhere: the page that tried is the app's, and has
        // no say here. `--self-test` shows that stopping works.
        let builder = view::restrict(WebViewBuilder::new(), app, proxy_port, |_what, _url| {})?;
        // The window starts empty. The app is loaded when `Confined` arrives.
        let pane = open(target, NAME, (1180.0, 800.0), move |_window| builder)?;
        pane.window.set_focus();
        let wake = self.wake.clone();
        view::block_other_addresses(&pane.webview, app, move |result| {
            wake.send_event(Message::Confined(result)).ok();
        });
        self.waiting_to_load = Some(url.to_string());
        self.proxy = Some(proxy);
        self.app = Some(pane);
        self.status = None;
        Ok(())
    }

    /// The user wants to quit: refuse what is still open, stop the sandboxes, then exit.
    fn quit(&mut self) {
        if self.quitting {
            return;
        }
        self.quitting = true;
        self.stop.store(true, Ordering::SeqCst);
        self.questions.clear();
        self.warnings.clear();
        self.app = None;
        if let Some(pane) = &self.status {
            self.log.push("Stopping ...".into());
            pane.webview
                .load_html(&pages::status_page(&self.log, None))
                .ok();
        }
        let (wake, done) = (self.wake.clone(), self.done.clone());
        std::thread::spawn(move || {
            // Also ends a coding agent that is still working: its container goes away.
            run::down(&DockerCli);
            // If the user quit while things were still starting, the supervising thread may
            // create a container after this. Give it a moment to notice, then clear up again.
            let patience = std::time::Instant::now();
            while !done.load(Ordering::SeqCst) && patience.elapsed().as_secs() < 20 {
                std::thread::sleep(std::time::Duration::from_millis(200));
            }
            run::down(&DockerCli);
            if let Ok(data) = settings::data_dir() {
                std::fs::remove_file(data.join("session.json")).ok();
            }
            wake.send_event(Message::Stopped).ok();
        });
    }

    fn closed(&mut self, id: WindowId) {
        let main = [&self.app, &self.status]
            .iter()
            .any(|pane| pane.as_ref().is_some_and(|pane| pane.window.id() == id));
        if main {
            self.quit();
        } else {
            // Closing a question without answering it means no.
            self.questions.remove(&id);
            self.warnings.remove(&id);
        }
    }
}

fn main() {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    match arguments.iter().map(String::as_str).collect::<Vec<_>>()[..] {
        [] => {}
        ["--self-test"] => std::process::exit(selftest::run(true)),
        ["--self-test-control"] => std::process::exit(selftest::run(false)),
        _ => {
            eprintln!("usage: email-triage [--self-test | --self-test-control]");
            std::process::exit(2);
        }
    }

    let event_loop = EventLoopBuilder::<Message>::with_user_event().build();
    let wake = event_loop.create_proxy();
    let stop = Arc::new(AtomicBool::new(false));
    let done = Arc::new(AtomicBool::new(false));
    #[cfg(target_os = "macos")]
    let _menu = menu_bar(wake.clone());

    let mut windows = Windows {
        wake: wake.clone(),
        stop: stop.clone(),
        done: done.clone(),
        status: None,
        log: Vec::new(),
        error: None,
        app: None,
        proxy: None,
        waiting_to_load: None,
        questions: HashMap::new(),
        warnings: HashMap::new(),
        quitting: false,
    };
    let mut started = false;

    event_loop.run(move |event, target, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::NewEvents(tao::event::StartCause::Init) if !started => {
                started = true;
                windows.show_status(target);
                let (wake, stop, done) = (wake.clone(), stop.clone(), done.clone());
                std::thread::spawn(move || supervise(wake, stop, done));
            }
            Event::UserEvent(message) => {
                if windows.handle(message, target) {
                    *control_flow = ControlFlow::Exit;
                }
            }
            // Destroyed: the window went away without asking (possible on some systems).
            Event::WindowEvent {
                event: WindowEvent::CloseRequested | WindowEvent::Destroyed,
                window_id,
                ..
            } => windows.closed(window_id),
            _ => {}
        }
    })
}
