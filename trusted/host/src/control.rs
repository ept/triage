//! Client for the gateway's control API and for the health check of the app, both of which are
//! published on the host's loopback interface.

use std::time::{Duration, Instant};

use serde_json::{Value, json};

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
            _ => return Err(format!("unsupported request {method} {path}")),
        };
        let mut response =
            result.map_err(|error| format!("the gateway cannot be reached: {error}"))?;
        let status = response.status();
        let value: Value = response.body_mut().read_json().unwrap_or(Value::Null);
        if status.is_success() {
            Ok(value)
        } else {
            let message = value
                .pointer("/error/message")
                .and_then(Value::as_str)
                .unwrap_or("no details");
            Err(format!("the gateway answered {status}: {message}"))
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
    pub fn set_accounts(&self, accounts: &Value) -> Result<(), String> {
        self.call("PUT", "/control/v1/accounts", Some(accounts))
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
