//! Building and replacing the app in the email sandbox.
//!
//! The app's source is taken from the last commit in the repository (exported inside the coding
//! sandbox), built into a new image, and started in place of the running one. If the build
//! fails (the app's tests run as part of it) the running app is left alone. If the new version
//! does not come up healthy, the previous image is started again.

use crate::docker::Docker;
use crate::sandbox::{self, Layout};
use crate::settings::State;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// The committed source is what is already running.
    Unchanged,
    /// A new version is running.
    Updated { image: String },
    /// The new version could not be built; the previous version keeps running.
    BuildFailed { reason: String },
    /// The new version did not start properly; the previous version was started again.
    RolledBack { failed_image: String },
    /// The new version did not start properly, and there is no previous version.
    Failed { failed_image: String },
}

fn start(docker: &dyn Docker, layout: &Layout, image: &str) -> Result<(), String> {
    // The container may or may not exist.
    docker.run(&sandbox::remove(sandbox::APP), &[]).ok();
    docker
        .run(&sandbox::run_app(layout, image)?, &[])
        .map(|_| ())
}

/// Starts the app from the image recorded in `state`, without building anything.
pub fn start_current(docker: &dyn Docker, layout: &Layout, state: &State) -> Result<bool, String> {
    match &state.current_image {
        Some(image) => start(docker, layout, image).map(|()| true),
        None => Ok(false),
    }
}

