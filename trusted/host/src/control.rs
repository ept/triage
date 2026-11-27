//! Client for the gateway's control API and for the health check of the app, both of which are
//! published on the host's loopback interface.

use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::settings::{HostConfig, Session};

/// Why a request to the gateway's control API failed.
#[derive(Debug, PartialEq, Eq)]
pub enum ControlError {
    /// No answer: the gateway is not running, or not ready yet.
    Unreachable(String),
    /// The gateway answered with an error.
    Refused { status: u16, message: String },
}

impl std::fmt::Display for ControlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ControlError::Unreachable(error) => {
                write!(f, "the gateway cannot be reached: {error}")
            }
            ControlError::Refused { status, message } => {
                write!(f, "the gateway answered {status}: {message}")
            }
        }
    }
}

/// What became of a change to the account list (see [`apply_accounts`]).
#[derive(Debug, PartialEq, Eq)]
pub enum Applied {
    /// The running gateway has taken over the new list; the app notices by itself.
    Now,
    /// Nothing is running, so the list takes effect when the gateway is next started.
    AtNextStart,
    /// Something is running but did not take the list, for the reason given. The list is
    /// still fine to store; it takes effect at the next start.
    NotNow(String),
}

/// Hands a new account list to the gateway if one is running, so that adding or removing an
/// account does not need a restart. `Err` means that the gateway looked at the list and
/// rejected it (a malformed address, say), in which case it should not be stored either.
pub fn apply_accounts(
    config: &HostConfig,
    session: Option<&Session>,
    accounts: &Value,
) -> Result<Applied, String> {
    let Some(session) = session else {
        return Ok(Applied::AtNextStart);
    };
    let control = Control::new(
        config.control_port,
        config.ui_port,
        &session.control_token,
        &session.ui_token,
    );
    match control.set_accounts(accounts) {
        Ok(()) => Ok(Applied::Now),
        // A session file can outlive its gateway (after a crash, or when Docker was stopped).
        Err(ControlError::Unreachable(_)) => Ok(Applied::AtNextStart),
        Err(ControlError::Refused {
            status: 400,
            message,
        }) => Err(message),
        Err(error) => Ok(Applied::NotNow(error.to_string())),
    }
}

pub struct Control {
    control_url: String,
    ui_url: String,
    control_token: String,
    ui_token: String,
    agent: ureq::Agent,
}

impl Control {
    pub fn new(control_port: u16, ui_port: u16, control_token: &str, ui_token: &str) -> Self {
        let agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .timeout_global(Some(Duration::from_secs(30)))
            // Never go through a proxy for loopback requests that carry secrets.
            .proxy(None)
            .build()
            .into();
        Control {
            control_url: format!("http://127.0.0.1:{control_port}"),
            ui_url: format!("http://127.0.0.1:{ui_port}"),
            control_token: control_token.to_string(),
            ui_token: ui_token.to_string(),
            agent,
        }
    }

    fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Result<Value, String> {
        self.try_call(method, path, body)
            .map_err(|error| error.to_string())
    }

