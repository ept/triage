//! Composing drafts.
//!
//! The app never supplies a raw message. It supplies a few structured fields, and the gateway
//! builds the message from them, so that a draft can only ever be a plain-text message with the
//! headers listed in [`ALLOWED_HEADERS`].

use chrono::Utc;
use mail_builder::MessageBuilder;
use serde::Deserialize;

use crate::error::{ApiError, ApiResult};
use crate::rules;

/// The only header fields that may appear in a draft. In particular there is no `Bcc` and no
/// `Reply-To`.
pub const ALLOWED_HEADERS: &[&str] = &[
    "from",
    "to",
    "cc",
    "subject",
    "date",
    "message-id",
    "in-reply-to",
    "references",
    "mime-version",
    "content-type",
    "content-transfer-encoding",
];

/// How many message ids of a thread are carried over into the `References` header.
const MAX_REFERENCES: usize = 30;

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Recipient {
    #[serde(default)]
    pub name: Option<String>,
    pub address: String,
}

/// The validated content of a draft, ready to be composed.
#[derive(Debug)]
pub struct Draft {
    pub from_name: Option<String>,
    pub from_address: String,
    pub to: Vec<Recipient>,
    pub cc: Vec<Recipient>,
    pub subject: String,
    /// With CRLF line endings.
    pub body: String,
    /// Message id of the message being replied to, without angle brackets.
    pub in_reply_to: Option<String>,
    /// Message ids of the thread so far, oldest first, without angle brackets.
    pub references: Vec<String>,
}

/// Checks the fields of a draft as submitted by the app. Returns the body with CRLF line endings.
pub fn validate(
    to: &[Recipient],
    cc: &[Recipient],
    subject: &str,
    body: &str,
) -> ApiResult<String> {
    if to.is_empty() {
        return Err(ApiError::bad_request(
            "a draft needs at least one recipient",
        ));
    }
    if to.len() + cc.len() > rules::MAX_RECIPIENTS {
        return Err(ApiError::bad_request("too many recipients"));
    }
    for recipient in to.iter().chain(cc) {
        rules::validate_address(&recipient.address)?;
        if let Some(name) = &recipient.name {
            rules::validate_display_name(name)?;
        }
    }
    rules::validate_subject(subject)?;
    rules::validate_body(body)
}

/// Builds the RFC 5322 message. Returns the message and its `Message-ID` (with angle brackets).
pub fn compose(draft: &Draft) -> ApiResult<(Vec<u8>, String)> {
    let domain = draft
        .from_address
        .rsplit_once('@')
        .map(|(_, domain)| domain)
        .ok_or_else(|| ApiError::bad_request("the account has no valid address"))?;
    let message_id = format!("{}@{}", uuid::Uuid::new_v4(), domain);

    let address = |r: &Recipient| -> mail_builder::headers::address::Address<'static> {
        match r.name.as_deref().filter(|name| !name.trim().is_empty()) {
            Some(name) => (name.to_string(), r.address.clone()).into(),
            None => r.address.clone().into(),
        }
    };
    let list = |recipients: &[Recipient]| -> mail_builder::headers::address::Address<'static> {
        mail_builder::headers::address::Address::new_list(recipients.iter().map(address).collect())
    };

    let from = Recipient {
        name: draft.from_name.clone(),
        address: draft.from_address.clone(),
    };
    let mut builder = MessageBuilder::new()
        .from(address(&from))
        .to(list(&draft.to))
        .subject(draft.subject.clone())
        .date(Utc::now().timestamp())
        .message_id(message_id.clone())
        .text_body(draft.body.clone());
    if !draft.cc.is_empty() {
        builder = builder.cc(list(&draft.cc));
    }
    if let Some(parent) = &draft.in_reply_to {
        builder = builder.in_reply_to(parent.clone());
        let mut references: Vec<String> = draft
            .references
            .iter()
            .filter(|id| *id != parent)
            .cloned()
            .collect();
        if references.len() >= MAX_REFERENCES {
            // Keep the root of the thread and the most recent messages.
            let tail = references.split_off(references.len() - (MAX_REFERENCES - 2));
            references.truncate(1);
            references.extend(tail);
        }
        references.push(parent.clone());
        builder = builder.references(references);
    }

    let message = builder
        .write_to_vec()
        .map_err(|e| ApiError::bad_request(format!("could not compose the draft: {e}")))?;
    check_headers(&message)?;
    Ok((message, format!("<{message_id}>")))
}