/// Brings the email sandbox up to date with the committed source. `healthy` is asked, after a
/// new version has been started, whether it works.
pub fn deploy(
    docker: &dyn Docker,
    layout: &Layout,
    state: &mut State,
    force: bool,
    healthy: &dyn Fn() -> bool,
) -> Result<Outcome, String> {
    let tree = docker
        .run(&sandbox::tree_of_app_source(), &[])
        .map_err(|e| format!("could not determine the committed version of the app: {e}"))?;
    let tree_ok = tree.len() >= 40 && tree.bytes().all(|b| b.is_ascii_hexdigit());
    if !tree_ok {
        return Err("the coding sandbox reported an invalid version of the app".into());
    }
    if !force && state.current_tree.as_deref() == Some(&tree) {
        return Ok(Outcome::Unchanged);
    }

    let generation = state.generation + 1;
    let image = format!("{}:{generation}", sandbox::APP_IMAGE);
    state.generation = generation;
    if let Err(reason) = docker.pipe(&sandbox::export_app_source(), &sandbox::build_app(&image)) {
        return Ok(Outcome::BuildFailed { reason });
    }

    let previous = state.current_image.clone();
    start(docker, layout, &image)?;
    if healthy() {
        state.current_image = Some(image.clone());
        state.current_tree = Some(tree);
        return Ok(Outcome::Updated { image });
    }
    match previous {
        Some(previous) => {
            start(docker, layout, &previous)?;
            Ok(Outcome::RolledBack {
                failed_image: image,
            })
        }
        None => {
            docker.run(&sandbox::remove(sandbox::APP), &[]).ok();
            Ok(Outcome::Failed {
                failed_image: image,
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::path::PathBuf;

    /// Records what it is asked to do; fails builds on request.
    #[derive(Default)]
    struct FakeDocker {
        calls: RefCell<Vec<String>>,
        tree: String,
        build_fails: bool,
    }

    impl Docker for FakeDocker {
        fn run(&self, args: &[String], _env: &[(&str, &str)]) -> Result<String, String> {
            self.calls.borrow_mut().push(args.join(" "));
            if args.contains(&"rev-parse".to_string()) {
                return Ok(self.tree.clone());
            }
            Ok(String::new())
        }

        fn pipe(&self, from: &[String], to: &[String]) -> Result<(), String> {
            self.calls
                .borrow_mut()
                .push(format!("{} | {}", from.join(" "), to.join(" ")));
            if self.build_fails {
                Err("tests failed".into())
            } else {
                Ok(())
            }
        }

        fn stream(&self, _: &[String], _: &str, _: &mut dyn FnMut(&str)) -> Result<bool, String> {
            unreachable!()
        }

        fn interactive(&self, _: &[String]) -> Result<bool, String> {
            unreachable!()
        }
    }

    fn layout() -> Layout {
        Layout {
            data: PathBuf::from("/home/robin/.triage"),
            repo: PathBuf::from("/home/robin/src/triage"),
            uid: 1000,
            gid: 1000,
            control_port: 8101,
            ui_port: 8102,
            llm_port: None,
            grants: vec![],
        }
    }

    fn docker(tree: &str) -> FakeDocker {
        FakeDocker {
            tree: tree.repeat(40),
            ..FakeDocker::default()
        }
    }

    fn started(docker: &FakeDocker) -> Vec<String> {
        docker
            .calls
            .borrow()
            .iter()
            .filter(|call| call.starts_with("run "))
            .map(|call| call.rsplit(' ').next().unwrap().to_string())
            .collect()
    }

    #[test]
    fn first_deployment() {
        let docker = docker("a");
        let mut state = State::default();
        let outcome = deploy(&docker, &layout(), &mut state, false, &|| true).unwrap();
        assert_eq!(
            outcome,
            Outcome::Updated {
                image: "triage-app:1".into()
            }
        );
        assert_eq!(state.current_image.as_deref(), Some("triage-app:1"));
        assert_eq!(state.current_tree.as_deref(), Some("a".repeat(40).as_str()));
        assert_eq!(started(&docker), vec!["triage-app:1"]);
        let calls = docker.calls.borrow();
        assert!(
            calls
                .iter()
                .any(|c| c.contains("archive") && c.contains("| build --tag triage-app:1 -"))
        );

        // Nothing to do when the committed source has not changed.
        drop(calls);
        let again = deploy(&docker, &layout(), &mut state, false, &|| true).unwrap();
        assert_eq!(again, Outcome::Unchanged);
        assert_eq!(started(&docker).len(), 1);
        // ... unless a rebuild is forced.
        let forced = deploy(&docker, &layout(), &mut state, true, &|| true).unwrap();
        assert_eq!(
            forced,
            Outcome::Updated {
                image: "triage-app:2".into()
            }
        );
    }

    #[test]
    fn a_failed_build_leaves_the_running_app_alone() {
        let mut state = State {
            generation: 4,
            current_image: Some("triage-app:4".into()),
            current_tree: Some("a".repeat(40)),
        };
        let docker = FakeDocker {
            build_fails: true,
            ..docker("b")
        };
        let outcome = deploy(&docker, &layout(), &mut state, false, &|| true).unwrap();
        assert_eq!(
            outcome,
            Outcome::BuildFailed {
                reason: "tests failed".into()
            }
        );
        assert_eq!(state.current_image.as_deref(), Some("triage-app:4"));
        assert_eq!(state.current_tree.as_deref(), Some("a".repeat(40).as_str()));
        assert_eq!(state.generation, 5, "image numbers are not reused");
        assert!(started(&docker).is_empty());
        assert!(!docker.calls.borrow().iter().any(|c| c.starts_with("rm ")));
    }

    #[test]
    fn an_unhealthy_version_is_rolled_back() {
        let mut state = State {
            generation: 4,
            current_image: Some("triage-app:4".into()),
            current_tree: Some("a".repeat(40)),
        };
        let docker = docker("b");
        let outcome = deploy(&docker, &layout(), &mut state, false, &|| false).unwrap();
        assert_eq!(
            outcome,
            Outcome::RolledBack {
                failed_image: "triage-app:5".into()
            }
        );
        assert_eq!(started(&docker), vec!["triage-app:5", "triage-app:4"]);
        assert_eq!(state.current_image.as_deref(), Some("triage-app:4"));
        assert_eq!(state.current_tree.as_deref(), Some("a".repeat(40).as_str()));
        // The broken commit is tried again only when the source changes or a rebuild is forced.
        assert_eq!(state.generation, 5);
    }

    #[test]
    fn an_unhealthy_first_version_is_stopped() {
        let docker = docker("a");
        let mut state = State::default();
        let outcome = deploy(&docker, &layout(), &mut state, false, &|| false).unwrap();
        assert_eq!(
            outcome,
            Outcome::Failed {
                failed_image: "triage-app:1".into()
            }
        );
        assert_eq!(state.current_image, None);
        assert!(
            docker
                .calls
                .borrow()
                .last()
                .unwrap()
                .starts_with("rm --force triage-app")
        );
    }

    #[test]
    fn a_bogus_version_from_the_coding_sandbox_is_rejected() {
        let docker = FakeDocker {
            tree: "--output=/etc/passwd".into(),
            ..FakeDocker::default()
        };
        assert!(deploy(&docker, &layout(), &mut State::default(), false, &|| true).is_err());
    }

    #[test]
    fn restarting_the_current_version() {
        let docker = docker("a");
        assert!(!start_current(&docker, &layout(), &State::default()).unwrap());
        let state = State {
            generation: 2,
            current_image: Some("triage-app:2".into()),
            current_tree: None,
        };
        assert!(start_current(&docker, &layout(), &state).unwrap());
        assert_eq!(started(&docker), vec!["triage-app:2"]);
    }
}
