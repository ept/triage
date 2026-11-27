//! The wrapper's own pages: what it shows while starting, and the window in which the user
//! approves or refuses a message to the coding agent.
//!
//! These pages are built here, from fixed markup and escaped text, and are shown in web views
//! of their own in which JavaScript is switched off. Nothing in them comes from the app's web
//! interface except the text that is to be approved, which is shown as text. The buttons are
//! links to addresses of the form `decision:<answer>`; the wrapper sees the attempt to follow
//! one, takes the answer from it, and never loads anything.

/// Escapes text for use in HTML, in element content and in attribute values.
pub fn escape(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len() + text.len() / 8);
    for c in text.chars() {
        match c {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&#39;"),
            // Not valid in HTML, and of no use in a text that a person is to read.
            '\0' => escaped.push('\u{FFFD}'),
            c => escaped.push(c),
        }
    }
    escaped
}

/// What the user chose in the approval window.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Decision {
    Send,
    DoNotSend,
}

/// The decision that an address stands for, if it is one of the two that the approval page
/// links to. Anything else is not a decision.
pub fn decision_from_url(url: &str) -> Option<Decision> {
    match url {
        "decision:send" => Some(Decision::Send),
        "decision:do-not-send" => Some(Decision::DoNotSend),
        _ => None,
    }
}

const STYLE: &str = "
:root { color-scheme: light dark; }
body { font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 24px;
       background: Canvas; color: CanvasText; }
h1 { font-size: 17px; margin: 0 0 12px; }
p { margin: 0 0 12px; max-width: 64em; }
pre { font: 13px/1.5 ui-monospace, Menlo, monospace; white-space: pre-wrap;
      overflow-wrap: anywhere; border: 1px solid GrayText; border-radius: 6px;
      padding: 12px; margin: 0 0 16px; max-height: 55vh; overflow: auto; }
.buttons { display: flex; gap: 12px; justify-content: flex-end; }
a.button { text-decoration: none; padding: 6px 18px; border-radius: 6px;
           border: 1px solid GrayText; color: CanvasText; }
