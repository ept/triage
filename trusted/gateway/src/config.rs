//! Configuration of the gateway: IMAP accounts (including credentials) and the local LLM.
//!
//! Accounts are configured by the user through the wrapper app, never by the email sandbox. The
//! gateway receives them over the control API (or, for development, from a file) and keeps the
//! credentials only in memory.

use std::fmt;

use serde::Deserialize;

use crate::error::{ApiError, ApiResult};
use crate::rules;

/// A string that must not end up in logs or error messages.
#[derive(Clone, Deserialize, PartialEq, Eq)]
#[serde(transparent)]
pub struct Secret(String);

impl Secret {
    pub fn new(value: impl Into<String>) -> Self {
        Secret(value.into())
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret(***)")
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Security {
    /// TLS from the first byte (usually port 993).
    #[default]
    Tls,
    /// Plain connection upgraded with STARTTLS before login (usually port 143).
    Starttls,
    /// No encryption. Only accepted when the gateway was started with
    /// `TRIAGE_ALLOW_INSECURE_IMAP=1`, which is meant for tests against a local server.
    Insecure,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Auth {
    Password {
        password: Secret,
    },
    /// OAuth 2 with the SASL XOAUTH2 mechanism. The interactive consent happens in the wrapper
    /// app; the gateway only exchanges the refresh token for access tokens.
    Oauth2 {
        token_url: String,
        client_id: String,
        #[serde(default)]
        client_secret: Option<Secret>,
        refresh_token: Secret,
    },
}

/// Lets the user name folders whose role the server does not announce, and mark additional
/// folders into which the app must not be able to move messages.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FolderOverrides {
    #[serde(default)]
    pub drafts: Option<String>,
    #[serde(default)]
    pub sent: Option<String>,
    #[serde(default)]
    pub trash: Option<String>,
    #[serde(default)]
    pub junk: Option<String>,
    #[serde(default)]
    pub archive: Option<String>,
    #[serde(default)]
    pub protected: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AccountConfig {
    /// Short stable identifier, `[a-z0-9-]{1,32}`.
    pub id: String,
    /// The user's email address at this account; used as the From address of drafts.
    pub address: String,
    #[serde(default)]
    pub display_name: Option<String>,
    pub host: String,
    pub port: u16,
    #[serde(default)]
    pub security: Security,
    pub username: String,
    pub auth: Auth,
    #[serde(default)]
    pub folders: FolderOverrides,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AccountsConfig {
    pub accounts: Vec<AccountConfig>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LlmConfig {
    /// Base URL of the local LLM's OpenAI-compatible server, e.g.
    /// `http://host.docker.internal:8012`.
    pub base_url: String,
    #[serde(default)]
    pub api_key: Option<Secret>,
}

impl AccountsConfig {
    pub fn validate(&self, allow_insecure: bool) -> ApiResult<()> {
        let mut seen = std::collections::HashSet::new();
        for account in &self.accounts {
            account.validate(allow_insecure)?;
            if !seen.insert(account.id.as_str()) {
                return Err(ApiError::bad_request(format!(
                    "duplicate account id {:?}",
                    account.id
                )));
            }
        }
        Ok(())
    }
}

impl AccountConfig {
    pub fn validate(&self, allow_insecure: bool) -> ApiResult<()> {
        rules::validate_account_id(&self.id)?;
        rules::validate_address(&self.address)?;
        if let Some(name) = &self.display_name {
            rules::validate_display_name(name)?;
        }
        rules::validate_hostname(&self.host)?;
        if self.port == 0 {
            return Err(ApiError::bad_request("port must not be 0"));
        }
        if self.security == Security::Insecure && !allow_insecure {
            return Err(ApiError::bad_request(
                "unencrypted IMAP connections are not allowed",
            ));
        }
        if self.username.is_empty() || self.username.chars().any(|c| c.is_control()) {
            return Err(ApiError::bad_request("invalid username"));
        }
        match &self.auth {
            Auth::Password { password } => {
                if password.expose().is_empty()
                    || password.expose().chars().any(|c| c == '\r' || c == '\n')
                {
                    return Err(ApiError::bad_request("invalid password"));
                }
            }
            Auth::Oauth2 { token_url, .. } => {
                if !token_url.starts_with("https://")
                    && !(allow_insecure && token_url.starts_with("http://"))
                {
                    return Err(ApiError::bad_request("token_url must be an https URL"));
                }
            }
        }
        Ok(())
    }
}

impl LlmConfig {
    pub fn validate(&self) -> ApiResult<()> {
        let url = self.base_url.trim_end_matches('/');
        let rest = url
            .strip_prefix("http://")
            .or_else(|| url.strip_prefix("https://"))
            .ok_or_else(|| ApiError::bad_request("base_url must be an http or https URL"))?;
        if rest.is_empty() || rest.contains(['?', '#', ' ', '@']) {
            return Err(ApiError::bad_request("invalid base_url"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> AccountConfig {
        serde_json::from_value(serde_json::json!({
            "id": "work",
            "address": "user@example.org",
            "host": "imap.example.org",
            "port": 993,
            "username": "user@example.org",
            "auth": {"type": "password", "password": "hunter2"}
        }))
        .unwrap()
    }

    #[test]
    fn accepts_a_minimal_account() {
        let account = sample();
        assert_eq!(account.security, Security::Tls);
        account.validate(false).unwrap();
    }

    #[test]
    fn secrets_are_not_printed() {
        let printed = format!("{:?}", sample());
        assert!(!printed.contains("hunter2"), "{printed}");
    }

    #[test]
    fn insecure_needs_explicit_permission() {
        let mut account = sample();
        account.security = Security::Insecure;
        assert!(account.validate(false).is_err());
        account.validate(true).unwrap();
    }

    #[test]
    fn rejects_bad_fields() {
        let mut account = sample();
        account.id = "Work Account".into();
        assert!(account.validate(false).is_err());

        let mut account = sample();
        account.host = "imap.example.org/evil".into();
        assert!(account.validate(false).is_err());

        let mut account = sample();
        account.auth = Auth::Oauth2 {
            token_url: "http://example.org/token".into(),
            client_id: "x".into(),
            client_secret: None,
            refresh_token: Secret::new("r"),
        };
        assert!(account.validate(false).is_err());
    }

    #[test]
    fn rejects_duplicate_ids_and_unknown_fields() {
        let config = AccountsConfig {
            accounts: vec![sample(), sample()],
        };
        assert!(config.validate(false).is_err());

        let unknown: Result<AccountConfig, _> = serde_json::from_value(serde_json::json!({
            "id": "work", "address": "user@example.org", "host": "h", "port": 1,
            "username": "u", "auth": {"type": "password", "password": "p"}, "smtp": true
        }));
        assert!(unknown.is_err());
    }

    #[test]
    fn llm_url_validation() {
        let ok = LlmConfig {
            base_url: "http://host.docker.internal:8012".into(),
            api_key: None,
        };
        ok.validate().unwrap();
        for bad in ["ftp://x", "http://", "http://user@host", "http://h/?a=b"] {
            let config = LlmConfig {
                base_url: bad.into(),
                api_key: None,
            };
            assert!(config.validate().is_err(), "{bad}");
        }
    }
}
