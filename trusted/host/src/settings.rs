//! The host tool's own files in the data directory (normally `~/.triage`).
//!
//! * `host.json`: settings chosen by the user (where the repository is, ports, granted
//!   directories).
//! * `state.json`: which app image is deployed and which one last worked.
//! * `session.json`: the secrets of the running gateway, readable by the user only.

use std::path::{Path, PathBuf};

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};

use crate::sandbox::{Grant, Layout};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct HostConfig {
    /// Working tree of the app's repository; mounted into the coding sandbox.
    pub repo: PathBuf,
    #[serde(default = "default_control_port")]
    pub control_port: u16,
    #[serde(default = "default_ui_port")]
    pub ui_port: u16,
    /// Port of the local LLM server on this machine.
    #[serde(default)]
    pub llm_port: Option<u16>,
    /// Model for the coding agent; the agent's default if not set.
    #[serde(default)]
    pub coding_model: Option<String>,
    #[serde(default)]
    pub grants: Vec<Grant>,
}

fn default_control_port() -> u16 {
    8101
}

fn default_ui_port() -> u16 {
    8102
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct State {
    /// Number of the most recently built app image.
    #[serde(default)]
    pub generation: u64,
    /// The image that is running, or should be.
    #[serde(default)]
    pub current_image: Option<String>,
    /// Git tree of the `app` directory from which the current image was built.
    #[serde(default)]
    pub current_tree: Option<String>,
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Session {
    pub control_token: String,
    pub ui_token: String,
}

/// The data directory: `TRIAGE_HOME`, or `~/.triage`.
pub fn data_dir() -> Result<PathBuf, String> {
    if let Some(dir) = std::env::var_os("TRIAGE_HOME") {
        return Ok(PathBuf::from(dir));
    }
    let home = std::env::var_os("HOME").ok_or("HOME is not set")?;
    Ok(PathBuf::from(home).join(".triage"))
}

pub fn read<T: DeserializeOwned>(path: &Path) -> Result<Option<T>, String> {
    match std::fs::read_to_string(path) {
        Ok(text) => serde_json::from_str(&text)
            .map(Some)
            .map_err(|e| format!("{}: {e}", path.display())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

/// Writes a file that only the user can read, replacing it atomically.
pub fn write<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let describe = |e: std::io::Error| format!("{}: {e}", path.display());
    let temporary = path.with_extension("tmp");
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&temporary)
        .map_err(describe)?;
    let text = serde_json::to_string_pretty(value).expect("serializable");
    file.write_all(text.as_bytes()).map_err(describe)?;
    file.write_all(b"\n").map_err(describe)?;
    file.sync_all().map_err(describe)?;
    std::fs::rename(&temporary, path).map_err(describe)
}

/// Creates the data directory and its subdirectories, accessible to the user only.
pub fn create_directories(data: &Path) -> Result<(), String> {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    for dir in [
        data.to_path_buf(),
        data.join("cache"),
        data.join("config"),
        data.join("gateway"),
    ] {
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&dir)
            .map_err(|e| format!("{}: {e}", dir.display()))?;
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700))
            .map_err(|e| format!("{}: {e}", dir.display()))?;
    }
    Ok(())
}

pub fn layout(data: &Path, config: &HostConfig) -> Layout {
    // SAFETY: getuid and getgid have no preconditions and cannot fail.
    let (uid, gid) = unsafe { (libc::getuid(), libc::getgid()) };
    Layout {
        data: data.to_path_buf(),
        repo: config.repo.clone(),
        uid,
        gid,
        control_port: config.control_port,
        ui_port: config.ui_port,
        llm_port: config.llm_port,
        grants: config.grants.clone(),
    }
}

/// A fresh random secret, as 64 hexadecimal characters.
pub fn random_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| format!("no source of randomness: {e}"))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn files_are_private_and_round_trip() {
        let dir = tempfile::tempdir().unwrap();
        let data = dir.path().join("triage");
        create_directories(&data).unwrap();
        for sub in ["", "cache", "config", "gateway"] {
            let mode = std::fs::metadata(data.join(sub))
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o777, 0o700, "{sub}");
        }
        let path = data.join("state.json");
        assert_eq!(read::<State>(&path).unwrap(), None);
        let state = State {
            generation: 3,
            current_image: Some("triage-app:3".into()),
            current_tree: Some("abc".into()),
        };
        write(&path, &state).unwrap();
        assert_eq!(read::<State>(&path).unwrap(), Some(state));
        let mode = std::fs::metadata(&path).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }

    #[test]
    fn host_config_defaults() {
        let config: HostConfig = serde_json::from_str(r#"{"repo": "/src/triage"}"#).unwrap();
        assert_eq!((config.control_port, config.ui_port), (8101, 8102));
        assert_eq!(config.llm_port, None);
        assert!(config.grants.is_empty());
    }

    #[test]
    fn tokens_are_long_and_different() {
        let a = random_token().unwrap();
        let b = random_token().unwrap();
        assert_eq!(a.len(), 64);
        assert_ne!(a, b);
    }
}
