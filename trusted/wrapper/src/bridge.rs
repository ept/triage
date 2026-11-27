//! The connection between the thread that starts and supervises the sandboxes and the thread
//! that owns the windows.
//!
//! Starting containers and waiting for the coding agent takes minutes and must not block the
//! windows, so it happens on a thread of its own. Whatever that thread has to tell or ask the
//! user is sent to the window thread as a [`Request`]. A question blocks the asking thread
//! until the user has answered; if the windows go away first, the answer is no.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;

use triage_host::run::{Frontend, describe_kind};

/// Something for the window thread to show.
#[derive(Debug)]
pub enum Request {
    /// A line of progress.
    Say(String),
    /// A warning that the user must not miss.
    Warn(String),
    /// A message that the app wants to send to the coding agent. `what` says what kind of
    /// message it is; `text` is the message in full. The answer is sent through `answer`:
    /// true to send it. Dropping `answer` without using it means no.
    Approve {
        what: String,
        text: String,
        answer: mpsc::Sender<bool>,
    },
}

/// The supervising thread's view of the user: everything is handed to `deliver`, which passes
/// it to the window thread.
pub struct Bridge<F: Fn(Request)> {
    deliver: F,
    stop: Arc<AtomicBool>,
}

impl<F: Fn(Request)> Bridge<F> {
    /// `stop` is set by the window thread when the user quits.
    pub fn new(deliver: F, stop: Arc<AtomicBool>) -> Self {
        Bridge { deliver, stop }
    }
}

impl<F: Fn(Request)> Frontend for Bridge<F> {
    fn say(&self, text: &str) {
        (self.deliver)(Request::Say(text.to_string()));
    }

    fn warn(&self, text: &str) {
        (self.deliver)(Request::Warn(text.to_string()));
    }

    fn approve(&self, kind: &str, text: &str) -> Result<bool, String> {
        if self.stopping() {
            return Ok(false);
        }
        let (answer, wait) = mpsc::channel();
        (self.deliver)(Request::Approve {
            what: describe_kind(kind).to_string(),
            text: text.to_string(),
            answer,
        });
        // An error means that nobody will answer any more (the windows are gone): not sent.
        Ok(wait.recv().unwrap_or(false))
    }

    fn stopping(&self) -> bool {
        self.stop.load(Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;

    fn bridge(answer: Option<bool>) -> (Bridge<impl Fn(Request)>, Arc<AtomicBool>) {
        let stop = Arc::new(AtomicBool::new(false));
        let deliver = move |request| {
            if let Request::Approve {
                answer: channel, ..
            } = request
                && let Some(answer) = answer
            {
                channel.send(answer).unwrap();
            }
            // Otherwise the request is dropped unanswered, as when the window is closed.
        };
        (Bridge::new(deliver, stop.clone()), stop)
    }

    #[test]
    fn the_answer_is_the_users() {
        assert_eq!(bridge(Some(true)).0.approve("feature", "text"), Ok(true));
        assert_eq!(bridge(Some(false)).0.approve("feature", "text"), Ok(false));
    }

    #[test]
    fn no_answer_means_no() {
        assert_eq!(bridge(None).0.approve("bug", "text"), Ok(false));
    }

    #[test]
    fn nothing_is_asked_once_the_user_has_quit() {
        let (bridge, stop) = bridge(Some(true));
        assert!(!bridge.stopping());
        stop.store(true, Ordering::SeqCst);
        assert!(bridge.stopping());
        assert_eq!(bridge.approve("feature", "text"), Ok(false));
    }

    #[test]
    fn the_user_is_shown_the_text_and_what_it_is() {
        let seen = RefCell::new(Vec::new());
        let bridge = Bridge::new(
            |request| seen.borrow_mut().push(format!("{request:?}")),
            Arc::new(AtomicBool::new(false)),
        );
        bridge.say("Starting the gateway ...");
        bridge.warn("40 messages were moved to the trash.");
        bridge.approve("reply", "Yes, please do.").unwrap();
        let seen = seen.borrow();
        assert_eq!(seen[0], "Say(\"Starting the gateway ...\")");
        assert_eq!(seen[1], "Warn(\"40 messages were moved to the trash.\")");
        assert!(seen[2].starts_with(
            "Approve { what: \"a reply to the coding agent\", text: \"Yes, please do.\""
        ));
    }
}