/// Defence in depth: whatever the message builder did, refuse to store a message that has a
/// header outside the allowed set, or that is not a single plain-text part.
fn check_headers(message: &[u8]) -> ApiResult<()> {
    let refuse = |what: String| ApiError::forbidden(format!("refusing to store the draft: {what}"));
    let header_end = find(message, b"\r\n\r\n")
        .ok_or_else(|| refuse("the message has no header section".into()))?;
    let header = std::str::from_utf8(&message[..header_end])
        .map_err(|_| refuse("the header is not ASCII".into()))?;
    if !header.is_ascii() {
        return Err(refuse("the header is not ASCII".into()));
    }
    let mut content_type = None;
    for line in unfold(header) {
        let (name, value) = line
            .split_once(':')
            .ok_or_else(|| refuse("malformed header line".into()))?;
        let name = name.to_ascii_lowercase();
        if !ALLOWED_HEADERS.contains(&name.as_str()) {
            return Err(refuse(format!("unexpected header {name:?}")));
        }
        if name == "content-type" {
            content_type = Some(value.trim().to_ascii_lowercase());
        }
    }
    match content_type {
        Some(value) if value.starts_with("text/plain") => Ok(()),
        other => Err(refuse(format!("unexpected content type {other:?}"))),
    }
}

/// Splits a header section into logical lines, joining continuation lines.
pub fn unfold(header: &str) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    for line in header.split("\r\n").flat_map(|l| l.split('\n')) {
        if line.starts_with([' ', '\t']) {
            if let Some(last) = lines.last_mut() {
                last.push(' ');
                last.push_str(line.trim_start());
            }
        } else if !line.is_empty() {
            lines.push(line.to_string());
        }
    }
    lines
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

/// Extracts message ids (without angle brackets) from the value of a `Message-ID`, `In-Reply-To`
/// or `References` header. Anything that does not look like a message id is skipped.
pub fn parse_message_ids(value: &str) -> Vec<String> {
    let mut ids = Vec::new();
    let mut rest = value;
    while let Some(start) = rest.find('<') {
        let after = &rest[start + 1..];
        let Some(end) = after.find('>') else { break };
        let candidate = &after[..end];
        rest = &after[end + 1..];
        let valid = (3..=250).contains(&candidate.len())
            && candidate.contains('@')
            && candidate
                .bytes()
                .all(|b| b.is_ascii_graphic() && b != b'<' && b != b'>');
        if valid && !ids.iter().any(|id| id == candidate) {
            ids.push(candidate.to_string());
        }
    }
    ids
}

