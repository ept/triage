//! Starting everything and supervising it: what `triage up` does, and what the wrapper app does
//! when it is opened.
//!
//! Whatever needs the user (progress, warnings, and above all the approval of each message to
//! the coding agent) goes through a [`Frontend`], so that the command line and the wrapper app
//! share everything else.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::agent::{self, AgentEvent};
use crate::control::Control;
use crate::deploy::{self, Outcome};
use crate::docker::Docker;
use crate::sandbox;
use crate::secrets::{self, SecretStore};
use crate::settings::{self, HostConfig, Session, State};

/// How the user is told things and asked things.
pub trait Frontend {
    /// Reports what is happening.
    fn say(&self, text: &str);

    /// A warning that the user must not miss.
    fn warn(&self, text: &str);

    /// Shows the exact text that the app wants to send to the coding agent, and asks the user
    /// whether to send it. `kind` is `feature`, `bug` or `reply`. This must not be shown inside
    /// the app's own web interface, which may be compromised.
    fn approve(&self, kind: &str, text: &str) -> Result<bool, String>;

    /// Whether the user has asked for everything to be stopped.
    fn stopping(&self) -> bool {
        false
    }
}

/// What kind of message the app wants to send to the coding agent, in words.
pub fn describe_kind(kind: &str) -> &'static str {
    match kind {
        "feature" => "a request for a change to the app",
        "bug" => "a bug report",
        "reply" => "a reply to the coding agent",
        _ => "a message",
    }
}

/// Everything that is known about the sandboxes once they are running.
pub struct Running {
    pub control: Control,
    pub config: HostConfig,
    layout: sandbox::Layout,
    state: State,
    state_path: PathBuf,
}

pub fn load_config(data: &Path) -> Result<HostConfig, String> {
    settings::read(&data.join("host.json"))?
        .ok_or_else(|| "not set up yet; run `triage init --repo <path>` first".to_string())
}

pub fn load_accounts(store: &dyn SecretStore) -> Result<Vec<Value>, String> {
    let Some(text) = store.get(secrets::ACCOUNTS)? else {
        return Ok(Vec::new());
    };
    let value: Value =
        serde_json::from_str(&text).map_err(|_| "the stored accounts are corrupt".to_string())?;
    Ok(value["accounts"].as_array().cloned().unwrap_or_default())
}

/// Stops and removes the containers.
pub fn down(docker: &dyn Docker) {
    for container in [sandbox::APP, sandbox::GATEWAY, sandbox::CODING] {
        docker.run(&sandbox::remove(container), &[]).ok();
    }
}

pub fn image_exists(docker: &dyn Docker, image: &str) -> bool {
    let args: Vec<String> = ["image", "inspect", "--format", "ok", image]
        .iter()
        .map(|s| s.to_string())
        .collect();
    docker.run(&args, &[]).is_ok()
}

pub fn report(outcome: &Outcome) -> String {
    match outcome {
        Outcome::Unchanged => "The app is up to date.".into(),
        Outcome::Updated { image } => format!("The app was updated ({image})."),
        Outcome::BuildFailed { reason } => format!(
            "The new version of the app could not be built, so the previous version keeps \
             running.\n{reason}"
        ),
        Outcome::RolledBack { failed_image } => format!(
            "The new version of the app ({failed_image}) did not start properly; the previous \
             version was restored."
        ),
        Outcome::Failed { failed_image } => {
            format!(
                "The app ({failed_image}) did not start properly, and there is no previous version."
            )
        }
    }
}

/// Creates the networks and containers, hands the accounts to the gateway, and brings the app
/// up to date with the committed source.
/// Makes sure, at the end of starting, that the app answers. `deploy` waits for a version
/// that it starts itself, but when the committed version is the one from last time, the app
/// was started only a moment ago and `deploy` returns at once. Whoever opens the app right
/// then (the wrapper does, by itself) would get the gateway's error instead of the app.
fn wait_for_app(outcome: &Outcome, healthy: &dyn Fn() -> bool) -> Result<(), String> {
    if matches!(outcome, Outcome::Failed { .. }) {
        return Err(
            "The app is not running: it did not start properly, and there is no previous \
             version to go back to."
                .into(),
        );
    }
    if healthy() {
        return Ok(());
    }
    Err(format!(
        "The app does not answer. `docker logs {}` shows what it has printed.",
        sandbox::APP
    ))
}

