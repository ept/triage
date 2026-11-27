//! Running the coding agent, and turning its output into events for the app's user interface.
//!
//! The coding agent is Claude Code, run non-interactively inside the coding sandbox. Its output
//! is a stream of JSON lines. Only a digest is passed on: what the agent says, and which tools
//! it uses.

use serde_json::Value;

use crate::sandbox::{CODING, DockerArgs};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AgentEvent {
    /// Something the agent said.
    Text(String),
    /// A short note on what the agent is doing.
    Status(String),
    /// The agent has finished.
    Done { ok: bool, text: String },
}

impl AgentEvent {
    /// The event kind and text, as the gateway's control API expects them.
    pub fn kind_and_text(&self) -> (&'static str, &str) {
        match self {
            AgentEvent::Text(text) => ("text", text),
            AgentEvent::Status(text) => ("status", text),
            AgentEvent::Done { text, .. } => ("done", text),
        }
    }
}

/// The command that runs the coding agent on a prompt, which it reads from standard input (so
/// that the prompt can never be mistaken for a command line option). `continue_session` makes
/// it carry on its most recent conversation, for replies to its questions.
pub fn command(model: Option<&str>, continue_session: bool) -> DockerArgs {
    let mut args: DockerArgs = [
        "exec",
        "--interactive",
        "--workdir",
        "/workspace",
        CODING,
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        // The agent works unattended; the container is what confines it.
        "--dangerously-skip-permissions",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    if let Some(model) = model {
        args.extend(["--model".to_string(), model.to_string()]);
    }
    if continue_session {
        args.push("--continue".to_string());
    }
    args
}

fn shorten(text: &str, max: usize) -> String {
    let line = text.lines().next().unwrap_or("");
    let mut short: String = line.chars().take(max).collect();
    if line.chars().count() > max || text.lines().count() > 1 {
        short.push('…');
    }
    short
}

/// Interprets one line of the agent's output.
pub fn parse_line(line: &str) -> Vec<AgentEvent> {
    let Ok(value) = serde_json::from_str::<Value>(line) else {
        return Vec::new();
    };
    match value.get("type").and_then(Value::as_str) {
        Some("assistant") => {
            let content = value
                .pointer("/message/content")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            content
                .iter()
                .filter_map(|block| match block.get("type").and_then(Value::as_str) {
                    Some("text") => block
                        .get("text")
                        .and_then(Value::as_str)
                        .filter(|text| !text.trim().is_empty())
                        .map(|text| AgentEvent::Text(text.to_string())),
                    Some("tool_use") => {
                        let name = block.get("name").and_then(Value::as_str).unwrap_or("tool");
                        let input = block.get("input");
                        let detail = ["file_path", "path", "command", "pattern", "description"]
                            .iter()
                            .find_map(|key| input?.get(key)?.as_str());
                        Some(AgentEvent::Status(match detail {
                            Some(detail) => format!("{name}: {}", shorten(detail, 100)),
                            None => name.to_string(),
                        }))
                    }
                    _ => None,
                })
                .collect()
        }
        Some("result") => {
            let ok = !value
                .get("is_error")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let text = value
                .get("result")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            vec![AgentEvent::Done { ok, text }]
        }
        _ => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_prompt_is_never_an_argument() {
        let fresh = command(None, false);
        assert!(fresh.contains(&"--print".to_string()));
        assert!(!fresh.contains(&"--continue".to_string()));
        assert_eq!(&fresh[..2], ["exec", "--interactive"]);
        let reply = command(Some("claude-sonnet-5-5"), true);
        assert!(reply.ends_with(&[
            "--model".to_string(),
            "claude-sonnet-5-5".to_string(),
            "--continue".to_string()
        ]));
    }

    #[test]
    fn assistant_messages_become_text_and_status_events() {
        let line = r#"{"type":"assistant","message":{"content":[
            {"type":"text","text":"I'll add the button."},
            {"type":"tool_use","name":"Edit","input":{"file_path":"/workspace/app/web/index.html"}},
            {"type":"tool_use","name":"Bash","input":{"command":"uv run pytest\nsecond line"}},
            {"type":"tool_use","name":"TodoWrite","input":{}},
            {"type":"text","text":"  "}]}}"#
            .replace('\n', " ");
        assert_eq!(
            parse_line(&line),
            vec![
                AgentEvent::Text("I'll add the button.".into()),
                AgentEvent::Status("Edit: /workspace/app/web/index.html".into()),
                AgentEvent::Status("Bash: uv run pytest…".into()),
                AgentEvent::Status("TodoWrite".into()),
            ]
        );
    }

    #[test]
    fn the_result_ends_the_run() {
        assert_eq!(
            parse_line(
                r#"{"type":"result","subtype":"success","is_error":false,"result":"Done."}"#
            ),
            vec![AgentEvent::Done {
                ok: true,
                text: "Done.".into()
            }]
        );
        assert_eq!(
            parse_line(r#"{"type":"result","subtype":"error_max_turns","is_error":true}"#),
            vec![AgentEvent::Done {
                ok: false,
                text: String::new()
            }]
        );
    }

    #[test]
    fn other_lines_are_ignored() {
        for line in [
            "",
            "not json",
            r#"{"type":"system","subtype":"init"}"#,
            r#"{"type":"user","message":{"content":[{"type":"tool_result","content":"secret"}]}}"#,
            r#"{"type":"assistant"}"#,
            r#"[1,2,3]"#,
        ] {
            assert!(parse_line(line).is_empty(), "{line}");
        }
    }
}
