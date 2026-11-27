//! Makes sure that the self-test has passed with the web view and the program at hand.
//!
//! The confinement in `view.rs` rests on how the system's web view behaves, which an update
//! of the system can change without anything here changing. So the wrapper remembers what
//! the self-test last passed with: the version of the web view, and this program itself.
//! When either differs at start, the self-test runs again before anything else, and the app
//! is not shown unless it passes.
//!
//! The record is kept in the data directory itself, which no sandbox can write to (they get
//! directories below it).

use std::hash::Hasher;
use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use triage_host::settings;

/// The file in the data directory that says what the self-test last passed with.
pub const RECORD: &str = "wrapper-self-test.json";
/// The file in the data directory with the output of the last automatic self-test.
pub const OUTPUT: &str = "wrapper-self-test.log";
/// The self-test takes about half a minute; after this long it counts as failed.
const PATIENCE: Duration = Duration::from_secs(180);

/// What a pass of the self-test is a statement about.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Subject {
    /// The version of the system's web view.
    pub web_view: String,
    /// A fingerprint of this program, which contains both the restrictions and the test.
    pub program: String,
}

impl Subject {
    /// The subject for the web view of this version and the program in the file `executable`.
    pub fn new(web_view: &str, executable: &Path) -> Result<Subject, String> {
        let describe = |error: std::io::Error| format!("{}: {error}", executable.display());
        let mut file = std::fs::File::open(executable).map_err(describe)?;
        // Not a defence against anyone (who can replace the program can do anything): it
        // only has to notice that the program is not the one that was tested.
        let mut hasher = std::hash::DefaultHasher::new();
        let mut buffer = vec![0; 1 << 16];
        let mut length: u64 = 0;
        loop {
            let read = file.read(&mut buffer).map_err(describe)?;
            if read == 0 {
                break;
            }
            hasher.write(&buffer[..read]);
            length += read as u64;
        }
        Ok(Subject {
            web_view: web_view.to_string(),
            program: format!("{length}-{:016x}", hasher.finish()),
        })
    }

    fn as_record(&self) -> Value {
        json!({"web_view": self.web_view, "program": self.program})
    }
}

/// Whether the record in `data` says that the self-test passed with exactly this subject.
/// A record that is missing or cannot be read says nothing.
pub fn passed_before(data: &Path, subject: &Subject) -> bool {
    matches!(
        settings::read::<Value>(&data.join(RECORD)),
        Ok(Some(record)) if record == subject.as_record()
    )
}

/// Runs `self_test` unless it has passed with this subject before, and remembers a pass.
/// Returns whether it ran. If it fails, nothing is remembered, including an earlier pass.
pub fn ensure(
    data: &Path,
    subject: &Subject,
    self_test: impl FnOnce() -> Result<(), String>,
) -> Result<bool, String> {
    if passed_before(data, subject) {
        return Ok(false);
    }
    let record = data.join(RECORD);
    // Before the test, so that a test that is cut short leaves no pass behind either.
    match std::fs::remove_file(&record) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(format!("{}: {error}", record.display())),
    }
    self_test()?;
    settings::write(&record, &subject.as_record())?;
    Ok(true)
}

/// Runs the self-test as a process of its own (`<executable> --self-test`), with its output
/// in the file `output`. Fails if the test does, if it takes too long, or if `stopping` says
/// that the user wants to quit; the process is ended in the last two cases.
pub fn run_self_test(
    executable: &Path,
    output: &Path,
    stopping: impl Fn() -> bool,
) -> Result<(), String> {
    run_until(executable, output, stopping, PATIENCE)
}

