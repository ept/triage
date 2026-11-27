//! The gateway's HTTP APIs, end to end: a real IMAP server (Dovecot) behind the gateway, and
//! stand-ins for the app's web server and the local LLM.

mod common;

use std::sync::Arc;
use std::time::Duration;

use axum::Router;
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{any, get};
use common::{Dovecot, message, parse_frames};
use serde_json::{Value, json};
use triage_gateway::config::Secret;
use triage_gateway::imap::{http_client, tls_config};
use triage_gateway::state::{Gateway, Settings};
use triage_gateway::{api, host};

const CONTROL_TOKEN: &str = "control-token-0123456789abcdef0123456789";
const UI_TOKEN: &str = "ui-token-0123456789abcdef0123456789abcdef";

struct TestGateway {
    sandbox: String,
    control: String,
    ui: String,
    http: reqwest::Client,
    _dir: tempfile::TempDir,
}

async fn serve(router: Router) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    format!("http://{addr}")
}

impl TestGateway {
    async fn start(app_url: &str, deletion_limit: u32) -> TestGateway {
        let dir = tempfile::tempdir().unwrap();
        let gateway = Arc::new(
            Gateway::new(Settings {
                data_dir: dir.path().to_path_buf(),
                control_token: Secret::new(CONTROL_TOKEN),
                ui_token: Secret::new(UI_TOKEN),
                app_url: app_url.to_string(),
                allow_insecure_imap: true,
                deletion_limit,
            })
            .unwrap(),
        );
        TestGateway {
            sandbox: serve(api::router(gateway.clone())).await,
            control: serve(host::control_router(gateway.clone())).await,
            ui: serve(host::ui_router(gateway)).await,
            http: http_client(&tls_config()),
            _dir: dir,
        }
    }

    /// A request to the sandbox API. Returns the status and the JSON body.
    async fn call(&self, path: &str, body: Value) -> (StatusCode, Value) {
        let response = self
            .http
            .post(format!("{}{path}", self.sandbox))
            .json(&body)
            .send()
            .await
            .unwrap();
        let status = response.status();
        (status, response.json().await.unwrap_or(Value::Null))
    }

    async fn ok(&self, path: &str, body: Value) -> Value {
        let (status, value) = self.call(path, body).await;
        assert!(status.is_success(), "{path}: {status} {value}");
        value
    }

    async fn get(&self, path: &str) -> Value {
        let response = self
            .http
            .get(format!("{}{path}", self.sandbox))
            .send()
            .await
            .unwrap();
        assert!(
            response.status().is_success(),
            "{path}: {}",
            response.status()
        );
        response.json().await.unwrap()
    }

    fn control(&self, method: reqwest::Method, path: &str) -> reqwest::RequestBuilder {
        self.http
            .request(method, format!("{}{path}", self.control))
            .bearer_auth(CONTROL_TOKEN)
    }

    async fn configure_account(&self, server: &Dovecot, user: &str) {
        let account = json!({
            "id": user,
            "address": format!("{user}@example.org"),
            "display_name": "Robin Example",
            "host": "127.0.0.1",
            "port": server.port,
            "security": "insecure",
            "username": user,
            "auth": {"type": "password", "password": common::PASSWORD},
        });
        let response = self
            .control(reqwest::Method::PUT, "/control/v1/accounts")
            .json(&json!({"accounts": [account]}))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }
}

fn seed(server: &Dovecot, user: &str, count: usize) {
    for i in 1..=count {
        let text = message(
            "kim@example.net",
            &format!("{user}@example.org"),
            &format!("Message {i}"),
            &format!("Body {i}"),
        );
        server.deliver(user, "INBOX", &text, "");
    }
}

fn error_code(body: &Value) -> &str {
    body["error"]["code"].as_str().unwrap_or("")
}

