//! Relaying HTTP requests: from the email sandbox to the local LLM, and from the wrapper app's
//! web view to the app's web server in the email sandbox.

use axum::body::Body;
use axum::http::{HeaderMap, HeaderName, HeaderValue, Request, Response};
use futures_util::TryStreamExt;

use crate::error::{ApiError, ApiResult};

/// Headers that describe a single connection and must not be passed on.
const HOP_BY_HOP: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
];

fn copy_headers(from: &HeaderMap, to: &mut HeaderMap, drop: &[&str]) {
    for (name, value) in from {
        let lower = name.as_str();
        if HOP_BY_HOP.contains(&lower) || drop.contains(&lower) {
            continue;
        }
        to.append(name.clone(), value.clone());
    }
}

/// Sends `request` to `url` and returns the upstream response, streaming both bodies.
///
/// `drop_request_headers` are removed from the request (in addition to hop-by-hop headers), and
/// `add_request_headers` are added to it.
pub async fn forward(
    client: &reqwest::Client,
    url: &str,
    request: Request<Body>,
    drop_request_headers: &[&str],
    add_request_headers: Vec<(HeaderName, HeaderValue)>,
) -> ApiResult<Response<Body>> {
    let (parts, body) = request.into_parts();
    let mut headers = HeaderMap::new();
    copy_headers(&parts.headers, &mut headers, drop_request_headers);
    for (name, value) in add_request_headers {
        headers.insert(name, value);
    }
    let upstream = client
        .request(parts.method, url)
        .headers(headers)
        .body(reqwest::Body::wrap_stream(body.into_data_stream()))
        .send()
        .await
        .map_err(|error| {
            ApiError::unavailable(if error.is_connect() {
                "could not connect to the upstream server"
            } else if error.is_timeout() {
                "the upstream server did not answer in time"
            } else {
                "the request to the upstream server failed"
            })
        })?;

    let mut response = Response::builder().status(upstream.status());
    if let Some(headers) = response.headers_mut() {
        copy_headers(upstream.headers(), headers, &[]);
    }
    let body = Body::from_stream(upstream.bytes_stream().map_err(std::io::Error::other));
    response
        .body(body)
        .map_err(|_| ApiError::unavailable("the upstream server sent an invalid response"))
}

/// Joins a base URL and a request path (with query string).
pub fn join_url(base: &str, path_and_query: &str) -> String {
    format!(
        "{}/{}",
        base.trim_end_matches('/'),
        path_and_query.trim_start_matches('/')
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn joins_urls() {
        assert_eq!(join_url("http://a:1/", "/v1/x?y=1"), "http://a:1/v1/x?y=1");
        assert_eq!(join_url("http://a:1", "v1/x"), "http://a:1/v1/x");
        assert_eq!(join_url("http://a:1/base", "/"), "http://a:1/base/");
    }

    #[test]
    fn connection_headers_are_not_copied() {
        let mut from = HeaderMap::new();
        from.insert("connection", HeaderValue::from_static("upgrade"));
        from.insert("upgrade", HeaderValue::from_static("websocket"));
        from.insert("host", HeaderValue::from_static("gateway"));
        from.insert("cookie", HeaderValue::from_static("a=b"));
        from.insert("accept", HeaderValue::from_static("*/*"));
        let mut to = HeaderMap::new();
        copy_headers(&from, &mut to, &["cookie"]);
        assert_eq!(to.len(), 1);
        assert!(to.contains_key("accept"));
    }
}
