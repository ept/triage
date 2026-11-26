//! OAuth 2 access tokens for IMAP login (SASL XOAUTH2).
//!
//! The interactive part of OAuth (the user giving consent in a browser) is done by the wrapper
//! app, which hands the resulting refresh token to the gateway. The gateway exchanges it for
//! short-lived access tokens as needed.

use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::config::Secret;
use crate::error::{ApiError, ApiResult};

#[derive(Debug)]
pub struct AccessToken {
    pub token: Secret,
    pub expires_at: Instant,
}

impl AccessToken {
    pub fn is_fresh(&self) -> bool {
        Instant::now() + Duration::from_secs(60) < self.expires_at
    }
}

#[derive(Debug)]
pub struct Refreshed {
    pub access: AccessToken,
    /// Some providers hand out a new refresh token with every access token.
    pub refresh_token: Option<Secret>,
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    #[serde(default)]
    expires_in: Option<u64>,
    #[serde(default)]
    refresh_token: Option<String>,
}

pub async fn refresh(
    http: &reqwest::Client,
    token_url: &str,
    client_id: &str,
    client_secret: Option<&Secret>,
    refresh_token: &Secret,
) -> ApiResult<Refreshed> {
    let mut form = vec![
        ("grant_type", "refresh_token"),
        ("client_id", client_id),
        ("refresh_token", refresh_token.expose()),
    ];
    if let Some(secret) = client_secret {
        form.push(("client_secret", secret.expose()));
    }
    // Error messages deliberately leave out the response body, which may echo credentials.
    let response = http
        .post(token_url)
        .form(&form)
        .timeout(Duration::from_secs(30))
        .send()
        .await
        .map_err(|e| ApiError::unavailable(format!("OAuth token request failed: {}", kind(&e))))?;
    let status = response.status();
    if !status.is_success() {
        return Err(ApiError::unavailable(format!(
            "the OAuth token endpoint answered with status {status}; \
             the account may need to be authorised again"
        )));
    }
    let body: TokenResponse = response
        .json()
        .await
        .map_err(|_| ApiError::unavailable("the OAuth token endpoint sent an invalid response"))?;
    if body.access_token.is_empty() || body.access_token.chars().any(|c| c.is_control()) {
        return Err(ApiError::unavailable(
            "the OAuth token endpoint sent an invalid token",
        ));
    }
    Ok(Refreshed {
        access: AccessToken {
            token: Secret::new(body.access_token),
            expires_at: Instant::now() + Duration::from_secs(body.expires_in.unwrap_or(300)),
        },
        refresh_token: body.refresh_token.map(Secret::new),
    })
}

fn kind(error: &reqwest::Error) -> &'static str {
    if error.is_timeout() {
        "timed out"
    } else if error.is_connect() {
        "could not connect"
    } else {
        "request error"
    }
}

/// The client side of the XOAUTH2 mechanism.
pub struct XOAuth2 {
    user: String,
    token: Secret,
    sent: bool,
}

impl XOAuth2 {
    pub fn new(user: &str, token: Secret) -> Self {
        XOAuth2 {
            user: user.to_string(),
            token,
            sent: false,
        }
    }
}

impl async_imap::Authenticator for XOAuth2 {
    type Response = String;

    fn process(&mut self, _challenge: &[u8]) -> String {
        if self.sent {
            // The server has rejected the token and sent an error description; the protocol
            // requires an empty response before it reports the failure.
            return String::new();
        }
        self.sent = true;
        format!(
            "user={}\x01auth=Bearer {}\x01\x01",
            self.user,
            self.token.expose()
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_imap::Authenticator;

    #[test]
    fn xoauth2_initial_response() {
        let mut auth = XOAuth2::new("user@example.org", Secret::new("tok"));
        assert_eq!(
            auth.process(b""),
            "user=user@example.org\x01auth=Bearer tok\x01\x01"
        );
        assert_eq!(auth.process(b"{\"status\":\"401\"}"), "");
    }
}