/// Finds the message ids in the `References` header of a raw header section.
pub fn references_from_header(header: &[u8]) -> Vec<String> {
    let header = String::from_utf8_lossy(header);
    unfold(&header)
        .iter()
        .filter_map(|line| line.split_once(':'))
        .filter(|(name, _)| name.trim().eq_ignore_ascii_case("references"))
        .flat_map(|(_, value)| parse_message_ids(value))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::StatusCode;

    fn recipient(name: Option<&str>, address: &str) -> Recipient {
        Recipient {
            name: name.map(str::to_string),
            address: address.to_string(),
        }
    }

    fn sample() -> Draft {
        Draft {
            from_name: Some("Robin Example".into()),
            from_address: "robin@example.org".into(),
            to: vec![recipient(Some("Kim Sample"), "kim@example.net")],
            cc: vec![],
            subject: "Re: Lunch".into(),
            body: "Hello,\r\n\r\nThursday works.\r\n".into(),
            in_reply_to: None,
            references: vec![],
        }
    }

    fn header_names(message: &[u8]) -> Vec<String> {
        let end = find(message, b"\r\n\r\n").unwrap();
        unfold(std::str::from_utf8(&message[..end]).unwrap())
            .iter()
            .map(|line| line.split_once(':').unwrap().0.to_ascii_lowercase())
            .collect()
    }

    fn header_value(message: &[u8], name: &str) -> Option<String> {
        let end = find(message, b"\r\n\r\n").unwrap();
        unfold(std::str::from_utf8(&message[..end]).unwrap())
            .iter()
            .filter_map(|line| line.split_once(':'))
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.trim().to_string())
    }

    #[test]
    fn composes_a_plain_text_message() {
        let (message, id) = compose(&sample()).unwrap();
        let text = String::from_utf8(message.clone()).unwrap();
        assert!(id.starts_with('<') && id.ends_with("@example.org>"), "{id}");
        assert_eq!(header_value(&message, "message-id").unwrap(), id);
        assert!(
            header_value(&message, "from")
                .unwrap()
                .contains("<robin@example.org>")
        );
        assert!(
            header_value(&message, "to")
                .unwrap()
                .contains("<kim@example.net>")
        );
        assert_eq!(header_value(&message, "subject").unwrap(), "Re: Lunch");
        assert!(
            header_value(&message, "content-type")
                .unwrap()
                .starts_with("text/plain")
        );
        assert!(text.contains("Thursday works."));
        let names = header_names(&message);
        assert!(!names.contains(&"bcc".to_string()));
        assert!(!names.contains(&"reply-to".to_string()));
        assert!(!names.contains(&"cc".to_string()));
        assert!(!names.contains(&"in-reply-to".to_string()));
        for name in names {
            assert!(ALLOWED_HEADERS.contains(&name.as_str()), "{name}");
        }
    }

    #[test]
    fn replies_reference_the_thread() {
        let mut draft = sample();
        draft.in_reply_to = Some("c@example.net".into());
        draft.references = vec!["a@example.net".into(), "b@example.net".into()];
        draft.cc = vec![recipient(None, "lee@example.com")];
        let (message, _) = compose(&draft).unwrap();
        assert_eq!(
            header_value(&message, "in-reply-to").unwrap(),
            "<c@example.net>"
        );
        assert_eq!(
            header_value(&message, "references").unwrap(),
            "<a@example.net> <b@example.net> <c@example.net>"
        );
        assert_eq!(header_value(&message, "cc").unwrap(), "<lee@example.com>");
    }

    #[test]
    fn long_reference_chains_are_trimmed() {
        let mut draft = sample();
        draft.in_reply_to = Some("last@example.net".into());
        draft.references = (0..100).map(|i| format!("m{i}@example.net")).collect();
        let (message, _) = compose(&draft).unwrap();
        let references = header_value(&message, "references").unwrap();
        let ids = parse_message_ids(&references);
        assert_eq!(ids.len(), MAX_REFERENCES);
        assert_eq!(ids.first().unwrap(), "m0@example.net");
        assert_eq!(ids.last().unwrap(), "last@example.net");
        assert_eq!(ids[ids.len() - 2], "m99@example.net");
    }

    #[test]
    fn non_ascii_is_encoded_and_cannot_inject_headers() {
        let mut draft = sample();
        draft.subject = "Größe – 日本語".into();
        draft.to = vec![recipient(
            Some("Zoë \"Z\" <x@evil.example>"),
            "kim@example.net",
        )];
        draft.body = "Grüße\r\nBcc: hidden@example.com\r\n".into();
        let (message, _) = compose(&draft).unwrap();
        let end = find(&message, b"\r\n\r\n").unwrap();
        assert!(message[..end].is_ascii());
        let names = header_names(&message);
        assert!(!names.contains(&"bcc".to_string()));
        // The only addresses in the To header are the ones we put there.
        let to = header_value(&message, "to").unwrap();
        assert!(to.ends_with("<kim@example.net>"), "{to}");
        assert_eq!(to.matches('<').count(), 1, "{to}");
    }

    #[test]
    fn validation_of_submitted_fields() {
        let to = vec![recipient(None, "kim@example.net")];
        assert_eq!(validate(&to, &[], "Hi", "a\nb").unwrap(), "a\r\nb");
        assert!(validate(&[], &to, "Hi", "x").is_err());
        let bad = vec![recipient(None, "kim@example.net\r\nBcc: x@example.com")];
        assert!(validate(&bad, &[], "Hi", "x").is_err());
        let bad_name = vec![recipient(
            Some("Kim\r\nBcc: x@example.com"),
            "kim@example.net",
        )];
        assert!(validate(&bad_name, &[], "Hi", "x").is_err());
        assert!(validate(&to, &[], "Hi\r\nBcc: x@example.com", "x").is_err());
        let many: Vec<_> = (0..51)
            .map(|i| recipient(None, &format!("user{i}@example.net")))
            .collect();
        assert!(validate(&many, &[], "Hi", "x").is_err());
        let hidden = validate(&to, &[], "Hi", "a\u{200b}\u{200b}b").unwrap_err();
        assert_eq!(hidden.status, StatusCode::FORBIDDEN);
    }

    #[test]
    fn header_check_rejects_unexpected_messages() {
        check_headers(b"From: a@example.org\r\nContent-Type: text/plain\r\n\r\nx").unwrap();
        for bad in [
            &b"From: a@example.org\r\nBcc: b@example.org\r\nContent-Type: text/plain\r\n\r\nx"[..],
            b"From: a@example.org\r\nContent-Type: text/html\r\n\r\nx",
            b"From: a@example.org\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\nx",
            b"From: a@example.org\r\n\r\nx",
            b"From: a@example.org\r\nX-Anything: 1\r\nContent-Type: text/plain\r\n\r\nx",
            b"no header end",
        ] {
            assert!(
                check_headers(bad).is_err(),
                "{}",
                String::from_utf8_lossy(bad)
            );
        }
    }

    #[test]
    fn message_id_parsing() {
        assert_eq!(
            parse_message_ids("<a@x.example>\r\n <b@y.example> junk <not an id> <c@z.example"),
            vec!["a@x.example", "b@y.example"]
        );
        assert_eq!(parse_message_ids("<a@x> <a@x>"), vec!["a@x"]);
        assert!(parse_message_ids("<no-at-sign>").is_empty());
        assert_eq!(
            references_from_header(
                b"Subject: x\r\nReferences: <a@x.example>\r\n\t<b@y.example>\r\nTo: z\r\n\r\n"
            ),
            vec!["a@x.example", "b@y.example"]
        );
    }
}
