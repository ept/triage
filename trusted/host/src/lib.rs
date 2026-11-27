//! The host side of the trusted base: creating the sandboxes, holding the secrets, asking the
//! user for approval, and running the coding agent. `triage` (see `main.rs`) is its command
//! line; the wrapper app will be built on the same library.

pub mod agent;
pub mod control;
pub mod deploy;
pub mod docker;
pub mod run;
pub mod sandbox;
pub mod secrets;
pub mod settings;
