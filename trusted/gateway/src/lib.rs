//! The gateway: the email sandbox's only network peer. See `docs/gateway-api.md`.

pub mod api;
pub mod config;
pub mod draft;
pub mod error;
pub mod host;
pub mod imap;
pub mod oauth;
pub mod raw;
pub mod relay;
pub mod rules;
pub mod state;
