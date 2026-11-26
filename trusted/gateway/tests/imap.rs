//! The IMAP layer against a real IMAP server (Dovecot), with synthetic mail.

mod common;

use std::sync::Arc;

use axum::http::StatusCode;
use common::{Dovecot, Options, message, parse_frames};
use tokio::sync::mpsc;
use triage_gateway::config::AccountConfig;
use triage_gateway::draft::Recipient;
use triage_gateway::error::ApiError;
use triage_gateway::imap::{Account, DraftRequest, MessageRef, http_client, tls_config};
use triage_gateway::rules::{Query, Role};

fn account_from(config: AccountConfig) -> Arc<Account> {
    let tls = tls_config();
    let http = http_client(&tls);
    Arc::new(Account::new(config, tls, http))
}

fn account(server: &Dovecot, user: &str) -> Arc<Account> {
    account_from(server.account(user))
}

fn seed(server: &Dovecot, user: &str, folder: &str, count: usize) {
    for i in 1..=count {
        let text = message(
            "kim@example.net",
            &format!("{user}@example.org"),
            &format!("Message {i}"),
            &format!("This is the body of message {i}."),
        );
        server.deliver(user, folder, &text, "");
    }
}

async fn fetch_all(
    account: &Arc<Account>,
    folder: &str,
    uids: &[u32],
) -> Result<Vec<u8>, ApiError> {
    let (tx, mut rx) = mpsc::channel(4);
    let task = {
        let account = account.clone();
        let folder = folder.to_string();
        let uids = uids.to_vec();
        tokio::spawn(async move { account.fetch_messages(&folder, &uids, tx).await })
    };
    let mut data = Vec::new();
    let mut result = Ok(());
    while let Some(item) = rx.recv().await {
        match item {
            Ok(bytes) => data.extend_from_slice(&bytes),
            Err(error) => result = Err(error),
        }
    }
    task.await.unwrap();
    result.map(|()| data)
}

async fn uid_validity(account: &Account, folder: &str) -> u32 {
    account.status(folder).await.unwrap().uid_validity
}

fn recipient(address: &str) -> Recipient {
    Recipient {
        name: None,
        address: address.to_string(),
    }
}