pub fn start(
    data: &Path,
    docker: &dyn Docker,
    rebuild: bool,
    frontend: &dyn Frontend,
) -> Result<Running, String> {
    let config = load_config(data)?;
    settings::create_directories(data)?;
    let layout = settings::layout(data, &config);
    let store = secrets::open()?;
    let accounts = load_accounts(store.as_ref())?;
    let llm_key = store.get(secrets::LLM_API_KEY)?;
    let coding_key = store.get(secrets::CODING_API_KEY)?;

    if !image_exists(docker, sandbox::GATEWAY_IMAGE) {
        return Err("the gateway image has not been built; run `triage build-gateway`".into());
    }
    if !image_exists(docker, sandbox::CODING_IMAGE) {
        frontend.say("Building the coding sandbox image (this takes a while the first time) ...");
        if !docker.interactive(&sandbox::build_coding(&config.repo)?)? {
            return Err("building the coding sandbox image failed".into());
        }
    }

    for network in sandbox::create_networks() {
        // Fails harmlessly if the network exists already.
        docker.run(&network, &[]).ok();
    }
    down(docker);

    let session = Session {
        control_token: settings::random_token()?,
        ui_token: settings::random_token()?,
    };
    settings::write(&data.join("session.json"), &session)?;
    let mut env = vec![
        (sandbox::ENV_CONTROL_TOKEN, session.control_token.as_str()),
        (sandbox::ENV_UI_TOKEN, session.ui_token.as_str()),
    ];
    if let Some(key) = &llm_key {
        env.push((sandbox::ENV_LLM_API_KEY, key.as_str()));
    }
    frontend.say("Starting the gateway ...");
    docker.run(&sandbox::create_gateway(&layout)?, &env)?;
    docker.run(&sandbox::connect_gateway(), &[])?;
    docker.run(&["start".to_string(), sandbox::GATEWAY.to_string()], &[])?;
    let control = Control::new(
        config.control_port,
        config.ui_port,
        &session.control_token,
        &session.ui_token,
    );
    control.wait_until_ready(Duration::from_secs(30))?;
    control
        .set_accounts(&json!({"accounts": accounts}))
        .map_err(|error| error.to_string())?;
    frontend.say(&format!("{} account(s) configured.", accounts.len()));
    if config.llm_port.is_none() {
        frontend.say("No local LLM is configured (`triage init --llm-port`); the app cannot answer questions.");
    }

    frontend.say("Starting the coding sandbox ...");
    let coding_env: Vec<(&str, &str)> = coding_key
        .as_deref()
        .map(|key| vec![(sandbox::ENV_CODING_API_KEY, key)])
        .unwrap_or_default();
    docker.run(
        &sandbox::run_coding(&layout, coding_key.is_some())?,
        &coding_env,
    )?;

    let state_path = data.join("state.json");
    let mut state: State = settings::read(&state_path)?.unwrap_or_default();
    let healthy = || control.wait_until_app_is_healthy(Duration::from_secs(90));
    frontend.say("Building and starting the app ...");
    // Start what worked last time, then bring it up to date.
    deploy::start_current(docker, &layout, &state).ok();
    let outcome = deploy::deploy(docker, &layout, &mut state, rebuild, &healthy)?;
    settings::write(&state_path, &state)?;
    frontend.say(&report(&outcome));
    wait_for_app(&outcome, &healthy)?;

    Ok(Running {
        control,
        config,
        layout,
        state,
        state_path,
    })
}

