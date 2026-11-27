//! Where the host tool keeps secrets: IMAP credentials and API keys.
//!
//! On macOS they are stored in the user's Keychain. Elsewhere (which is only meant for
//! development) they are stored in a file that only the user can read, and only if
//! `TRIAGE_SECRETS_FILE` names one.

use std::collections::BTreeMap;
use std::path::PathBuf;

pub const ACCOUNTS: &str = "accounts";
pub const LLM_API_KEY: &str = "llm-api-key";
pub const CODING_API_KEY: &str = "coding-api-key";

pub trait SecretStore {
    fn get(&self, key: &str) -> Result<Option<String>, String>;
    fn set(&self, key: &str, value: &str) -> Result<(), String>;
    fn delete(&self, key: &str) -> Result<(), String>;
}

#[cfg(target_os = "macos")]
pub struct Keychain;

#[cfg(target_os = "macos")]
const SERVICE: &str = "email-triage";

#[cfg(target_os = "macos")]
const NOT_FOUND: i32 = -25300; // errSecItemNotFound

#[cfg(target_os = "macos")]
impl SecretStore for Keychain {
    fn get(&self, key: &str) -> Result<Option<String>, String> {
        match security_framework::passwords::get_generic_password(SERVICE, key) {
            Ok(bytes) => String::from_utf8(bytes)
                .map(Some)
                .map_err(|_| "the Keychain item is not text".to_string()),
            Err(error) if error.code() == NOT_FOUND => Ok(None),
            Err(error) => Err(format!("could not read from the Keychain: {error}")),
        }
    }

    fn set(&self, key: &str, value: &str) -> Result<(), String> {
        security_framework::passwords::set_generic_password(SERVICE, key, value.as_bytes())
            .map_err(|error| format!("could not write to the Keychain: {error}"))
    }

    fn delete(&self, key: &str) -> Result<(), String> {
        match security_framework::passwords::delete_generic_password(SERVICE, key) {
            Ok(()) => Ok(()),
            Err(error) if error.code() == NOT_FOUND => Ok(()),
            Err(error) => Err(format!("could not delete from the Keychain: {error}")),
        }
    }
}

/// Secrets in a JSON file that is readable by the user only.
pub struct FileStore {
    pub path: PathBuf,
}

impl FileStore {
    fn load(&self) -> Result<BTreeMap<String, String>, String> {
        match std::fs::read_to_string(&self.path) {
            Ok(text) => serde_json::from_str(&text)
                .map_err(|_| format!("{} is not a valid secrets file", self.path.display())),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(BTreeMap::new()),
            Err(error) => Err(format!("{}: {error}", self.path.display())),
        }
    }

    fn save(&self, secrets: &BTreeMap<String, String>) -> Result<(), String> {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let describe = |error: std::io::Error| format!("{}: {error}", self.path.display());
        let temporary = self.path.with_extension("tmp");
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&temporary)
            .map_err(describe)?;
        let text = serde_json::to_string_pretty(secrets).expect("serializable");
        file.write_all(text.as_bytes()).map_err(describe)?;
        file.sync_all().map_err(describe)?;
        std::fs::rename(&temporary, &self.path).map_err(describe)
    }
}

impl SecretStore for FileStore {
    fn get(&self, key: &str) -> Result<Option<String>, String> {
        Ok(self.load()?.get(key).cloned())
    }

    fn set(&self, key: &str, value: &str) -> Result<(), String> {
        let mut secrets = self.load()?;
        secrets.insert(key.to_string(), value.to_string());
        self.save(&secrets)
    }

    fn delete(&self, key: &str) -> Result<(), String> {
        let mut secrets = self.load()?;
        secrets.remove(key);
        self.save(&secrets)
    }
}

/// The secret store for this machine.
pub fn open() -> Result<Box<dyn SecretStore>, String> {
    if let Some(path) = std::env::var_os("TRIAGE_SECRETS_FILE") {
        return Ok(Box::new(FileStore { path: path.into() }));
    }
    #[cfg(target_os = "macos")]
    {
        Ok(Box::new(Keychain))
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err(
            "no Keychain on this system; set TRIAGE_SECRETS_FILE to store secrets in a file \
             (for development only)"
                .into(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn file_store_round_trip_with_private_permissions() {
        let dir = tempfile::tempdir().unwrap();
        let store = FileStore {
            path: dir.path().join("secrets.json"),
        };
        assert_eq!(store.get(ACCOUNTS).unwrap(), None);
        store.set(ACCOUNTS, "{\"accounts\":[]}").unwrap();
        store.set(LLM_API_KEY, "key").unwrap();
        assert_eq!(
            store.get(ACCOUNTS).unwrap().as_deref(),
            Some("{\"accounts\":[]}")
        );
        let mode = std::fs::metadata(&store.path).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
        store.delete(LLM_API_KEY).unwrap();
        store.delete("never-existed").unwrap();
        assert_eq!(store.get(LLM_API_KEY).unwrap(), None);
        assert!(store.get(ACCOUNTS).unwrap().is_some());

        std::fs::write(&store.path, "not json").unwrap();
        assert!(store.get(ACCOUNTS).is_err());
    }
}
