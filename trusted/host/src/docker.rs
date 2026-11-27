//! Running `docker`. Arguments are always passed as a list, never through a shell.

use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

pub trait Docker {
    /// Runs a command to completion and returns its standard output. Variables in `env` are
    /// added to the environment of the `docker` process, which is how secrets are passed to
    /// `--env NAME` options without appearing on a command line.
    fn run(&self, args: &[String], env: &[(&str, &str)]) -> Result<String, String>;

    /// Runs `from`, feeding its standard output into the standard input of `to`.
    fn pipe(&self, from: &[String], to: &[String]) -> Result<(), String>;

    /// Runs a command with `input` on its standard input, calling `on_line` for each line it
    /// prints. Returns whether the command succeeded.
    fn stream(
        &self,
        args: &[String],
        input: &str,
        on_line: &mut dyn FnMut(&str),
    ) -> Result<bool, String>;

    /// Runs a command attached to the terminal.
    fn interactive(&self, args: &[String]) -> Result<bool, String>;
}

pub struct Cli;

fn describe(args: &[String]) -> String {
    format!(
        "docker {}",
        args.iter().take(3).cloned().collect::<Vec<_>>().join(" ")
    )
}

fn last_lines(text: &[u8], count: usize) -> String {
    let text = String::from_utf8_lossy(text);
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(count)..].join("\n")
}

impl Docker for Cli {
    fn run(&self, args: &[String], env: &[(&str, &str)]) -> Result<String, String> {
        let output = Command::new("docker")
            .args(args)
            .envs(env.iter().copied())
            .stdin(Stdio::null())
            .output()
            .map_err(|e| format!("could not run docker: {e}"))?;
        if output.status.success() {
            Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
        } else {
            Err(format!(
                "{} failed:\n{}",
                describe(args),
                last_lines(&output.stderr, 15)
            ))
        }
    }

    fn pipe(&self, from: &[String], to: &[String]) -> Result<(), String> {
        let mut source = Command::new("docker")
            .args(from)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("could not run docker: {e}"))?;
        let stdout = source.stdout.take().expect("piped stdout");
        let sink = Command::new("docker")
            .args(to)
            .stdin(Stdio::from(stdout))
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .output()
            .map_err(|e| format!("could not run docker: {e}"))?;
        let source = source
            .wait_with_output()
            .map_err(|e| format!("could not run docker: {e}"))?;
        if !source.status.success() {
            return Err(format!(
                "{} failed:\n{}",
                describe(from),
                last_lines(&source.stderr, 15)
            ));
        }
        if !sink.status.success() {
            return Err(format!(
                "{} failed:\n{}",
                describe(to),
                last_lines(&sink.stderr, 40)
            ));
        }
        Ok(())
    }

    fn stream(
        &self,
        args: &[String],
        input: &str,
        on_line: &mut dyn FnMut(&str),
    ) -> Result<bool, String> {
        let mut child = Command::new("docker")
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("could not run docker: {e}"))?;
        {
            let mut stdin = child.stdin.take().expect("piped stdin");
            stdin
                .write_all(input.as_bytes())
                .map_err(|e| format!("could not write to docker: {e}"))?;
        }
        let stdout = child.stdout.take().expect("piped stdout");
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            on_line(&line);
        }
        let status = child
            .wait()
            .map_err(|e| format!("could not run docker: {e}"))?;
        Ok(status.success())
    }

    fn interactive(&self, args: &[String]) -> Result<bool, String> {
        let status = Command::new("docker")
            .args(args)
            .status()
            .map_err(|e| format!("could not run docker: {e}"))?;
        Ok(status.success())
    }
}