#[tokio::test]
async fn mailbox_operations_end_to_end() {
    let server = Dovecot::start();
    seed(&server, "robin", 4);
    let gateway = TestGateway::start("http://127.0.0.1:1", 20).await;

    assert_eq!(gateway.get("/v1/health").await["status"], "ok");
    assert_eq!(gateway.get("/v1/accounts").await["accounts"], json!([]));
    gateway.configure_account(&server, "robin").await;
    let accounts = gateway.get("/v1/accounts").await;
    assert_eq!(
        accounts["accounts"],
        json!([{"id": "robin", "address": "robin@example.org", "display_name": "Robin Example"}])
    );
    assert!(!accounts.to_string().contains(common::PASSWORD));

    let folders = gateway
        .ok("/v1/folders/list", json!({"account": "robin"}))
        .await;
    let drafts = folders["folders"]
        .as_array()
        .unwrap()
        .iter()
        .find(|f| f["name"] == "Drafts")
        .unwrap();
    assert_eq!(drafts["role"], "drafts");
    assert_eq!(drafts["protected"], true);
    assert!(drafts.get("raw_name").is_none());

    let status = gateway
        .ok(
            "/v1/folders/status",
            json!({"account": "robin", "folder": "INBOX"}),
        )
        .await;
    assert_eq!(status["messages"], 4);
    let validity = status["uid_validity"].clone();

    let list = gateway
        .ok(
            "/v1/messages/list",
            json!({"account": "robin", "folder": "INBOX", "limit": 3}),
        )
        .await;
    assert_eq!(list["messages"].as_array().unwrap().len(), 3);
    assert_eq!(list["next_uid"], 4);

    // Fetch: a framed stream.
    let response = gateway
        .http
        .post(format!("{}/v1/messages/fetch", gateway.sandbox))
        .json(&json!({"account": "robin", "folder": "INBOX", "uids": [2, 3]}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.headers()["content-type"],
        "application/vnd.triage.messages"
    );
    let (frames, end) = parse_frames(&response.bytes().await.unwrap());
    assert_eq!(frames.len(), 2);
    assert_eq!(end["uid_validity"], validity);
    assert!(String::from_utf8_lossy(&frames[0].body).contains("Subject: Message"));

    let search = gateway
        .ok(
            "/v1/messages/search",
            json!({"account": "robin", "folder": "INBOX", "query": {"subject": "Message 3"}}),
        )
        .await;
    assert_eq!(search["uids"], json!([3]));

    // Changes, each of which must show up in the operation log.
    gateway
        .ok(
            "/v1/messages/flags",
            json!({"account": "robin", "folder": "INBOX", "uid_validity": validity,
                   "uids": [1], "add": ["\\seen"]}),
        )
        .await;
    let moved = gateway
        .ok(
            "/v1/messages/move",
            json!({"account": "robin", "folder": "INBOX", "uid_validity": validity,
                   "uids": [2], "destination": "Archive"}),
        )
        .await;
    assert_eq!(moved["moved"][0]["uid"], 2);
    assert!(moved["moved"][0]["new_uid"].is_u64());
    gateway
        .ok(
            "/v1/folders/create",
            json!({"account": "robin", "name": "Triage/Later"}),
        )
        .await;
    let draft = gateway
        .ok(
            "/v1/drafts",
            json!({"account": "robin", "to": [{"address": "kim@example.net"}],
                   "subject": "Re: Message 1", "body": "Thanks!",
                   "in_reply_to": {"folder": "INBOX", "uid_validity": validity, "uid": 1}}),
        )
        .await;
    assert_eq!(draft["folder"], "Drafts");

    let log = gateway.get("/v1/log").await;
    let entries = log["entries"].as_array().unwrap();
    let ops: Vec<&str> = entries.iter().map(|e| e["op"].as_str().unwrap()).collect();
    assert_eq!(ops, vec!["flags", "move", "create_folder", "draft"]);
    assert_eq!(log["last_seq"], 4);
    assert_eq!(entries[0]["add"], json!(["\\Seen"]));
    assert_eq!(entries[0]["uids"], json!([1]));
    assert_eq!(entries[1]["destination"], "Archive");
    assert_eq!(entries[1]["moved"], moved["moved"]);
    assert_eq!(entries[2]["folder"], "Triage/Later");
    assert_eq!(entries[3]["message_id"], draft["message_id"]);
    assert_eq!(entries[3]["in_reply_to"]["uid"], 1);
    assert!(
        entries
            .iter()
            .all(|e| e["account"] == "robin" && e["time"].is_string())
    );
    assert!(
        !log.to_string().contains("Thanks!"),
        "no message content in the log"
    );
    assert_eq!(
        gateway.get("/v1/log?after=3").await["entries"]
            .as_array()
            .unwrap()
            .len(),
        1
    );

    // Refusals: in the API's error format, and without a trace in the operation log.
    let cases = [
        (
            "/v1/messages/flags",
            json!({"account": "robin", "folder": "INBOX",
            "uid_validity": validity, "uids": [1], "add": ["\\Deleted"]}),
            403,
            "forbidden",
        ),
        (
            "/v1/messages/move",
            json!({"account": "robin", "folder": "INBOX",
            "uid_validity": validity, "uids": [1], "destination": "Drafts"}),
            403,
            "forbidden",
        ),
        (
            "/v1/messages/move",
            json!({"account": "robin", "folder": "INBOX",
            "uid_validity": 1, "uids": [1], "destination": "Archive"}),
            409,
            "uid_validity_changed",
        ),
        (
            "/v1/drafts",
            json!({"account": "robin", "to": [{"address": "mallory@evil.example"}],
            "subject": "Re: x", "body": "x",
            "in_reply_to": {"folder": "INBOX", "uid_validity": validity, "uid": 1}}),
            403,
            "forbidden",
        ),
        (
            "/v1/drafts",
            json!({"account": "robin", "to": [{"address": "kim@example.net"}],
            "bcc": [{"address": "mallory@evil.example"}], "subject": "x", "body": "x"}),
            400,
            "bad_request",
        ),
        (
            "/v1/folders/status",
            json!({"account": "nobody", "folder": "INBOX"}),
            404,
            "not_found",
        ),
        (
            "/v1/folders/status",
            json!({"account": "robin", "folder": "Public/Announce"}),
            404,
            "not_found",
        ),
        (
            "/v1/folders/status",
            json!({"account": "robin", "folder": "INBOX", "extra": 1}),
            400,
            "bad_request",
        ),
        (
            "/v1/messages/search",
            json!({"account": "robin", "folder": "INBOX",
            "query": {"deleted": true}}),
            400,
            "bad_request",
        ),
        (
            "/v1/messages/fetch",
            json!({"account": "robin", "folder": "Nope", "uids": [1]}),
            404,
            "not_found",
        ),
    ];
    for (path, body, status, code) in cases {
        let (actual, response) = gateway.call(path, body.clone()).await;
        assert_eq!(actual.as_u16(), status, "{path} {body}: {response}");
        assert_eq!(error_code(&response), code, "{path} {body}");
    }
    assert_eq!(gateway.get("/v1/log").await["last_seq"], 4);

    // Endpoints that must not exist on the sandbox API.
    for path in [
        "/control/v1/status",
        "/control/v1/accounts",
        "/v1/messages/delete",
        "/v1/expunge",
    ] {
        let (status, _) = gateway.call(path, json!({})).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{path}");
    }
}

#[tokio::test]
async fn the_user_is_warned_about_many_deletions() {
    let server = Dovecot::start();
    seed(&server, "robin", 5);
    let gateway = TestGateway::start("http://127.0.0.1:1", 2).await;
    gateway.configure_account(&server, "robin").await;
    let validity = gateway
        .ok(
            "/v1/folders/status",
            json!({"account": "robin", "folder": "INBOX"}),
        )
        .await["uid_validity"]
        .clone();
    let trash = async |uids: Value, destination: &str| {
        gateway
            .ok(
                "/v1/messages/move",
                json!({"account": "robin", "folder": "INBOX", "uid_validity": validity,
                       "uids": uids, "destination": destination}),
            )
            .await;
    };

    trash(json!([1, 2]), "Trash").await;
    trash(json!([3]), "Archive").await;
    let events = gateway.get("/v1/events").await;
    assert!(
        events["events"]
            .as_array()
            .unwrap()
            .iter()
            .all(|e| e["kind"] != "deletion_rate"),
        "two deletions are within the limit, and archiving does not count"
    );

    trash(json!([4]), "Junk").await;
    let events = gateway.get("/v1/events").await;
    let warning = events["events"]
        .as_array()
        .unwrap()
        .iter()
        .find(|e| e["kind"] == "deletion_rate")
        .expect("a warning event");
    assert_eq!(warning["account"], "robin");
    assert_eq!(warning["count"], 3);
    assert_eq!(warning["window_minutes"], 60);

    let status: Value = gateway
        .control(reqwest::Method::GET, "/control/v1/status")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(status["deletion_warnings"][0]["count"], 3);
    assert_eq!(status["accounts"][0]["id"], "robin");
    assert_eq!(status["accounts"][0]["last_error"], Value::Null);
}

#[tokio::test]
async fn change_requests_go_nowhere_without_approval() {
    let gateway = TestGateway::start("http://127.0.0.1:1", 20).await;

    let (status, submitted) = gateway
        .call(
            "/v1/change-requests",
            json!({"kind": "feature", "text": "Please add a button"}),
        )
        .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(submitted["state"], "pending");
    let id = submitted["id"].as_str().unwrap().to_string();

    let seen = gateway.get(&format!("/v1/change-requests/{id}")).await;
    assert_eq!(
        seen,
        json!({"id": id, "kind": "feature", "state": "pending"})
    );

    // The sandbox cannot approve its own request.
    for path in [
        format!("/v1/change-requests/{id}/decision"),
        format!("/control/v1/change-requests/{id}/decision"),
    ] {
        let (status, _) = gateway.call(&path, json!({"approve": true})).await;
        assert!(
            status == StatusCode::NOT_FOUND || status == StatusCode::METHOD_NOT_ALLOWED,
            "{path}: {status}"
        );
    }
    assert_eq!(
        gateway.get(&format!("/v1/change-requests/{id}")).await["state"],
        "pending"
    );

    // The wrapper app sees the exact text, and records the user's decision.
    let pending: Value = gateway
        .control(
            reqwest::Method::GET,
            "/control/v1/change-requests?state=pending",
        )
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(pending["change_requests"][0]["text"], "Please add a button");
    assert_eq!(pending["change_requests"][0]["id"], id.as_str());
    let decided = gateway
        .control(
            reqwest::Method::POST,
            &format!("/control/v1/change-requests/{id}/decision"),
        )
        .json(&json!({"approve": true}))
        .send()
        .await
        .unwrap();
    assert_eq!(decided.status(), StatusCode::OK);
    assert_eq!(
        gateway.get(&format!("/v1/change-requests/{id}")).await["state"],
        "approved"
    );
    let events = gateway.get("/v1/events").await;
    assert_eq!(events["events"][0]["kind"], "change_request", "{events}");
    assert_eq!(events["events"][0]["state"], "approved");

    // Text that could hide something from the user is refused outright.
    for text in [
        "",
        "zero\u{200b}width",
        "tag\u{e0041}chars",
        &"x".repeat(4001),
    ] {
        let (status, _) = gateway
            .call("/v1/change-requests", json!({"kind": "bug", "text": text}))
            .await;
        assert!(status.is_client_error(), "{text:?}");
    }
    let (status, _) = gateway
        .call("/v1/change-requests", json!({"kind": "other", "text": "x"}))
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    // Output of the coding agent reaches the app through the gateway.
    let waiting = {
        let url = format!("{}/v1/coding/events?after=0&wait=10", gateway.sandbox);
        let http = gateway.http.clone();
        tokio::spawn(async move {
            http.get(url)
                .send()
                .await
                .unwrap()
                .json::<Value>()
                .await
                .unwrap()
        })
    };
    tokio::time::sleep(Duration::from_millis(50)).await;
    let pushed = gateway
        .control(reqwest::Method::POST, "/control/v1/coding/events")
        .json(&json!({"request_id": id, "kind": "text", "text": "Done."}))
        .send()
        .await
        .unwrap();
    assert_eq!(pushed.status(), StatusCode::OK);
    let received = waiting.await.unwrap();
    assert_eq!(received["events"][0]["text"], "Done.");
    assert_eq!(received["events"][0]["kind"], "text");
    assert_eq!(received["events"][0]["request_id"], id.as_str());
    assert_eq!(received["last_seq"], 1);
}

#[tokio::test]
async fn the_control_api_needs_its_token_and_a_loopback_host() {
    let gateway = TestGateway::start("http://127.0.0.1:1", 20).await;
    let url = format!("{}/control/v1/status", gateway.control);

    let ok = gateway
        .http
        .get(&url)
        .bearer_auth(CONTROL_TOKEN)
        .send()
        .await
        .unwrap();
    assert_eq!(ok.status(), StatusCode::OK);

    let none = gateway.http.get(&url).send().await.unwrap();
    assert_eq!(none.status(), StatusCode::UNAUTHORIZED);
    for wrong in [UI_TOKEN, "", "control-token-0123456789abcdef012345678X"] {
        let response = gateway
            .http
            .get(&url)
            .bearer_auth(wrong)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED, "{wrong}");
    }
    let cookie = gateway
        .http
        .get(&url)
        .header("cookie", format!("triage_session={CONTROL_TOKEN}"))
        .send()
        .await
        .unwrap();
    assert_eq!(cookie.status(), StatusCode::UNAUTHORIZED);

    // A web page that reaches the port through DNS rebinding sends its own host name.
    let rebound = gateway
        .http
        .get(&url)
        .bearer_auth(CONTROL_TOKEN)
        .header("host", "evil.example")
        .send()
        .await
        .unwrap();
    assert_eq!(rebound.status(), StatusCode::FORBIDDEN);

    // Invalid configuration is rejected.
    let insecure_llm = gateway
        .control(reqwest::Method::PUT, "/control/v1/llm")
        .json(&json!({"base_url": "ftp://example.org"}))
        .send()
        .await
        .unwrap();
    assert_eq!(insecure_llm.status(), StatusCode::BAD_REQUEST);
    let bad_account = gateway
        .control(reqwest::Method::PUT, "/control/v1/accounts")
        .json(
            &json!({"accounts": [{"id": "Bad Id", "address": "a@example.org", "host": "h",
            "port": 993, "username": "u", "auth": {"type": "password", "password": "p"}}]}),
        )
        .send()
        .await
        .unwrap();
    assert_eq!(bad_account.status(), StatusCode::BAD_REQUEST);
}

