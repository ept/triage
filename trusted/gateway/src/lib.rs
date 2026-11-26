//! The gateway: the email sandbox's only network peer. See `docs/gateway-api.md`.

pub mod config;
pub mod draft;
pub mod error;
pub mod imap;
pub mod oauth;
pub mod raw;
pub mod rules;