    fn try_call(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<Value, ControlError> {
        let url = format!("{}{path}", self.control_url);
        let authorization = format!("Bearer {}", self.control_token);
        let result = match (method, body) {
            ("GET", _) => self
                .agent
                .get(&url)
                .header("Authorization", &authorization)
                .call(),
            ("PUT", Some(body)) => self
                .agent
                .put(&url)
                .header("Authorization", &authorization)
                .send_json(body),
            ("POST", Some(body)) => self
                .agent
                .post(&url)
                .header("Authorization", &authorization)
                .send_json(body),
            _ => panic!("unsupported request {method} {path}"),
        };
        let mut response = result.map_err(|error| ControlError::Unreachable(error.to_string()))?;
        let status = response.status();
        let value: Value = response.body_mut().read_json().unwrap_or(Value::Null);
        if status.is_success() {
            Ok(value)
        } else {
            let message = value
                .pointer("/error/message")
                .and_then(Value::as_str)
                .unwrap_or("no details");
            Err(ControlError::Refused {
                status: status.as_u16(),
                message: message.to_string(),
            })
        }
    }

    pub fn status(&self) -> Result<Value, String> {
        self.call("GET", "/control/v1/status", None)
    }

    pub fn wait_until_ready(&self, limit: Duration) -> Result<(), String> {
        let deadline = Instant::now() + limit;
        loop {
            match self.status() {
                Ok(_) => return Ok(()),
                Err(error) if Instant::now() > deadline => return Err(error),
                Err(_) => std::thread::sleep(Duration::from_millis(200)),
            }
        }
    }

    /// Hands the account configuration (a JSON object with an `accounts` array, including
    /// credentials) to the gateway, which keeps it in memory only.
    pub fn set_accounts(&self, accounts: &Value) -> Result<(), ControlError> {
        self.try_call("PUT", "/control/v1/accounts", Some(accounts))
            .map(|_| ())
    }

    pub fn pending_change_requests(&self) -> Result<Vec<Value>, String> {
        let value = self.call("GET", "/control/v1/change-requests?state=pending", None)?;
        Ok(value["change_requests"]
            .as_array()
            .cloned()
            .unwrap_or_default())
    }

    pub fn decide(&self, id: &str, approve: bool) -> Result<(), String> {
        let path = format!("/control/v1/change-requests/{id}/decision");
        self.call("POST", &path, Some(&json!({"approve": approve})))
            .map(|_| ())
    }

    pub fn push_coding_event(
        &self,
        request_id: Option<&str>,
        kind: &str,
        text: &str,
    ) -> Result<(), String> {
        // The gateway limits the size of an event.
        let text: String = text.chars().take(16_000).collect();
        let body = json!({"request_id": request_id, "kind": kind, "text": text});
        self.call("POST", "/control/v1/coding/events", Some(&body))
            .map(|_| ())
    }

    /// Whether the app in the email sandbox answers its health check (through the gateway's
    /// relay, since the email sandbox has no port on the host).
    pub fn app_is_healthy(&self) -> bool {
        let url = format!("{}/healthz", self.ui_url);
        self.agent
            .get(&url)
            .header("Cookie", &format!("triage_session={}", self.ui_token))
            .call()
            .is_ok_and(|response| response.status().is_success())
    }

    pub fn wait_until_app_is_healthy(&self, limit: Duration) -> bool {
        let deadline = Instant::now() + limit;
        while Instant::now() < deadline {
            if self.app_is_healthy() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        false
    }

    /// The address that starts a session in the web interface.
    pub fn session_url(&self) -> String {
        format!("{}/_triage/session?token={}", self.ui_url, self.ui_token)
    }
}

#[cfg(test)]
mod tests {
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::mpsc;

    use super::*;

    const TOKEN: &str = "control-token-for-tests-0123456789abcdef";

    /// A stand-in for the gateway's control API that answers one request with the given
    /// status and body, and reports what it received: request line, authorization, body.
    fn serve_once(
        status: u16,
        body: &'static str,
    ) -> (u16, mpsc::Receiver<(String, String, Value)>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (sender, receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream);
            let mut request_line = String::new();
            reader.read_line(&mut request_line).unwrap();
            let (mut length, mut authorization) = (0, String::new());
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let line = line.trim_end();
                if line.is_empty() {
                    break;
                }
                let (name, value) = line.split_once(':').unwrap();
                match name.to_ascii_lowercase().as_str() {
                    "content-length" => length = value.trim().parse().unwrap(),
                    "authorization" => authorization = value.trim().to_string(),
                    _ => {}
                }
            }
            let mut content = vec![0; length];
            reader.read_exact(&mut content).unwrap();
            let received = serde_json::from_slice(&content).unwrap_or(Value::Null);
            let response = format!(
                "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n\
                 Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            reader.get_mut().write_all(response.as_bytes()).unwrap();
            sender
                .send((request_line.trim_end().to_string(), authorization, received))
                .unwrap();
        });
        (port, receiver)
    }

    fn config(control_port: u16) -> HostConfig {
        serde_json::from_value(json!({
            "repo": "/Users/robin/src/triage", "control_port": control_port, "ui_port": 1,
        }))
        .unwrap()
    }

    fn session() -> Session {
        Session {
            control_token: TOKEN.to_string(),
            ui_token: "ui-token-for-tests-0123456789abcdef".to_string(),
        }
    }

    fn accounts() -> Value {
        json!({"accounts": [{"id": "work", "address": "robin@example.org"}]})
    }

    #[test]
    fn a_running_gateway_takes_over_the_accounts_at_once() {
        let (port, received) = serve_once(200, "{}");
        let result = apply_accounts(&config(port), Some(&session()), &accounts());
        assert_eq!(result, Ok(Applied::Now));
        let (request_line, authorization, body) = received.recv().unwrap();
        assert_eq!(request_line, "PUT /control/v1/accounts HTTP/1.1");
        assert_eq!(authorization, format!("Bearer {TOKEN}"));
        assert_eq!(body, accounts());
    }

    #[test]
    fn without_a_running_gateway_the_accounts_wait_for_the_next_start() {
        // No session at all.
        let result = apply_accounts(&config(1), None, &accounts());
        assert_eq!(result, Ok(Applied::AtNextStart));
        // A session file left behind by a gateway that is gone: nothing listens on its port.
        let unused = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = unused.local_addr().unwrap().port();
        drop(unused);
        let result = apply_accounts(&config(port), Some(&session()), &accounts());
        assert_eq!(result, Ok(Applied::AtNextStart));
    }

    #[test]
    fn accounts_that_the_gateway_rejects_are_an_error() {
        let (port, _received) = serve_once(
            400,
            r#"{"error": {"code": "bad_request", "message": "invalid account id"}}"#,
        );
        let result = apply_accounts(&config(port), Some(&session()), &accounts());
        assert_eq!(result, Err("invalid account id".to_string()));
    }

    #[test]
    fn other_failures_do_not_stop_the_accounts_from_being_stored() {
        // For instance a session file that belongs to an earlier gateway: wrong token.
        let (port, _received) = serve_once(
            401,
            r#"{"error": {"code": "unauthorized", "message": "missing or wrong token"}}"#,
        );
        let result = apply_accounts(&config(port), Some(&session()), &accounts());
        assert_eq!(
            result,
            Ok(Applied::NotNow(
                "the gateway answered 401: missing or wrong token".to_string()
            ))
        );
    }
}