a.button.default { background: Highlight; color: HighlightText; border-color: Highlight; }
.note { color: GrayText; }
.error { color: #c0392b; font-weight: 600; }
";

fn page(title: &str, body: &str) -> String {
    // No scripts and nothing from anywhere else, whatever the body contains.
    format!(
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n\
         <meta http-equiv=\"Content-Security-Policy\" \
         content=\"default-src 'none'; style-src 'unsafe-inline'\">\n\
         <title>{}</title>\n<style>{STYLE}</style></head>\n<body>\n{body}\n</body></html>\n",
        escape(title)
    )
}

/// The page that asks whether a message may be sent to the coding agent. `what` describes the
/// kind of message ("a request for a change to the app"); `text` is the message, exactly as it
/// would be sent.
pub fn approval_page(what: &str, text: &str) -> String {
    let characters = text.chars().count();
    page(
        "Send this to the coding agent?",
        &format!(
            "<h1>The app wants to send {} to the coding agent</h1>\n\
             <p>If you approve, the text below leaves this computer and is sent to a remote \
             model. Check that it contains nothing from your email that you want to keep \
             private. Scroll to the end before you decide.</p>\n\
             <pre>{}</pre>\n\
             <p class=\"note\">{characters} characters. Closing this window does not send \
             it.</p>\n\
             <div class=\"buttons\">\
             <a class=\"button default\" href=\"decision:do-not-send\">Do not send</a>\
             <a class=\"button\" href=\"decision:send\">Send</a></div>",
            escape(what),
            escape(text)
        ),
    )
}

/// The page that is shown while everything is starting, and when starting has failed.
pub fn status_page(lines: &[String], error: Option<&str>) -> String {
    let log = lines
        .iter()
        .map(|line| escape(line))
        .collect::<Vec<_>>()
        .join("\n");
    let heading = match error {
        Some(_) => "Email Triage could not be started",
        None => "Starting Email Triage ...",
    };
    let problem = match error {
        Some(error) => format!("<p class=\"error\">{}</p>\n", escape(error)),
        None => String::new(),
    };
    page(
        heading,
        &format!("<h1>{heading}</h1>\n{problem}<pre>{log}</pre>"),
    )
}

/// The page that shows a warning from the gateway.
pub fn warning_page(text: &str) -> String {
    page(
        "Warning",
        &format!(
            "<h1>Warning</h1>\n<p class=\"error\">{}</p>\n\
             <p>If you did not ask for this, quit Email Triage now; that stops the app. \
             Messages in the trash can be moved back in your mail program.</p>",
            escape(text)
        ),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The body of the page without its fixed head.
    fn body(page: &str) -> &str {
        page.split_once("<body>").unwrap().1
    }

    #[test]
    fn markup_in_the_text_is_shown_and_not_obeyed() {
        let hostile = "</pre><script>alert(1)</script><a href=\"decision:send\">x</a>\
                       <img src='http://evil.example/?q' onerror=x>&lt;&amp;\0";
        let page = approval_page("a bug report", hostile);
        let shown = body(&page);
        // The only elements are the fixed ones: nothing in the text opened or closed one.
        // (h1, p, pre, p, div and two links, each opened and closed; then </body></html>.)
        assert_eq!(shown.matches('<').count(), 2 * 7 + 2, "{shown}");
        assert_eq!(shown.matches("<a ").count(), 2);
        assert_eq!(
            shown.matches("decision:send").count(),
            2,
            "once as a link, once as text"
        );
        assert_eq!(shown.matches("href=\"decision:send\"").count(), 1);
        assert!(shown.contains(
            "&lt;/pre&gt;&lt;script&gt;alert(1)&lt;/script&gt;&lt;a href=&quot;decision:send&quot;"
        ));
        assert!(shown.contains("onerror=x&gt;&amp;lt;&amp;amp;\u{FFFD}</pre>"));
        // The kind of message is escaped as well, although it comes from a fixed list.
        assert!(approval_page("<b>", "x").contains("send &lt;b&gt; to"));
    }

    #[test]
    fn the_whole_text_is_on_the_page_with_its_length() {
        let text = format!("first line\n{}\nlast line", "x".repeat(5000));
        let page = approval_page("a request for a change to the app", &text);
        assert!(page.contains(&format!("<pre>{text}</pre>")));
        assert!(page.contains("5021 characters."));
        // Refusing is the button that stands out.
        assert!(page.contains("<a class=\"button default\" href=\"decision:do-not-send\">"));
    }

    #[test]
    fn no_page_can_run_scripts_or_load_anything() {
        for page in [
            approval_page("a message", "text"),
            status_page(&["one".into(), "<two>".into()], Some("it <failed>")),
            warning_page("<40> messages"),
        ] {
            assert!(page.contains("content=\"default-src 'none'; style-src 'unsafe-inline'\""));
            assert!(!page.contains("<script"));
            assert!(!page.contains("http://") && !page.contains("https://"));
            assert!(
                !page.contains("<two>") && !page.contains("<failed>") && !page.contains("<40>")
            );
        }
    }

    #[test]
    fn only_the_two_fixed_addresses_are_decisions() {
        assert_eq!(decision_from_url("decision:send"), Some(Decision::Send));
        assert_eq!(
            decision_from_url("decision:do-not-send"),
            Some(Decision::DoNotSend)
        );
        for other in [
            "decision:send ",
            "decision:Send",
            "decision:send?x",
            "decision://send",
            "",
            "about:blank",
        ] {
            assert_eq!(decision_from_url(other), None, "{other:?}");
        }
    }

    #[test]
    fn the_status_page_shows_progress_and_failure() {
        let page = status_page(&["Starting the gateway ...".into()], None);
        assert!(page.contains("<h1>Starting Email Triage ...</h1>"));
        assert!(page.contains("<pre>Starting the gateway ...</pre>"));
        let page = status_page(&[], Some("the gateway image has not been built"));
        assert!(page.contains("could not be started"));
        assert!(page.contains("<p class=\"error\">the gateway image has not been built</p>"));
    }
}
