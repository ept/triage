//! The gateway's state: accounts, the operation log, change requests, and notifications.

use std::collections::{HashMap, VecDeque};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use chrono::{SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::sync::Notify;

use crate::config::{AccountsConfig, LlmConfig, Secret};
use crate::error::{ApiError, ApiResult};
use crate::imap::{self, Account};
use crate::rules;

fn now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

// ---------------------------------------------------------------------------------------------
// Settings

/// Settings that are fixed when the gateway starts. They come from the wrapper app.
#[derive(Clone, Debug)]
pub struct Settings {
    /// Directory in which the operation log is kept.
    pub data_dir: PathBuf,
    /// Secret for the control API.
    pub control_token: Secret,
    /// Secret with which the wrapper app's web view obtains its session cookie.
    pub ui_token: Secret,
    /// Where the app's web server listens, e.g. `http://app:8080`.
    pub app_url: String,
    /// Accept accounts without TLS (for tests against a local server).
    pub allow_insecure_imap: bool,
    /// Moves to the trash or junk folder per account and hour above which the user is warned.
    pub deletion_limit: u32,
}

// ---------------------------------------------------------------------------------------------
// Event logs with long polling

#[derive(Clone, Debug, Serialize)]
pub struct Event {
    pub seq: u64,
    pub time: String,
    #[serde(flatten)]
    pub data: Value,
}

/// A bounded in-memory list of events that readers can wait on.
pub struct EventLog {
    capacity: usize,
    inner: Mutex<(u64, VecDeque<Event>)>,
    notify: Notify,
}

impl EventLog {
    pub fn new(capacity: usize) -> Self {
        EventLog {
            capacity,
            inner: Mutex::new((0, VecDeque::new())),
            notify: Notify::new(),
        }
    }

    /// Appends an event; `data` must be a JSON object.
    pub fn push(&self, data: Value) -> u64 {
        let seq = {
            let mut inner = self.inner.lock().expect("event log lock");
            inner.0 += 1;
            let seq = inner.0;
            inner.1.push_back(Event {
                seq,
                time: now(),
                data,
            });
            while inner.1.len() > self.capacity {
                inner.1.pop_front();
            }
            seq
        };
        self.notify.notify_waiters();
        seq
    }

    fn snapshot(&self, after: u64) -> (Vec<Event>, u64) {
        let inner = self.inner.lock().expect("event log lock");
        let events = inner.1.iter().filter(|e| e.seq > after).cloned().collect();
        (events, inner.0)
    }

    /// Returns the events after `after`, waiting up to `wait` for one to arrive.
    pub async fn read(&self, after: u64, wait: Duration) -> (Vec<Event>, u64) {
        let deadline = tokio::time::Instant::now() + wait;
        loop {
            let notified = self.notify.notified();
            tokio::pin!(notified);
            // Register interest before looking, so that no event is missed in between.
            notified.as_mut().enable();
            let (events, last) = self.snapshot(after);
            if !events.is_empty() || tokio::time::Instant::now() >= deadline {
                return (events, last);
            }
            if tokio::time::timeout_at(deadline, notified).await.is_err() {
                return self.snapshot(after);
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Operation log

/// The append-only record of every change made to a mailbox through the gateway. It lives in
/// the gateway's own data directory, which the email sandbox cannot write to.
pub struct OpLog {
    path: PathBuf,
    inner: Mutex<(File, u64)>,
}

impl OpLog {
    pub fn open(dir: &Path) -> std::io::Result<OpLog> {
        std::fs::create_dir_all(dir)?;
        let path = dir.join("operations.jsonl");
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        let mut last_seq = 0;
        for line in BufReader::new(File::open(&path)?).lines() {
            if let Ok(entry) = serde_json::from_str::<Value>(&line?)
                && let Some(seq) = entry.get("seq").and_then(Value::as_u64)
            {
                last_seq = last_seq.max(seq);
            }
        }
        Ok(OpLog {
            path,
            inner: Mutex::new((file, last_seq)),
        })
    }

    /// Appends an entry; `fields` must be a JSON object without `seq` and `time`.
    pub fn append(&self, account: &str, op: &str, fields: Value) -> std::io::Result<u64> {
        let mut inner = self.inner.lock().expect("operation log lock");
        let seq = inner.1 + 1;
        let mut entry = json!({"seq": seq, "time": now(), "account": account, "op": op});
        if let (Some(entry), Value::Object(fields)) = (entry.as_object_mut(), fields) {
            entry.extend(fields);
        }
        let mut line = serde_json::to_vec(&entry)?;
        line.push(b'\n');
        inner.0.write_all(&line)?;
        inner.0.sync_data()?;
        inner.1 = seq;
        Ok(seq)
    }

    pub fn read(&self, after: u64, limit: usize) -> std::io::Result<(Vec<Value>, u64)> {
        let last_seq = self.inner.lock().expect("operation log lock").1;
        let mut entries = Vec::new();
        for line in BufReader::new(File::open(&self.path)?).lines() {
            let Ok(entry) = serde_json::from_str::<Value>(&line?) else {
                continue;
            };
            let seq = entry.get("seq").and_then(Value::as_u64).unwrap_or(0);
            if seq > after && seq <= last_seq {
                entries.push(entry);
                if entries.len() >= limit {
                    break;
                }
            }
        }
        Ok((entries, last_seq))
    }
}

// ---------------------------------------------------------------------------------------------
// Change requests

pub const MAX_PENDING_CHANGE_REQUESTS: usize = 5;
const CHANGE_REQUEST_LIFETIME: Duration = Duration::from_secs(24 * 3600);
const MAX_REMEMBERED_CHANGE_REQUESTS: usize = 200;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ChangeRequestKind {
    Feature,
    Bug,
    Reply,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ChangeRequestState {
    Pending,
    Approved,
    Rejected,
    Expired,
}

/// A message that the app wants to send to the coding agent. It goes nowhere until the user has
/// approved it in the wrapper app.
#[derive(Clone, Debug, Serialize)]
pub struct ChangeRequest {
    pub id: String,
    pub kind: ChangeRequestKind,
    pub state: ChangeRequestState,
    pub time: String,
    /// Only shown on the control API.
    pub text: String,
    #[serde(skip)]
    created: Instant,
}

#[derive(Default)]
struct ChangeRequests {
    order: VecDeque<String>,
    by_id: HashMap<String, ChangeRequest>,
}

impl ChangeRequests {
    fn expire(&mut self) {
        for request in self.by_id.values_mut() {
            if request.state == ChangeRequestState::Pending
                && request.created.elapsed() > CHANGE_REQUEST_LIFETIME
            {
                request.state = ChangeRequestState::Expired;
            }
        }
        while self.order.len() > MAX_REMEMBERED_CHANGE_REQUESTS {
            // Forget the oldest request that is no longer pending.
            let Some(position) = self.order.iter().position(|id| {
                self.by_id
                    .get(id)
                    .is_none_or(|r| r.state != ChangeRequestState::Pending)
            }) else {
                break;
            };
            if let Some(id) = self.order.remove(position) {
                self.by_id.remove(&id);
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Deletion rate

const DELETION_WINDOW: Duration = Duration::from_secs(3600);

#[derive(Clone, Debug, Serialize)]
pub struct DeletionWarning {
    pub time: String,
    pub account: String,
    pub count: u32,
    pub window_minutes: u32,
}

#[derive(Default)]
struct Deletions {
    recent: VecDeque<(Instant, String, u32)>,
    /// Per account: when the user was last warned.
    warned: HashMap<String, Instant>,
    warnings: VecDeque<DeletionWarning>,
}

// ---------------------------------------------------------------------------------------------
// The gateway

pub struct Gateway {
    pub settings: Settings,
    pub tls: Arc<rustls::ClientConfig>,
    pub http: reqwest::Client,
    accounts: RwLock<Vec<Arc<Account>>>,
    llm: RwLock<Option<LlmConfig>>,
    pub oplog: OpLog,
    /// Notifications for the app.
    pub events: EventLog,
    /// Output of the coding agent, for display in the app.
    pub coding_events: EventLog,
    change_requests: Mutex<ChangeRequests>,
    deletions: Mutex<Deletions>,
}

impl Gateway {
    pub fn new(settings: Settings) -> std::io::Result<Gateway> {
        let tls = imap::tls_config();
        let http = imap::http_client(&tls);
        let oplog = OpLog::open(&settings.data_dir)?;
        Ok(Gateway {
            settings,
            tls,
            http,
            accounts: RwLock::new(Vec::new()),
            llm: RwLock::new(None),
            oplog,
            events: EventLog::new(1000),
            coding_events: EventLog::new(5000),
            change_requests: Mutex::new(ChangeRequests::default()),
            deletions: Mutex::new(Deletions::default()),
        })
    }

    // ----- accounts

    /// Replaces the account configuration. Accounts whose configuration is unchanged keep
    /// their connection.
    pub fn set_accounts(&self, config: AccountsConfig) -> ApiResult<()> {
        config.validate(self.settings.allow_insecure_imap)?;
        let mut accounts = self.accounts.write().expect("accounts lock");
        let updated = config
            .accounts
            .into_iter()
            .map(
                |new| match accounts.iter().find(|existing| existing.config == new) {
                    Some(existing) => existing.clone(),
                    None => Arc::new(Account::new(new, self.tls.clone(), self.http.clone())),
                },
            )
            .collect();
        *accounts = updated;
        drop(accounts);
        self.events.push(json!({"kind": "accounts_changed"}));
        Ok(())
    }

    pub fn accounts(&self) -> Vec<Arc<Account>> {
        self.accounts.read().expect("accounts lock").clone()
    }

    pub fn account(&self, id: &str) -> ApiResult<Arc<Account>> {
        self.accounts
            .read()
            .expect("accounts lock")
            .iter()
            .find(|account| account.config.id == id)
            .cloned()
            .ok_or_else(|| ApiError::not_found("no such account"))
    }

    // ----- local LLM

    pub fn set_llm(&self, config: Option<LlmConfig>) -> ApiResult<()> {
        if let Some(config) = &config {
            config.validate()?;
        }
        *self.llm.write().expect("llm lock") = config;
        Ok(())
    }

    pub fn llm(&self) -> Option<LlmConfig> {
        self.llm.read().expect("llm lock").clone()
    }

    // ----- operation log

    /// Records a change to a mailbox. `outcome` is the result of the operation; failures of the
    /// IMAP server are recorded too, because the change may have been applied in part.
    pub fn record<T>(&self, account: &str, op: &str, mut fields: Value, outcome: &ApiResult<T>) {
        match outcome {
            Ok(_) => {}
            Err(error) if matches!(error.code, "imap_error" | "unavailable") => {
                fields["error"] = Value::String(error.message.clone());
            }
            // Requests that were rejected before reaching the server changed nothing.
            Err(_) => return,
        }
        if let Err(error) = self.oplog.append(account, op, fields) {
            tracing::error!("could not write to the operation log: {}", error.kind());
        }
    }

    // ----- change requests

    pub fn submit_change_request(
        &self,
        kind: ChangeRequestKind,
        text: &str,
    ) -> ApiResult<ChangeRequest> {
        let text = rules::validate_change_request_text(text)?;
        let mut requests = self.change_requests.lock().expect("change requests lock");
        requests.expire();
        let pending = requests
            .by_id
            .values()
            .filter(|r| r.state == ChangeRequestState::Pending)
            .count();
        if pending >= MAX_PENDING_CHANGE_REQUESTS {
            return Err(ApiError::forbidden(
                "too many change requests are waiting for the user's approval",
            ));
        }
        let request = ChangeRequest {
            id: format!("cr-{}", uuid::Uuid::new_v4().simple()),
            kind,
            state: ChangeRequestState::Pending,
            time: now(),
            text,
            created: Instant::now(),
        };
        requests.order.push_back(request.id.clone());
        requests.by_id.insert(request.id.clone(), request.clone());
        Ok(request)
    }

    pub fn change_request(&self, id: &str) -> ApiResult<ChangeRequest> {
        let mut requests = self.change_requests.lock().expect("change requests lock");
        requests.expire();
        requests
            .by_id
            .get(id)
            .cloned()
            .ok_or_else(|| ApiError::not_found("no such change request"))
    }

    pub fn change_requests(&self, state: Option<ChangeRequestState>) -> Vec<ChangeRequest> {
        let mut requests = self.change_requests.lock().expect("change requests lock");
        requests.expire();
        requests
            .order
            .iter()
            .filter_map(|id| requests.by_id.get(id))
            .filter(|r| state.is_none_or(|s| r.state == s))
            .cloned()
            .collect()
    }

    /// Records the user's decision on a pending change request.
    pub fn decide_change_request(&self, id: &str, approve: bool) -> ApiResult<ChangeRequest> {
        let decided = {
            let mut requests = self.change_requests.lock().expect("change requests lock");
            requests.expire();
            let request = requests
                .by_id
                .get_mut(id)
                .ok_or_else(|| ApiError::not_found("no such change request"))?;
            if request.state != ChangeRequestState::Pending {
                return Err(ApiError::bad_request(
                    "the change request is no longer pending",
                ));
            }
            request.state = if approve {
                ChangeRequestState::Approved
            } else {
                ChangeRequestState::Rejected
            };
            request.clone()
        };
        self.events.push(json!({
            "kind": "change_request",
            "id": decided.id,
            "state": decided.state,
        }));
        Ok(decided)
    }

    // ----- deletion rate

    /// Counts messages moved to the trash or junk folder. If the account exceeds the limit, a
    /// warning is raised (at most once per window).
    pub fn count_deletions(&self, account: &str, count: u32) {
        if count == 0 {
            return;
        }
        let warning = {
            let mut deletions = self.deletions.lock().expect("deletions lock");
            let now_instant = Instant::now();
            deletions
                .recent
                .push_back((now_instant, account.to_string(), count));
            while deletions
                .recent
                .front()
                .is_some_and(|(at, _, _)| at.elapsed() > DELETION_WINDOW)
            {
                deletions.recent.pop_front();
            }
            let total: u32 = deletions
                .recent
                .iter()
                .filter(|(_, a, _)| a == account)
                .map(|(_, _, n)| n)
                .sum();
            let recently_warned = deletions
                .warned
                .get(account)
                .is_some_and(|at| at.elapsed() < DELETION_WINDOW);
            if total > self.settings.deletion_limit && !recently_warned {
                deletions.warned.insert(account.to_string(), now_instant);
                let warning = DeletionWarning {
                    time: now(),
                    account: account.to_string(),
                    count: total,
                    window_minutes: (DELETION_WINDOW.as_secs() / 60) as u32,
                };
                deletions.warnings.push_back(warning.clone());
                while deletions.warnings.len() > 20 {
                    deletions.warnings.pop_front();
                }
                Some(warning)
            } else {
                None
            }
        };
        if let Some(warning) = warning {
            tracing::warn!(
                "account {}: {} messages deleted within an hour",
                warning.account,
                warning.count
            );
            self.events.push(json!({
                "kind": "deletion_rate",
                "account": warning.account,
                "count": warning.count,
                "window_minutes": warning.window_minutes,
            }));
        }
    }

    pub fn deletion_warnings(&self) -> Vec<DeletionWarning> {
        let deletions = self.deletions.lock().expect("deletions lock");
        deletions.warnings.iter().cloned().collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gateway(dir: &Path) -> Gateway {
        Gateway::new(Settings {
            data_dir: dir.to_path_buf(),
            control_token: Secret::new("c".repeat(32)),
            ui_token: Secret::new("u".repeat(32)),
            app_url: "http://127.0.0.1:1".into(),
            allow_insecure_imap: false,
            deletion_limit: 3,
        })
        .unwrap()
    }

    #[test]
    fn operation_log_survives_restarts_and_keeps_counting() {
        let dir = tempfile::tempdir().unwrap();
        let log = OpLog::open(dir.path()).unwrap();
        assert_eq!(log.append("a", "flags", json!({"uids": [1]})).unwrap(), 1);
        assert_eq!(log.append("a", "move", json!({"uids": [2]})).unwrap(), 2);
        drop(log);

        let log = OpLog::open(dir.path()).unwrap();
        assert_eq!(log.append("b", "draft", json!({})).unwrap(), 3);
        let (entries, last) = log.read(1, 10).unwrap();
        assert_eq!(last, 3);
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0]["seq"], 2);
        assert_eq!(entries[0]["op"], "move");
        assert_eq!(entries[0]["account"], "a");
        assert_eq!(entries[0]["uids"], json!([2]));
        assert!(entries[0]["time"].is_string());
        assert_eq!(entries[1]["account"], "b");
        let (limited, _) = log.read(0, 2).unwrap();
        assert_eq!(limited.len(), 2);
    }

    #[test]
    fn only_operations_that_may_have_changed_something_are_recorded() {
        let dir = tempfile::tempdir().unwrap();
        let gateway = gateway(dir.path());
        gateway.record("a", "flags", json!({"n": 1}), &Ok(()));
        gateway.record::<()>(
            "a",
            "flags",
            json!({"n": 2}),
            &Err(ApiError::forbidden("no")),
        );
        gateway.record::<()>(
            "a",
            "flags",
            json!({"n": 3}),
            &Err(ApiError::imap("server said no")),
        );
        let (entries, _) = gateway.oplog.read(0, 10).unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0]["n"], 1);
        assert!(entries[0].get("error").is_none());
        assert_eq!(entries[1]["n"], 3);
        assert_eq!(entries[1]["error"], "server said no");
    }

    #[tokio::test]
    async fn event_log_long_polling() {
        let log = Arc::new(EventLog::new(3));
        let (events, last) = log.read(0, Duration::from_millis(10)).await;
        assert!(events.is_empty());
        assert_eq!(last, 0);

        let waiter = {
            let log = log.clone();
            tokio::spawn(async move { log.read(0, Duration::from_secs(5)).await })
        };
        tokio::time::sleep(Duration::from_millis(20)).await;
        log.push(json!({"kind": "x"}));
        let (events, last) = waiter.await.unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(last, 1);
        assert_eq!(serde_json::to_value(&events[0]).unwrap()["kind"], "x");

        for _ in 0..5 {
            log.push(json!({"kind": "y"}));
        }
        let (events, last) = log.read(1, Duration::ZERO).await;
        assert_eq!(last, 6);
        assert_eq!(
            events.iter().map(|e| e.seq).collect::<Vec<_>>(),
            vec![4, 5, 6]
        );
    }

    #[test]
    fn change_request_lifecycle() {
        let dir = tempfile::tempdir().unwrap();
        let gateway = gateway(dir.path());
        let request = gateway
            .submit_change_request(ChangeRequestKind::Feature, "Please add a button")
            .unwrap();
        assert_eq!(request.state, ChangeRequestState::Pending);
        assert_eq!(
            gateway
                .change_requests(Some(ChangeRequestState::Pending))
                .len(),
            1
        );

        let decided = gateway.decide_change_request(&request.id, true).unwrap();
        assert_eq!(decided.state, ChangeRequestState::Approved);
        assert_eq!(decided.text, "Please add a button");
        assert!(gateway.decide_change_request(&request.id, false).is_err());
        assert!(gateway.decide_change_request("cr-unknown", true).is_err());
        assert_eq!(
            gateway.change_request(&request.id).unwrap().state,
            ChangeRequestState::Approved
        );

        for i in 0..MAX_PENDING_CHANGE_REQUESTS {
            gateway
                .submit_change_request(ChangeRequestKind::Bug, &format!("bug {i}"))
                .unwrap();
        }
        let error = gateway
            .submit_change_request(ChangeRequestKind::Bug, "one too many")
            .unwrap_err();
        assert_eq!(error.code, "forbidden");
        assert!(
            gateway
                .submit_change_request(ChangeRequestKind::Bug, "hid\u{200b}den")
                .is_err()
        );
    }

    #[tokio::test]
    async fn deletion_rate_warning_is_raised_once() {
        let dir = tempfile::tempdir().unwrap();
        let gateway = gateway(dir.path());
        gateway.count_deletions("a", 2);
        gateway.count_deletions("b", 3);
        assert!(gateway.deletion_warnings().is_empty());
        gateway.count_deletions("a", 2);
        let warnings = gateway.deletion_warnings();
        assert_eq!(warnings.len(), 1);
        assert_eq!(warnings[0].account, "a");
        assert_eq!(warnings[0].count, 4);
        gateway.count_deletions("a", 5);
        assert_eq!(
            gateway.deletion_warnings().len(),
            1,
            "one warning per window"
        );
        let (events, _) = gateway.events.read(0, Duration::ZERO).await;
        let kinds: Vec<_> = events
            .iter()
            .map(|e| e.data["kind"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(kinds, vec!["deletion_rate"]);
    }
}