#[tokio::test]
async fn folders_have_roles_and_shared_namespaces_are_hidden() {
    let server = Dovecot::start();
    let account = account(&server, "robin");
    let folders = account.list_folders().await.unwrap();
    let find = |name: &str| folders.iter().find(|f| f.name == name);

    assert_eq!(find("INBOX").unwrap().role, Some(Role::Inbox));
    assert_eq!(find("Drafts").unwrap().role, Some(Role::Drafts));
    assert!(find("Drafts").unwrap().protected);
    assert!(find("Sent").unwrap().protected);
    assert_eq!(find("Trash").unwrap().role, Some(Role::Trash));
    assert!(!find("Trash").unwrap().protected);
    assert_eq!(find("Archive").unwrap().role, Some(Role::Archive));
    // The server has a public namespace with a folder that this user may write to; it must be
    // invisible and unreachable.
    assert!(
        folders.iter().all(|f| !f.name.starts_with("Public")),
        "{folders:?}"
    );
    let error = account.status("Public/Announce").await.unwrap_err();
    assert_eq!(error.status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn creating_folders() {
    let server = Dovecot::start();
    let account = account(&server, "robin");

    account.create_folder("Triage/Büro & Ablage").await.unwrap();
    let folders = account.list_folders().await.unwrap();
    let created = folders
        .iter()
        .find(|f| f.name == "Triage/Büro & Ablage")
        .expect("the new folder is listed under its Unicode name");
    assert!(created.selectable && !created.protected);
    assert_eq!(
        account
            .status("Triage/Büro & Ablage")
            .await
            .unwrap()
            .messages,
        0
    );

    for (name, status) in [
        ("Triage/Büro & Ablage", StatusCode::BAD_REQUEST), // exists
        ("Outbox", StatusCode::FORBIDDEN),
        ("Triage/Drafts", StatusCode::FORBIDDEN),
        ("Public/Leak", StatusCode::FORBIDDEN),
        ("a\r\nb", StatusCode::BAD_REQUEST),
        ("INBOX", StatusCode::BAD_REQUEST),
    ] {
        let error = account.create_folder(name).await.unwrap_err();
        assert_eq!(error.status, status, "{name:?}: {error}");
    }
    assert!(!server.folder_exists("robin", "Outbox"));
}

#[tokio::test]
async fn listing_messages_in_pages() {
    let server = Dovecot::start();
    seed(&server, "robin", "INBOX", 7);
    server.deliver(
        "robin",
        "INBOX",
        &message("a@example.net", "b@example.org", "Read", "x"),
        "S",
    );
    let account = account(&server, "robin");

    let status = account.status("INBOX").await.unwrap();
    assert_eq!(status.messages, 8);
    assert_eq!(status.unseen, Some(7));
    assert_eq!(status.uid_next, Some(9));

    let first = account.list_messages("INBOX", 1, None, 3).await.unwrap();
    assert_eq!(first.uid_validity, status.uid_validity);
    let uids: Vec<u32> = first.messages.iter().map(|m| m.uid).collect();
    assert_eq!(uids.len(), 3);
    assert!(uids.windows(2).all(|w| w[0] < w[1]));
    let next = first.next_uid.expect("more messages");
    assert!(next > uids[2]);
    assert!(
        first
            .messages
            .iter()
            .all(|m| m.size > 100 && m.internal_date.is_some())
    );

    let mut all = uids.clone();
    let mut cursor = Some(next);
    while let Some(uid_min) = cursor {
        let page = account
            .list_messages("INBOX", uid_min, None, 3)
            .await
            .unwrap();
        all.extend(page.messages.iter().map(|m| m.uid));
        cursor = page.next_uid;
    }
    assert_eq!(all, (1..=8).collect::<Vec<u32>>());

    let seen: Vec<u32> = account
        .list_messages("INBOX", 1, None, 100)
        .await
        .unwrap()
        .messages
        .iter()
        .filter(|m| m.flags.contains(&"\\Seen".to_string()))
        .map(|m| m.uid)
        .collect();
    assert_eq!(seen.len(), 1);

    // A range beyond the last message is empty (`n:*` would otherwise match the last one).
    let beyond = account.list_messages("INBOX", 100, None, 10).await.unwrap();
    assert!(beyond.messages.is_empty() && beyond.next_uid.is_none());
    let bounded = account
        .list_messages("INBOX", 2, Some(4), 10)
        .await
        .unwrap();
    assert_eq!(
        bounded.messages.iter().map(|m| m.uid).collect::<Vec<_>>(),
        vec![2, 3, 4]
    );

    let empty = account.list_messages("Archive", 1, None, 10).await.unwrap();
    assert!(empty.messages.is_empty());

    assert!(account.list_messages("INBOX", 0, None, 10).await.is_err());
    assert!(
        account
            .list_messages("INBOX", 5, Some(4), 10)
            .await
            .is_err()
    );
    assert!(account.list_messages("INBOX", 1, None, 0).await.is_err());
    assert!(account.list_messages("INBOX", 1, None, 2001).await.is_err());
    assert_eq!(
        account
            .list_messages("Nope", 1, None, 10)
            .await
            .unwrap_err()
            .status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn fetching_messages_does_not_mark_them_as_read() {
    let server = Dovecot::start();
    seed(&server, "robin", "INBOX", 3);
    let big = "x".repeat(300_000);
    server.deliver(
        "robin",
        "INBOX",
        &message("kim@example.net", "robin@example.org", "Big", &big),
        "",
    );
    let account = account(&server, "robin");

    let data = fetch_all(&account, "INBOX", &[1, 3, 4, 99]).await.unwrap();
    let (frames, end) = parse_frames(&data);
    assert_eq!(end["end"], true);
    assert_eq!(end["uid_validity"], uid_validity(&account, "INBOX").await);
    let mut uids: Vec<u64> = frames
        .iter()
        .map(|f| f.header["uid"].as_u64().unwrap())
        .collect();
    uids.sort_unstable();
    assert_eq!(uids, vec![1, 3, 4], "the missing UID 99 is left out");

    let on_disk = server.messages_on_disk("robin", "INBOX");
    for frame in &frames {
        let body = String::from_utf8(frame.body.clone()).unwrap();
        assert!(
            on_disk.contains(&body),
            "bodies are passed through byte for byte"
        );
        assert!(frame.header["internal_date"].is_string());
    }
    assert!(frames.iter().any(|f| f.body.len() > 300_000));

    // Nothing was marked as seen.
    let status = account.status("INBOX").await.unwrap();
    assert_eq!(status.unseen, Some(4));

    let (frames, end) = parse_frames(&fetch_all(&account, "INBOX", &[]).await.unwrap());
    assert!(frames.is_empty());
    assert_eq!(end["end"], true);

    let too_many: Vec<u32> = (1..=101).collect();
    assert_eq!(
        fetch_all(&account, "INBOX", &too_many)
            .await
            .unwrap_err()
            .status,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        fetch_all(&account, "Nope", &[1]).await.unwrap_err().status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        fetch_all(&account, "INBOX", &[0]).await.unwrap_err().status,
        StatusCode::BAD_REQUEST
    );
}

#[tokio::test]
async fn searching() {
    let server = Dovecot::start();
    seed(&server, "robin", "INBOX", 3);
    server.deliver(
        "robin",
        "INBOX",
        &message(
            "lee@example.com",
            "robin@example.org",
            "Invoice \"42\"",
            "Please pay.",
        ),
        "S",
    );
    let account = account(&server, "robin");
    let search = async |json: serde_json::Value| {
        let query: Query = serde_json::from_value(json).unwrap();
        account.search("INBOX", &query).await.unwrap().uids
    };

    assert_eq!(
        search(serde_json::json!({"all": true})).await,
        vec![1, 2, 3, 4]
    );
    assert_eq!(
        search(serde_json::json!({"from": "lee@example.com"})).await,
        vec![4]
    );
    assert_eq!(
        search(serde_json::json!({"seen": false})).await,
        vec![1, 2, 3]
    );
    assert_eq!(
        search(serde_json::json!({"subject": "Invoice \"42\""})).await,
        vec![4]
    );
    assert_eq!(
        search(serde_json::json!({"or": [{"subject": "Message 1"}, {"body": "pay"}]})).await,
        vec![1, 4]
    );
    assert_eq!(
        search(serde_json::json!({"and": [{"since": "2020-01-01"}, {"not": {"uid": [2, 4]}}]}))
            .await,
        vec![1]
    );
    assert!(
        search(serde_json::json!({"before": "2020-01-01"}))
            .await
            .is_empty()
    );
}

#[tokio::test]
async fn changing_flags() {
    let server = Dovecot::start();
    seed(&server, "robin", "INBOX", 3);
    let account = account(&server, "robin");
    let validity = uid_validity(&account, "INBOX").await;
    let flags_of = async |uid: u32| {
        let list = account
            .list_messages("INBOX", uid, Some(uid), 1)
            .await
            .unwrap();
        list.messages[0].flags.clone()
    };
    let strings = |flags: &[&str]| flags.iter().map(|f| f.to_string()).collect::<Vec<_>>();

    let (added, _) = account
        .store_flags(
            "INBOX",
            validity,
            &[1, 2],
            &strings(&["\\seen", "$Triage-Done"]),
            &[],
        )
        .await
        .unwrap();
    assert_eq!(added, strings(&["\\Seen", "$Triage-Done"]));
    assert_eq!(flags_of(1).await, strings(&["$Triage-Done", "\\Seen"]));
    assert_eq!(flags_of(3).await, Vec::<String>::new());

    account
        .store_flags(
            "INBOX",
            validity,
            &[1],
            &strings(&["\\Flagged"]),
            &strings(&["\\Seen"]),
        )
        .await
        .unwrap();
    assert_eq!(flags_of(1).await, strings(&["$Triage-Done", "\\Flagged"]));

    for forbidden in ["\\Deleted", "\\Draft", "\\deleted"] {
        let error = account
            .store_flags("INBOX", validity, &[1], &strings(&[forbidden]), &[])
            .await
            .unwrap_err();
        assert_eq!(error.status, StatusCode::FORBIDDEN, "{forbidden}");
    }
    let injected = account
        .store_flags("INBOX", validity, &[1], &strings(&["x) \\Deleted (y"]), &[])
        .await
        .unwrap_err();
    assert_eq!(injected.status, StatusCode::BAD_REQUEST);

    let stale = account
        .store_flags("INBOX", validity + 1, &[1], &strings(&["\\Seen"]), &[])
        .await
        .unwrap_err();
    assert_eq!(stale.code, "uid_validity_changed");
    assert_eq!(
        flags_of(1).await,
        strings(&["$Triage-Done", "\\Flagged"]),
        "nothing changed"
    );

    assert!(
        account
            .store_flags("INBOX", validity, &[], &strings(&["\\Seen"]), &[])
            .await
            .is_err()
    );
    assert!(
        account
            .store_flags("INBOX", validity, &[1], &[], &[])
            .await
            .is_err()
    );
    let many: Vec<u32> = (1..=501).collect();
    assert!(
        account
            .store_flags("INBOX", validity, &many, &strings(&["\\Seen"]), &[])
            .await
            .is_err()
    );
}

async fn check_moving(server: &Dovecot) {
    seed(server, "robin", "INBOX", 5);
    // A message that some other mail client has marked as deleted but not yet expunged.
    server.deliver(
        "robin",
        "INBOX",
        &message(
            "kim@example.net",
            "robin@example.org",
            "Marked deleted",
            "Keep me.",
        ),
        "T",
    );
    let account = account(server, "robin");
    let validity = uid_validity(&account, "INBOX").await;

    let result = account
        .move_messages("INBOX", validity, &[2, 4, 77], "Archive")
        .await
        .unwrap();
    assert_eq!(result.destination_role, Some(Role::Archive));
    let moved: Vec<u32> = result.moved.iter().map(|m| m.uid).collect();
    assert_eq!(
        moved,
        vec![2, 4],
        "the missing UID 77 is not reported as moved"
    );
    assert_eq!(
        result.destination_uid_validity,
        Some(uid_validity(&account, "Archive").await)
    );

    let remaining = account.list_messages("INBOX", 1, None, 100).await.unwrap();
    let remaining_uids: Vec<u32> = remaining.messages.iter().map(|m| m.uid).collect();
    assert_eq!(remaining_uids, vec![1, 3, 5, 6]);
    // The message that was already marked as deleted is still there: moving never expunges
    // anything other than the messages it moved.
    assert!(
        remaining.messages[3]
            .flags
            .contains(&"\\Deleted".to_string())
    );

    let new_uids: Vec<u32> = result.moved.iter().map(|m| m.new_uid.unwrap()).collect();
    let (frames, _) = parse_frames(&fetch_all(&account, "Archive", &new_uids).await.unwrap());
    let mut subjects: Vec<String> = frames
        .iter()
        .map(|f| {
            let text = String::from_utf8_lossy(&f.body).to_string();
            text.lines()
                .find(|l| l.starts_with("Subject:"))
                .unwrap()
                .to_string()
        })
        .collect();
    subjects.sort();
    assert_eq!(subjects, vec!["Subject: Message 2", "Subject: Message 4"]);

    // Moving to the trash is allowed, and reported as such.
    let trashed = account
        .move_messages("INBOX", validity, &[1], "Trash")
        .await
        .unwrap();
    assert_eq!(trashed.destination_role, Some(Role::Trash));
    assert_eq!(server.messages_on_disk("robin", "Trash").len(), 1);

    // Protected and foreign destinations.
    for (destination, status) in [
        ("Drafts", StatusCode::FORBIDDEN),
        ("Sent", StatusCode::FORBIDDEN),
        ("Public/Announce", StatusCode::NOT_FOUND),
        ("Nope", StatusCode::NOT_FOUND),
        ("INBOX", StatusCode::BAD_REQUEST),
    ] {
        let error = account
            .move_messages("INBOX", validity, &[3], destination)
            .await
            .unwrap_err();
        assert_eq!(error.status, status, "{destination}: {error}");
    }
    let stale = account
        .move_messages("INBOX", validity + 1, &[3], "Archive")
        .await
        .unwrap_err();
    assert_eq!(stale.code, "uid_validity_changed");
    assert_eq!(account.status("INBOX").await.unwrap().messages, 3);
    assert!(server.messages_on_disk("robin", "Drafts").is_empty());
}

#[tokio::test]
async fn moving_messages() {
    check_moving(&Dovecot::start()).await;
}

#[tokio::test]
async fn moving_messages_on_a_server_without_move() {
    let server = Dovecot::start_with(Options {
        without_capabilities: vec!["MOVE"],
    });
    check_moving(&server).await;
}

#[tokio::test]
async fn moving_is_refused_without_move_and_uidplus() {
    let server = Dovecot::start_with(Options {
        without_capabilities: vec!["MOVE", "UIDPLUS"],
    });
    seed(&server, "robin", "INBOX", 1);
    let account = account(&server, "robin");
    let validity = uid_validity(&account, "INBOX").await;
    let error = account
        .move_messages("INBOX", validity, &[1], "Archive")
        .await
        .unwrap_err();
    assert_eq!(error.status, StatusCode::NOT_IMPLEMENTED);
    assert_eq!(account.status("INBOX").await.unwrap().messages, 1);
}

#[tokio::test]
async fn drafts() {
    let server = Dovecot::start();
    let original = "From: Kim Sample <kim@example.net>\n\
        To: robin@example.org, Lee <lee@example.com>\n\
        Cc: pat@example.net\n\
        Subject: Lunch\n\
        Date: Mon, 02 Nov 2026 10:00:00 +0000\n\
        Message-ID: <lunch-2@example.net>\n\
        References: <lunch-0@example.net>\n <lunch-1@example.net>\n\
        \n\
        Are you free on Thursday?\n";
    server.deliver("robin", "INBOX", original, "");
    let account = account(&server, "robin");
    let validity = uid_validity(&account, "INBOX").await;
    let parent = MessageRef {
        folder: "INBOX".into(),
        uid_validity: validity,
        uid: 1,
    };

    // A reply to the sender and one of the other recipients.
    let reply = DraftRequest {
        to: vec![Recipient {
            name: Some("Kim Sample".into()),
            address: "Kim@Example.net".into(),
        }],
        cc: vec![recipient("lee@example.com")],
        subject: "Re: Lunch".into(),
        body: "Thursday works.\n\nRobin".into(),
        in_reply_to: Some(parent.clone()),
    };
    let result = account.create_draft(&reply).await.unwrap();
    assert_eq!(result.folder, "Drafts");
    assert!(result.message_id.ends_with("@example.org>"));

    let drafts = account.list_messages("Drafts", 1, None, 10).await.unwrap();
    assert_eq!(drafts.messages.len(), 1);
    assert!(drafts.messages[0].flags.contains(&"\\Draft".to_string()));
    // Dovecot stores messages with bare line feeds.
    let stored = server.messages_on_disk("robin", "Drafts")[0].replace("\r\n", "\n");
    let (header, body) = stored.split_once("\n\n").unwrap();
    assert!(
        header.contains("From: \"Robin Example\" <robin@example.org>"),
        "{header}"
    );
    assert!(header.contains("<Kim@Example.net>"), "{header}");
    assert!(header.contains("Cc: <lee@example.com>"), "{header}");
    assert!(
        header.contains("In-Reply-To: <lunch-2@example.net>"),
        "{header}"
    );
    assert!(header.contains("<lunch-0@example.net>"), "{header}");
    assert!(header.contains("<lunch-1@example.net>"), "{header}");
    assert!(
        header.contains(&format!("Message-ID: {}", result.message_id)),
        "{header}"
    );
    assert!(header.contains("Content-Type: text/plain"), "{header}");
    assert!(!header.to_lowercase().contains("bcc"));
    assert!(!header.to_lowercase().contains("\nreply-to:"));
    assert!(body.contains("Thursday works."));

    // A reply cannot be addressed to someone who was not part of the conversation.
    let mut diverted = reply.clone();
    diverted.cc = vec![recipient("mallory@evil.example")];
    let error = account.create_draft(&diverted).await.unwrap_err();
    assert_eq!(error.status, StatusCode::FORBIDDEN);
    let mut diverted = reply.clone();
    diverted.to = vec![recipient("mallory@evil.example")];
    diverted.cc = vec![];
    assert_eq!(
        account.create_draft(&diverted).await.unwrap_err().status,
        StatusCode::FORBIDDEN
    );

    // The parent must exist and belong to the folder the caller knows.
    let mut missing = reply.clone();
    missing.in_reply_to = Some(MessageRef {
        uid: 42,
        ..parent.clone()
    });
    assert_eq!(
        account.create_draft(&missing).await.unwrap_err().status,
        StatusCode::NOT_FOUND
    );
    let mut stale = reply.clone();
    stale.in_reply_to = Some(MessageRef {
        uid_validity: validity + 1,
        ..parent.clone()
    });
    assert_eq!(
        account.create_draft(&stale).await.unwrap_err().code,
        "uid_validity_changed"
    );

    // A new message that is not a reply can go to anyone; that the user asked for it is the
    // app's responsibility.
    let fresh = DraftRequest {
        to: vec![recipient("someone@example.com")],
        cc: vec![],
        subject: "Größe".into(),
        body: "Hello".into(),
        in_reply_to: None,
    };
    account.create_draft(&fresh).await.unwrap();

    // Attempts to smuggle in headers or hidden text are rejected before anything is stored.
    let mut injected = fresh.clone();
    injected.subject = "Hi\r\nBcc: mallory@evil.example".into();
    assert!(account.create_draft(&injected).await.is_err());
    let mut injected = fresh.clone();
    injected.to = vec![recipient("a@example.com\r\nBcc: mallory@evil.example")];
    assert!(account.create_draft(&injected).await.is_err());
    let mut hidden = fresh.clone();
    hidden.body = "Hello\u{e0041}\u{e0042}".into();
    assert_eq!(
        account.create_draft(&hidden).await.unwrap_err().status,
        StatusCode::FORBIDDEN
    );

    assert_eq!(server.messages_on_disk("robin", "Drafts").len(), 2);
    assert_eq!(
        account.status("INBOX").await.unwrap().unseen,
        Some(1),
        "the original stays unread"
    );
}

#[tokio::test]
async fn login_failures_do_not_leak_the_password() {
    let server = Dovecot::start();
    let mut config = server.account("robin");
    config.auth = serde_json::from_value(serde_json::json!({
        "type": "password", "password": "wrong-secret-password"
    }))
    .unwrap();
    let account = account_from(config);
    let error = account.list_folders().await.unwrap_err();
    assert_eq!(error.status, StatusCode::SERVICE_UNAVAILABLE);
    assert!(!error.message.contains("wrong-secret-password"), "{error}");
    let last = account.last_error().await.unwrap();
    assert!(!last.contains("wrong-secret-password"));
}

#[tokio::test]
async fn broken_connections_are_reported_and_replaced() {
    let server = Dovecot::start();
    seed(&server, "robin", "INBOX", 1);
    // Reach the server through a relay, so that the test can cut the connection.
    let relay = common::Relay::start(server.port).await;
    let mut config = server.account("robin");
    config.port = relay.port;
    let account = account_from(config.clone());
    assert_eq!(account.status("INBOX").await.unwrap().messages, 1);

    relay.cut();
    let error = account.status("INBOX").await.unwrap_err();
    assert_eq!(error.status, StatusCode::SERVICE_UNAVAILABLE, "{error}");
    assert!(account.last_error().await.is_some());

    // The next operation opens a new connection.
    assert_eq!(account.status("INBOX").await.unwrap().messages, 1);
    assert!(account.last_error().await.is_none());

    // An account on a port where nothing listens fails cleanly too.
    config.port = 1;
    let error = account_from(config).list_folders().await.unwrap_err();
    assert_eq!(error.status, StatusCode::SERVICE_UNAVAILABLE);
}
