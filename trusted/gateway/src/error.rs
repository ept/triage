//! Errors returned by the gateway's HTTP APIs.

use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::json;

/// An error as it is reported to the caller: an HTTP status, a stable machine-readable code, and
/// a message for humans. Messages must never contain credentials.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiError {
    pub status: StatusCode,
    pub code: &'static str,
    pub message: String,
}

pub type ApiResult<T> = Result<T, ApiError>;

impl ApiError {
    fn new(status: StatusCode, code: &'static str, message: impl Into<String>) -> Self {
        ApiError {
            status,
            code,
            message: message.into(),
        }
    }

    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, "bad_request", message)
    }

    /// The request is well-formed, but the gateway's rules do not allow it.
    pub fn forbidden(message: impl Into<String>) -> Self {
        Self::new(StatusCode::FORBIDDEN, "forbidden", message)
    }

    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new(StatusCode::NOT_FOUND, "not_found", message)
    }

    pub fn uid_validity_changed(expected: u32, actual: u32) -> Self {
        Self::new(
            StatusCode::CONFLICT,
            "uid_validity_changed",
            format!("uid_validity is {actual}, but the request assumed {expected}"),
        )
    }

    pub fn unsupported(message: impl Into<String>) -> Self {
        Self::new(StatusCode::NOT_IMPLEMENTED, "unsupported", message)
    }

    /// The IMAP server answered, but refused the command.
    pub fn imap(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_GATEWAY, "imap_error", message)
    }

    /// The IMAP server or the local LLM could not be reached, or login failed.
    pub fn unavailable(message: impl Into<String>) -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, "unavailable", message)
    }

    pub fn unauthorized() -> Self {
        Self::new(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "missing or wrong token",
        )
    }

    /// A request to the web interface relay without a valid session. The usual cause is a
    /// browser window left over from before a restart, which is what the message explains.
    pub fn no_session() -> Self {
        Self::new(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "This window is not connected to the app any more, probably because the app was \
             restarted. Open the address that `triage up` printed when it started.",
        )
    }

    pub fn body(&self) -> serde_json::Value {
        json!({"error": {"code": self.code, "message": self.message}})
    }
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for ApiError {}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(self.body())).into_response()
    }
}