/// Stands in for the app's web server: reports what it received.
fn fake_app() -> Router {
    Router::new()
        .route(
            "/",
            get(|| async { ([("content-type", "text/html")], "<h1>app</h1>") }),
        )
        .route(
            "/echo",
            any(|headers: HeaderMap, body: String| async move {
                let cookie = headers
                    .get("cookie")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("");
                (
                    [
                        ("content-security-policy", "default-src *"),
                        ("x-from-app", "yes"),
                    ],
                    format!("cookie=[{cookie}] body=[{body}]"),
                )
            }),
        )
}

#[tokio::test]
async fn the_web_interface_relay_needs_a_session() {
    let app = serve(fake_app()).await;
    let gateway = TestGateway::start(&app, 20).await;
    let http = &gateway.http;

    // Without a session nothing is relayed.
    let anonymous = http.get(format!("{}/", gateway.ui)).send().await.unwrap();
    assert_eq!(anonymous.status(), StatusCode::UNAUTHORIZED);
    for wrong in ["nope", CONTROL_TOKEN] {
        let response = http
            .get(format!("{}/_triage/session?token={wrong}", gateway.ui))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        let response = http
            .get(format!("{}/", gateway.ui))
            .header("cookie", format!("triage_session={wrong}"))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        // A window left over from before a restart ends up here, so the answer says what to do.
        let body: Value = response.json().await.unwrap();
        let message = body["error"]["message"].as_str().unwrap();
        assert!(message.contains("restarted") && message.contains("triage up"));
    }

    // The wrapper app's web view starts a session with its secret ...
    let session = http
        .get(format!("{}/_triage/session?token={UI_TOKEN}", gateway.ui))
        .send()
        .await
        .unwrap();
    assert_eq!(session.status(), StatusCode::SEE_OTHER);
    assert_eq!(session.headers()["location"], "/");
    let cookie = session.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .to_string();
    assert!(
        cookie.contains("HttpOnly") && cookie.contains("SameSite=Strict"),
        "{cookie}"
    );
    let cookie = cookie.split(';').next().unwrap().to_string();

    // ... and then sees the app.
    let page = http
        .get(format!("{}/", gateway.ui))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(page.status(), StatusCode::OK);
    let policy = page.headers()["content-security-policy"]
        .to_str()
        .unwrap()
        .to_string();
    assert!(policy.contains("default-src 'none'") && policy.contains("connect-src 'self'"));
    assert_eq!(page.headers()["referrer-policy"], "no-referrer");
    assert_eq!(page.text().await.unwrap(), "<h1>app</h1>");

    // The app does not get to see the session cookie, and cannot loosen the policy.
    let echo = http
        .post(format!("{}/echo?x=1", gateway.ui))
        .header("cookie", format!("theme=dark; {cookie}"))
        .body("hello")
        .send()
        .await
        .unwrap();
    assert_eq!(echo.status(), StatusCode::OK);
    assert_eq!(echo.headers()["x-from-app"], "yes");
    let policies: Vec<_> = echo
        .headers()
        .get_all("content-security-policy")
        .iter()
        .map(|v| v.to_str().unwrap().to_string())
        .collect();
    assert_eq!(
        policies.len(),
        2,
        "the gateway's policy is added to the app's"
    );
    assert!(policies.iter().any(|p| p.contains("default-src 'none'")));
    assert_eq!(
        echo.text().await.unwrap(),
        "cookie=[theme=dark] body=[hello]"
    );

    // Reserved paths are not relayed, the control API is not reachable through this listener,
    // and requests from a rebound host name are refused.
    let reserved = http
        .get(format!("{}/_triage/other", gateway.ui))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(reserved.status(), StatusCode::NOT_FOUND);
    let control = http
        .get(format!("{}/control/v1/status", gateway.ui))
        .header("cookie", &cookie)
        .bearer_auth(CONTROL_TOKEN)
        .send()
        .await
        .unwrap();
    assert_eq!(
        control.status(),
        StatusCode::NOT_FOUND,
        "relayed to the app, which has no such page"
    );
    let rebound = http
        .get(format!("{}/", gateway.ui))
        .header("cookie", &cookie)
        .header("host", "evil.example")
        .send()
        .await
        .unwrap();
    assert_eq!(rebound.status(), StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn the_app_being_down_is_reported() {
    let gateway = TestGateway::start("http://127.0.0.1:1", 20).await;
    let response = gateway
        .http
        .get(format!("{}/", gateway.ui))
        .header("cookie", format!("triage_session={UI_TOKEN}"))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(response.headers().contains_key("content-security-policy"));
}

/// Stands in for the local LLM server.
fn fake_llm() -> Router {
    Router::new().route(
        "/v1/chat/completions",
        any(|headers: HeaderMap, body: String| async move {
            let auth = headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            let cookie = headers
                .get("cookie")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            format!("auth=[{auth}] cookie=[{cookie}] body=[{body}]")
        }),
    )
}

#[tokio::test]
async fn requests_to_the_local_llm_are_relayed() {
    let llm = serve(fake_llm()).await;
    let gateway = TestGateway::start("http://127.0.0.1:1", 20).await;
    let url = format!("{}/v1/llm/v1/chat/completions", gateway.sandbox);

    let unconfigured = gateway.http.post(&url).body("{}").send().await.unwrap();
    assert_eq!(unconfigured.status(), StatusCode::SERVICE_UNAVAILABLE);

    let configured = gateway
        .control(reqwest::Method::PUT, "/control/v1/llm")
        .json(&json!({"base_url": llm, "api_key": "llm-secret"}))
        .send()
        .await
        .unwrap();
    assert_eq!(configured.status(), StatusCode::OK);

    let response = gateway
        .http
        .post(&url)
        .header("authorization", "Bearer from-the-sandbox")
        .header("cookie", "a=b")
        .body("{\"messages\":[]}")
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.text().await.unwrap(),
        "auth=[Bearer llm-secret] cookie=[] body=[{\"messages\":[]}]"
    );

    let other_path = gateway
        .http
        .get(format!("{}/v1/llm/v1/models", gateway.sandbox))
        .send()
        .await
        .unwrap();
    assert_eq!(
        other_path.status(),
        StatusCode::NOT_FOUND,
        "passed through from the LLM server"
    );
    let delete = gateway.http.delete(&url).send().await.unwrap();
    assert_eq!(delete.status(), StatusCode::BAD_REQUEST);

    // Removing the configuration turns the relay off again.
    gateway
        .control(reqwest::Method::PUT, "/control/v1/llm")
        .json(&json!({}))
        .send()
        .await
        .unwrap();
    let off = gateway.http.post(&url).body("{}").send().await.unwrap();
    assert_eq!(off.status(), StatusCode::SERVICE_UNAVAILABLE);
}