/// Waits for things that need the user or the coding agent: change requests that want
/// approval, and warnings from the gateway. Returns when the frontend says that the user wants
/// to stop, or with an error when the gateway can no longer be reached.
pub fn supervise(
    docker: &dyn Docker,
    running: &mut Running,
    frontend: &dyn Frontend,
) -> Result<(), String> {
    let Running {
        control,
        config,
        layout,
        state,
        state_path,
    } = running;
    let mut warnings_seen = 0;
    let mut last_contact = Instant::now();
    loop {
        if frontend.stopping() {
            return Ok(());
        }
        std::thread::sleep(Duration::from_secs(1));
        let status = match control.status() {
            Ok(status) => {
                last_contact = Instant::now();
                status
            }
            Err(error) if last_contact.elapsed() > Duration::from_secs(30) => return Err(error),
            Err(_) => continue,
        };

        let warnings = status["deletion_warnings"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        for warning in warnings.iter().skip(warnings_seen) {
            frontend.warn(&format!(
                "{} messages were moved to the trash in account {} within {} minutes.",
                warning["count"], warning["account"], warning["window_minutes"]
            ));
        }
        warnings_seen = warnings.len();

        if status["pending_change_requests"].as_u64().unwrap_or(0) == 0 {
            continue;
        }
        for request in control.pending_change_requests()? {
            let (Some(id), Some(kind), Some(text)) = (
                request["id"].as_str(),
                request["kind"].as_str(),
                request["text"].as_str(),
            ) else {
                continue;
            };
            if !frontend.approve(kind, text)? {
                control.decide(id, false)?;
                frontend.say("Not sent.\n");
                continue;
            }
            control.decide(id, true)?;
            run_agent(docker, control, config, id, kind, text, frontend);
            let healthy = || control.wait_until_app_is_healthy(Duration::from_secs(90));
            let message = match deploy::deploy(docker, layout, state, false, &healthy) {
                Ok(outcome) => report(&outcome),
                Err(error) => format!("The app could not be updated: {error}"),
            };
            settings::write(state_path, state)?;
            frontend.say(&format!("{message}\n"));
            control.push_coding_event(Some(id), "status", &message).ok();
        }
    }
}

fn run_agent(
    docker: &dyn Docker,
    control: &Control,
    config: &HostConfig,
    id: &str,
    kind: &str,
    text: &str,
    frontend: &dyn Frontend,
) {
    let input = match kind {
        "bug" => format!(
            "The app running in the email sandbox reports the following bug. Diagnose it, fix \
             it, add a test, and commit.\n\n{text}"
        ),
        _ => text.to_string(),
    };
    frontend.say("The coding agent is working ...");
    let mut finished = false;
    let args = agent::command(config.coding_model.as_deref(), kind == "reply");
    let result = docker.stream(&args, &input, &mut |line| {
        for event in agent::parse_line(line) {
            let (event_kind, event_text) = event.kind_and_text();
            match &event {
                AgentEvent::Status(status) => frontend.say(&format!("  · {status}")),
                AgentEvent::Text(text) => {
                    frontend.say(&format!("  {}", text.replace('\n', "\n  ")))
                }
                AgentEvent::Done { .. } => finished = true,
            }
            control
                .push_coding_event(Some(id), event_kind, event_text)
                .ok();
        }
    });
    if !finished {
        let message = match result {
            Ok(_) => "The coding agent stopped without finishing. Is it logged in? (`triage coding-login`)"
                .to_string(),
            Err(error) => format!("The coding agent could not be run: {error}"),
        };
        frontend.say(&message);
        control.push_coding_event(Some(id), "done", &message).ok();
    }
}

#[cfg(test)]
mod tests {
    use std::cell::{Cell, RefCell};
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    use super::*;

    const TREE: &str = "0123456789abcdef0123456789abcdef01234567";
    const TEXT: &str = "Please add a dark mode.";

    type Requests = Arc<Mutex<Vec<(String, Value)>>>;

    /// A stand-in for the gateway's control API with one change request waiting. Records
    /// every request line and body.
    fn gateway() -> (u16, Requests) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let requests: Requests = Arc::default();
        let seen = requests.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { return };
                let mut reader = BufReader::new(stream);
                let mut request_line = String::new();
                reader.read_line(&mut request_line).unwrap();
                let mut length = 0;
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    if line.trim_end().is_empty() {
                        break;
                    }
                    if let Some((name, value)) = line.split_once(':')
                        && name.eq_ignore_ascii_case("content-length")
                    {
                        length = value.trim().parse().unwrap();
                    }
                }
                let mut content = vec![0; length];
                reader.read_exact(&mut content).unwrap();
                let request = request_line.trim_end().trim_end_matches(" HTTP/1.1");
                let body = if request.ends_with("/status") {
                    json!({
                        "pending_change_requests": 1,
                        "deletion_warnings":
                            [{"count": 40, "account": "work", "window_minutes": 10}],
                    })
                } else if request.contains("change-requests?state=pending") {
                    json!({"change_requests": [{"id": "cr-7", "kind": "feature", "text": TEXT}]})
                } else {
                    json!({})
                }
                .to_string();
                seen.lock().unwrap().push((
                    request.to_string(),
                    serde_json::from_slice(&content).unwrap_or(Value::Null),
                ));
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                     Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                reader.get_mut().write_all(response.as_bytes()).unwrap();
            }
        });
        (port, requests)
    }

    /// Answers every question with `answer`, and asks for a stop once it has been asked.
    struct Scripted {
        answer: bool,
        said: RefCell<Vec<String>>,
        warned: RefCell<Vec<String>>,
        asked: RefCell<Vec<(String, String)>>,
        done: Cell<bool>,
    }

    impl Scripted {
        fn new(answer: bool) -> Self {
            Scripted {
                answer,
                said: RefCell::default(),
                warned: RefCell::default(),
                asked: RefCell::default(),
                done: Cell::new(false),
            }
        }
    }

    impl Frontend for Scripted {
        fn say(&self, text: &str) {
            self.said.borrow_mut().push(text.to_string());
        }

        fn warn(&self, text: &str) {
            self.warned.borrow_mut().push(text.to_string());
        }

        fn approve(&self, kind: &str, text: &str) -> Result<bool, String> {
            self.asked
                .borrow_mut()
                .push((kind.to_string(), text.to_string()));
            self.done.set(true);
            Ok(self.answer)
        }

        fn stopping(&self) -> bool {
            self.done.get()
        }
    }

    /// Plays a coding agent that says one thing and finishes, and a repository whose app is
    /// the version that is already running.
    #[derive(Default)]
    struct FakeDocker {
        calls: RefCell<Vec<String>>,
        inputs: RefCell<Vec<String>>,
    }

    impl Docker for FakeDocker {
        fn run(&self, args: &[String], _env: &[(&str, &str)]) -> Result<String, String> {
            self.calls.borrow_mut().push(args.join(" "));
            Ok(TREE.to_string())
        }

        fn pipe(&self, _from: &[String], _to: &[String]) -> Result<(), String> {
            panic!("nothing is to be built in this test");
        }

        fn stream(
            &self,
            args: &[String],
            input: &str,
            on_line: &mut dyn FnMut(&str),
        ) -> Result<bool, String> {
            self.calls.borrow_mut().push(args.join(" "));
            self.inputs.borrow_mut().push(input.to_string());
            on_line(
                r#"{"type":"assistant","message":{"content":[{"type":"text","text":"Done."}]}}"#,
            );
            on_line(r#"{"type":"result","is_error":false,"result":"Added a dark mode."}"#);
            Ok(true)
        }

        fn interactive(&self, _args: &[String]) -> Result<bool, String> {
            panic!("nothing is interactive in this test");
        }
    }

    fn running(port: u16, directory: &Path) -> Running {
        let config = HostConfig {
            repo: directory.join("repo"),
            control_port: port,
            ui_port: port,
            llm_port: None,
            coding_model: None,
            grants: Vec::new(),
        };
        Running {
            control: Control::new(port, port, "control-token", "ui-token"),
            layout: settings::layout(directory, &config),
            config,
            state: State {
                generation: 3,
                current_image: Some("triage-app:3".into()),
                current_tree: Some(TREE.into()),
            },
            state_path: directory.join("state.json"),
        }
    }

    fn sent(requests: &Requests, part: &str) -> Vec<Value> {
        let requests = requests.lock().unwrap();
        requests
            .iter()
            .filter(|(line, _)| line.starts_with("POST") && line.contains(part))
            .map(|(_, body)| body.clone())
            .collect()
    }

    #[test]
    fn starting_waits_for_an_app_that_was_only_just_started() {
        use std::cell::Cell;
        // The usual start: nothing new to deploy, and the app needs a moment to come up.
        let asked = Cell::new(0);
        let comes_up = || {
            asked.set(asked.get() + 1);
            true
        };
        assert_eq!(wait_for_app(&Outcome::Unchanged, &comes_up), Ok(()));
        assert_eq!(asked.get(), 1);

        for outcome in [
            Outcome::Unchanged,
            Outcome::Updated {
                image: "triage-app:2".into(),
            },
            Outcome::BuildFailed {
                reason: "tests failed".into(),
            },
            Outcome::RolledBack {
                failed_image: "triage-app:2".into(),
            },
        ] {
            assert_eq!(wait_for_app(&outcome, &|| true), Ok(()));
            let error = wait_for_app(&outcome, &|| false).unwrap_err();
            assert!(error.contains("does not answer"), "{error}");
        }

        // Nothing to wait for: there is no app.
        let failed = Outcome::Failed {
            failed_image: "triage-app:1".into(),
        };
        let error = wait_for_app(&failed, &|| panic!("not asked")).unwrap_err();
        assert!(error.contains("not running"), "{error}");
    }

    #[test]
    fn a_refused_request_is_not_sent_and_runs_nothing() {
        let (port, requests) = gateway();
        let directory = tempfile::tempdir().unwrap();
        let docker = FakeDocker::default();
        let frontend = Scripted::new(false);
        supervise(&docker, &mut running(port, directory.path()), &frontend).unwrap();

        // The user was shown the exact text, and warned about the deletions.
        assert_eq!(
            *frontend.asked.borrow(),
            vec![("feature".to_string(), TEXT.to_string())]
        );
        assert_eq!(
            *frontend.warned.borrow(),
            vec!["40 messages were moved to the trash in account \"work\" within 10 minutes."]
        );
        assert_eq!(
            sent(&requests, "/change-requests/cr-7/decision"),
            vec![json!({"approve": false})]
        );
        assert!(docker.calls.borrow().is_empty(), "no agent, no build");
        assert!(sent(&requests, "/coding/events").is_empty());
    }

    #[test]
    fn an_approved_request_goes_to_the_agent_word_for_word() {
        let (port, requests) = gateway();
        let directory = tempfile::tempdir().unwrap();
        let docker = FakeDocker::default();
        let frontend = Scripted::new(true);
        supervise(&docker, &mut running(port, directory.path()), &frontend).unwrap();

        assert_eq!(
            sent(&requests, "/change-requests/cr-7/decision"),
            vec![json!({"approve": true})]
        );
        assert_eq!(*docker.inputs.borrow(), vec![TEXT.to_string()]);
        let calls = docker.calls.borrow();
        assert!(calls[0].contains("claude --print"), "{calls:?}");
        assert!(
            calls[1].contains("rev-parse"),
            "then the app is brought up to date"
        );
        // What the agent said reaches the app's interface, and so does the outcome.
        let events = sent(&requests, "/coding/events");
        let texts: Vec<&str> = events.iter().filter_map(|e| e["text"].as_str()).collect();
        assert_eq!(
            texts,
            vec!["Done.", "Added a dark mode.", "The app is up to date."]
        );
        assert!(events.iter().all(|event| event["request_id"] == "cr-7"));
        assert!(
            frontend
                .said
                .borrow()
                .iter()
                .any(|line| line.contains("Done."))
        );
    }
}