fn run_until(
    executable: &Path,
    output: &Path,
    stopping: impl Fn() -> bool,
    patience: Duration,
) -> Result<(), String> {
    let file =
        std::fs::File::create(output).map_err(|error| format!("{}: {error}", output.display()))?;
    let mut child = Command::new(executable)
        .arg("--self-test")
        .stdin(Stdio::null())
        .stdout(file)
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("could not start the self-test: {error}"))?;
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => return Ok(()),
            Ok(Some(_)) => return Err("the self-test did not pass".into()),
            Ok(None) => {}
            Err(error) => return Err(format!("could not wait for the self-test: {error}")),
        }
        let reason = if stopping() {
            Some("the self-test was interrupted")
        } else if start.elapsed() > patience {
            Some("the self-test did not finish")
        } else {
            None
        };
        if let Some(reason) = reason {
            child.kill().ok();
            child.wait().ok();
            return Err(reason.into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;
    use std::path::PathBuf;

    use super::*;

    /// A directory of its own for one test, removed again when dropped.
    struct Scratch(PathBuf);

    impl Scratch {
        fn new(name: &str) -> Scratch {
            let path =
                std::env::temp_dir().join(format!("triage-wrapper-{}-{name}", std::process::id()));
            std::fs::remove_dir_all(&path).ok();
            std::fs::create_dir_all(&path).unwrap();
            Scratch(path)
        }

        /// A stand-in for the program: a shell script that is given `--self-test`.
        fn program(&self, script: &str) -> PathBuf {
            use std::os::unix::fs::PermissionsExt;
            let path = self.0.join("program");
            std::fs::write(&path, format!("#!/bin/sh\n{script}\n")).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
            path
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.0).ok();
        }
    }

    fn subject(web_view: &str, program: &str) -> Subject {
        Subject {
            web_view: web_view.into(),
            program: program.into(),
        }
    }

    #[test]
    fn the_test_runs_once_per_web_view_and_program() {
        let scratch = Scratch::new("once");
        let runs = Cell::new(0);
        let pass = || {
            runs.set(runs.get() + 1);
            Ok(())
        };
        let first = subject("605.1.15", "1-a");

        assert_eq!(ensure(&scratch.0, &first, pass), Ok(true));
        assert_eq!(ensure(&scratch.0, &first, pass), Ok(false));
        assert_eq!(runs.get(), 1);

        // An update of the system's web view, then a new build of the wrapper.
        let updated = subject("605.1.16", "1-a");
        assert_eq!(ensure(&scratch.0, &updated, pass), Ok(true));
        let rebuilt = subject("605.1.16", "2-b");
        assert_eq!(ensure(&scratch.0, &rebuilt, pass), Ok(true));
        assert_eq!(ensure(&scratch.0, &rebuilt, pass), Ok(false));
        assert_eq!(runs.get(), 3);

        // Going back to what passed earlier is tested again: only the last pass is kept.
        assert_eq!(ensure(&scratch.0, &first, pass), Ok(true));
    }

    #[test]
    fn a_failure_is_not_remembered_and_forgets_the_pass_before_it() {
        let scratch = Scratch::new("failure");
        let before = subject("605.1.15", "1-a");
        assert_eq!(ensure(&scratch.0, &before, || Ok(())), Ok(true));

        let updated = subject("605.1.16", "1-a");
        let failed = ensure(&scratch.0, &updated, || Err("did not pass".into()));
        assert_eq!(failed, Err("did not pass".to_string()));
        assert!(!passed_before(&scratch.0, &updated));
        assert!(!passed_before(&scratch.0, &before));
        assert!(!scratch.0.join(RECORD).exists());

        // It is tried again at the next start.
        assert_eq!(ensure(&scratch.0, &updated, || Ok(())), Ok(true));
    }

    #[test]
    fn a_record_that_says_something_else_counts_for_nothing() {
        let scratch = Scratch::new("record");
        let subject = subject("605.1.15", "1-a");
        for content in [
            "",
            "not json",
            "{}",
            "true",
            r#"{"web_view": "605.1.15"}"#,
            r#"{"web_view": "605.1.15", "program": "1-b"}"#,
            r#"{"web_view": "605.1.150", "program": "1-a"}"#,
        ] {
            std::fs::write(scratch.0.join(RECORD), content).unwrap();
            assert!(!passed_before(&scratch.0, &subject), "{content}");
        }
        std::fs::write(
            scratch.0.join(RECORD),
            r#"{"program": "1-a", "web_view": "605.1.15"}"#,
        )
        .unwrap();
        assert!(passed_before(&scratch.0, &subject));
    }

    #[test]
    fn the_subject_changes_with_the_program_and_the_web_view() {
        let scratch = Scratch::new("subject");
        let program = scratch.program("exit 0");
        let first = Subject::new("605.1.15", &program).unwrap();
        assert_eq!(Subject::new("605.1.15", &program).unwrap(), first);
        assert_ne!(Subject::new("605.1.16", &program).unwrap(), first);

        // Same length, different content.
        let program = scratch.program("exit 1");
        assert_ne!(Subject::new("605.1.15", &program).unwrap(), first);
        assert!(Subject::new("605.1.15", &scratch.0.join("missing")).is_err());
    }

    #[test]
    fn the_self_test_is_run_as_a_process_and_judged_by_how_it_ends() {
        let scratch = Scratch::new("process");
        let output = scratch.0.join(OUTPUT);
        let patience = Duration::from_secs(30);

        let passes = scratch.program(r#"echo "asked for: $1"; exit 0"#);
        assert_eq!(run_until(&passes, &output, || false, patience), Ok(()));
        assert_eq!(
            std::fs::read_to_string(&output).unwrap(),
            "asked for: --self-test\n"
        );

        let fails = scratch.program("echo FAILED; exit 1");
        assert_eq!(
            run_until(&fails, &output, || false, patience),
            Err("the self-test did not pass".to_string())
        );
        assert_eq!(std::fs::read_to_string(&output).unwrap(), "FAILED\n");

        let missing = scratch.0.join("missing");
        assert!(run_until(&missing, &output, || false, patience).is_err());
    }

    #[test]
    fn a_self_test_that_hangs_or_is_interrupted_is_ended_and_fails() {
        let scratch = Scratch::new("hangs");
        let output = scratch.0.join(OUTPUT);
        let hangs = scratch.program("exec sleep 600");

        let start = Instant::now();
        assert_eq!(
            run_until(&hangs, &output, || false, Duration::from_millis(300)),
            Err("the self-test did not finish".to_string())
        );
        assert_eq!(
            run_until(&hangs, &output, || true, Duration::from_secs(600)),
            Err("the self-test was interrupted".to_string())
        );
        assert!(start.elapsed() < Duration::from_secs(20));
    }
}
