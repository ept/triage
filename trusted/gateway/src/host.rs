//! The listeners that face the host: the control API, used by the wrapper app, and the relay
//! through which the wrapper app's web view reaches the app's web interface.
//!
//! Both are published on the host's loopback interface, where any local program, and any web
//! page open in a browser, can reach them. Each therefore requires a secret that the wrapper app
//! generated when it started the gateway, and both reject requests whose `Host` header is not a
//! loopback address (which defeats DNS rebinding). The two secrets are different, so that the
//! app's own code, which runs in the web view, cannot use the control API.

use std::sync::Arc;

use axum::extract::{Path, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::api::{Input, Params};
use crate::config::{AccountsConfig, LlmConfig, Secret};
use crate::error::{ApiError, ApiResult};
use crate::relay;
use crate::state::{ChangeRequestState, Gateway};

type Shared = State<Arc<Gateway>>;

const SESSION_COOKIE: &str = "triage_session";
const MAX_CODING_EVENT_TEXT: usize = 64 * 1024;

/// The policy for everything shown in the web view: nothing may be loaded from, or sent to,
/// anywhere but the app itself. It is added by the gateway so that the app, whose code is not
/// reviewed, cannot loosen it (several policies combine to the strictest).
pub const CONTENT_SECURITY_POLICY: &str = "default-src 'none'; script-src 'self'; \
    style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; \
    media-src 'self' data: blob:; connect-src 'self'; frame-src 'self' blob:; \
    child-src 'self' blob:; worker-src 'self'; form-action 'self'; base-uri 'none'; \
    frame-ancestors 'none'";

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn matches_secret(candidate: &str, secret: &Secret) -> bool {
    constant_time_eq(candidate.as_bytes(), secret.expose().as_bytes())
}

/// Whether the request was addressed to a loopback name. A page on another site that has been
/// pointed at 127.0.0.1 by DNS still sends its own host name.
fn host_is_loopback(headers: &HeaderMap) -> bool {
    let Some(host) = headers.get(header::HOST).and_then(|h| h.to_str().ok()) else {
        return false;
    };
    let name = if let Some(rest) = host.strip_prefix('[') {
        rest.split_once(']').map(|(name, _)| name).unwrap_or("")
    } else {
        host.rsplit_once(':')
            .filter(|(_, port)| port.bytes().all(|b| b.is_ascii_digit()))
            .map_or(host, |(name, _)| name)
    };
    matches!(
        name.to_ascii_lowercase().as_str(),
        "127.0.0.1" | "localhost" | "::1"
    )
}

async fn require_loopback_host(request: Request, next: Next) -> Response {
    if host_is_loopback(request.headers()) {
        next.run(request).await
    } else {
        ApiError::forbidden("this service only answers to loopback addresses").into_response()
    }
}

// ---------------------------------------------------------------------------------------------
// Control API

pub fn control_router(gateway: Arc<Gateway>) -> Router {
    Router::new()
        .route("/control/v1/accounts", put(set_accounts))
        .route("/control/v1/llm", put(set_llm))
        .route("/control/v1/status", get(status))
        .route("/control/v1/change-requests", get(change_requests))
        .route("/control/v1/change-requests/{id}/decision", post(decide))
        .route("/control/v1/coding/events", post(push_coding_event))
        .fallback(|| async { ApiError::not_found("no such endpoint") })
        .layer(middleware::from_fn_with_state(
            gateway.clone(),
            require_control_token,
        ))
        .layer(middleware::from_fn(require_loopback_host))
        .with_state(gateway)
}

async fn require_control_token(State(gateway): Shared, request: Request, next: Next) -> Response {
    let authorised = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .is_some_and(|token| matches_secret(token, &gateway.settings.control_token));
    if authorised {
        next.run(request).await
    } else {
        ApiError::unauthorized().into_response()
    }
}

async fn set_accounts(
    State(gateway): Shared,
    Input(config): Input<AccountsConfig>,
) -> ApiResult<Json<Value>> {
    gateway.set_accounts(config)?;
    Ok(Json(json!({})))
}

#[derive(Deserialize)]
#[serde(untagged)]
enum LlmInput {
    Configured(LlmConfig),
    Cleared {},
}

async fn set_llm(State(gateway): Shared, Input(input): Input<LlmInput>) -> ApiResult<Json<Value>> {
    gateway.set_llm(match input {
        LlmInput::Configured(config) => Some(config),
        LlmInput::Cleared {} => None,
    })?;
    Ok(Json(json!({})))
}

async fn status(State(gateway): Shared) -> Json<Value> {
    let mut accounts = Vec::new();
    for account in gateway.accounts() {
        accounts.push(json!({
            "id": account.config.id,
            "address": account.config.address,
            "last_error": account.last_error().await,
        }));
    }
    Json(json!({
        "version": env!("CARGO_PKG_VERSION"),
        "accounts": accounts,
        "llm_configured": gateway.llm().is_some(),
        "deletion_warnings": gateway.deletion_warnings(),
        "pending_change_requests":
            gateway.change_requests(Some(ChangeRequestState::Pending)).len(),
    }))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ChangeRequestParams {
    #[serde(default)]
    state: Option<ChangeRequestState>,
}

async fn change_requests(
    State(gateway): Shared,
    Params(params): Params<ChangeRequestParams>,
) -> Json<Value> {
    Json(json!({"change_requests": gateway.change_requests(params.state)}))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Decision {
    approve: bool,
}

async fn decide(
    State(gateway): Shared,
    Path(id): Path<String>,
    Input(decision): Input<Decision>,
) -> ApiResult<Json<Value>> {
    let request = gateway.decide_change_request(&id, decision.approve)?;
    Ok(Json(json!(request)))
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum CodingEventKind {
    Text,
    Status,
    Question,
    Done,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CodingEvent {
    #[serde(default)]
    request_id: Option<String>,
    kind: CodingEventKind,
    #[serde(default)]
    text: String,
}

async fn push_coding_event(
    State(gateway): Shared,
    Input(event): Input<CodingEvent>,
) -> ApiResult<Json<Value>> {
    if event.text.len() > MAX_CODING_EVENT_TEXT {
        return Err(ApiError::bad_request("the text is too long"));
    }
    let kind = match event.kind {
        CodingEventKind::Text => "text",
        CodingEventKind::Status => "status",
        CodingEventKind::Question => "question",
        CodingEventKind::Done => "done",
    };
    let seq = gateway.coding_events.push(json!({
        "request_id": event.request_id,
        "kind": kind,
        "text": event.text,
    }));
    Ok(Json(json!({"seq": seq})))
}

// ---------------------------------------------------------------------------------------------
// Web interface relay

pub fn ui_router(gateway: Arc<Gateway>) -> Router {
    Router::new()
        .route("/_triage/session", get(start_session))
        .route(
            "/_triage/{*rest}",
            axum::routing::any(|| async { ApiError::not_found("no such endpoint") }),
        )
        .fallback(relay_to_app)
        .layer(middleware::from_fn(require_loopback_host))
        .with_state(gateway)
}

#[derive(Deserialize)]
struct SessionParams {
    token: String,
}

/// Exchanges the secret, which the wrapper app puts into the first URL it loads, for a cookie
/// that scripts cannot read and that is not sent along with requests from other sites.
async fn start_session(State(gateway): Shared, Params(params): Params<SessionParams>) -> Response {
    if !matches_secret(&params.token, &gateway.settings.ui_token) {
        return ApiError::no_session().into_response();
    }
    let cookie = format!(
        "{SESSION_COOKIE}={}; Path=/; HttpOnly; SameSite=Strict",
        gateway.settings.ui_token.expose()
    );
    (
        StatusCode::SEE_OTHER,
        [
            (header::SET_COOKIE, cookie),
            (header::LOCATION, "/".to_string()),
            (header::CACHE_CONTROL, "no-store".to_string()),
        ],
    )
        .into_response()
}

/// Splits a `Cookie` header into the session cookie's value and the remaining cookies.
fn split_cookies(headers: &HeaderMap) -> (Option<String>, Option<String>) {
    let mut session = None;
    let mut others = Vec::new();
    for value in headers.get_all(header::COOKIE) {
        let Ok(value) = value.to_str() else { continue };
        for cookie in value.split(';') {
            let cookie = cookie.trim();
            match cookie.split_once('=') {
                Some((name, value)) if name == SESSION_COOKIE => session = Some(value.to_string()),
                _ if !cookie.is_empty() => others.push(cookie.to_string()),
                _ => {}
            }
        }
    }
    let others = (!others.is_empty()).then(|| others.join("; "));
    (session, others)
}

async fn relay_to_app(State(gateway): Shared, request: Request) -> Response {
    let (session, other_cookies) = split_cookies(request.headers());
    if !session.is_some_and(|value| matches_secret(&value, &gateway.settings.ui_token)) {
        return ApiError::no_session().into_response();
    }
    let path_and_query = request
        .uri()
        .path_and_query()
        .map(|pq| pq.as_str())
        .unwrap_or("/");
    let url = relay::join_url(&gateway.settings.app_url, path_and_query);
    let mut add = Vec::new();
    if let Some(cookies) = other_cookies
        && let Ok(value) = HeaderValue::from_str(&cookies)
    {
        add.push((header::COOKIE, value));
    }
    let mut response = match relay::forward(&gateway.http, &url, request, &["cookie"], add).await {
        Ok(response) => response,
        Err(error) => error.into_response(),
    };
    let headers = response.headers_mut();
    headers.append(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(CONTENT_SECURITY_POLICY),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    fn headers(name: header::HeaderName, value: &str) -> HeaderMap {
        let mut map = HeaderMap::new();
        map.insert(name, HeaderValue::from_str(value).unwrap());
        map
    }

    #[test]
    fn loopback_hosts() {
        for good in [
            "127.0.0.1",
            "127.0.0.1:8102",
            "localhost:8102",
            "LOCALHOST",
            "[::1]:8102",
            "[::1]",
        ] {
            assert!(host_is_loopback(&headers(header::HOST, good)), "{good}");
        }
        for bad in [
            "evil.example",
            "evil.example:8102",
            "127.0.0.1.evil.example",
            "localhost.evil.example:80",
            "[::2]:8102",
            "192.168.1.5:8102",
            "",
        ] {
            assert!(!host_is_loopback(&headers(header::HOST, bad)), "{bad}");
        }
        assert!(!host_is_loopback(&HeaderMap::new()));
    }

    #[test]
    fn cookies_are_split() {
        let (session, others) =
            split_cookies(&headers(header::COOKIE, "a=1; triage_session=secret; b=2"));
        assert_eq!(session.as_deref(), Some("secret"));
        assert_eq!(others.as_deref(), Some("a=1; b=2"));
        let (session, others) = split_cookies(&headers(header::COOKIE, "xtriage_session=secret"));
        assert_eq!(session, None);
        assert_eq!(others.as_deref(), Some("xtriage_session=secret"));
        assert_eq!(split_cookies(&HeaderMap::new()), (None, None));
    }

    #[test]
    fn secrets_are_compared_in_full() {
        let secret = Secret::new("abcdef");
        assert!(matches_secret("abcdef", &secret));
        assert!(!matches_secret("abcdeg", &secret));
        assert!(!matches_secret("abcde", &secret));
        assert!(!matches_secret("", &secret));
    }
}
