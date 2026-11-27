//! The sandbox configuration: exactly how the containers and networks are created.
//!
//! Everything here is fixed. None of it is read from files that the coding agent can modify,
//! which is what lets the security requirements hold whatever code ends up inside the images.
//! The functions only build argument lists for the `docker` command line; nothing is executed
//! here.
//!
//! The pieces:
//!
//! * `triage-internal`: an internal network (no route to the outside or to the host). Members:
//!   the email sandbox and the gateway.
//! * `triage-egress`: a network of its own for the gateway's connections to the IMAP servers
//!   and to the local LLM on the host. Only the gateway is attached.
//! * `triage-coding`: a network of its own for the coding sandbox, which has internet access.
//! * `triage-gateway`: the trusted gateway (image built from a reviewed checkout).
//! * `triage-app`: the email sandbox (image built from the app's source).
//! * `triage-coding`: the coding sandbox, with the repository's working tree mounted.

use std::path::{Path, PathBuf};

pub const INTERNAL_NETWORK: &str = "triage-internal";
pub const EGRESS_NETWORK: &str = "triage-egress";
pub const CODING_NETWORK: &str = "triage-coding";
pub const GATEWAY: &str = "triage-gateway";
pub const APP: &str = "triage-app";
pub const CODING: &str = "triage-coding";
pub const GATEWAY_IMAGE: &str = "triage-gateway:local";
pub const CODING_IMAGE: &str = "triage-coding:local";
pub const APP_IMAGE: &str = "triage-app";
pub const CODING_HOME_VOLUME: &str = "triage-coding-home";

/// Environment variables through which secrets are handed to `docker`, so that they do not
/// appear on any command line.
pub const ENV_CONTROL_TOKEN: &str = "TRIAGE_CONTROL_TOKEN";
pub const ENV_UI_TOKEN: &str = "TRIAGE_UI_TOKEN";
pub const ENV_LLM_API_KEY: &str = "TRIAGE_LLM_API_KEY";
pub const ENV_CODING_API_KEY: &str = "ANTHROPIC_API_KEY";

/// An address from a range reserved for documentation, which is never routed. Used as the
/// "upstream" DNS server of the email sandbox, so that names outside the internal network
/// cannot be resolved (and DNS cannot be used to smuggle data out).
const UNROUTABLE_DNS: &str = "192.0.2.1";

pub type DockerArgs = Vec<String>;

/// A directory that the user has explicitly made available to the email sandbox.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Grant {
    pub path: PathBuf,
    /// Name under which the directory appears in the sandbox (`/data/extra/<name>`).
    pub name: String,
    pub read_only: bool,
}

#[derive(Clone, Debug)]
pub struct Layout {
    /// The directory for persistent data, normally `~/.triage`.
    pub data: PathBuf,
    /// The working tree of the app's repository, which the coding agent may modify.
    pub repo: PathBuf,
    /// User and group that own the data directory on the host.
    pub uid: u32,
    pub gid: u32,
    /// Ports on the host's loopback interface.
    pub control_port: u16,
    pub ui_port: u16,
    /// Port of the local LLM server on the host, if there is one.
    pub llm_port: Option<u16>,
    pub grants: Vec<Grant>,
}

impl Layout {
    pub fn cache_dir(&self) -> PathBuf {
        self.data.join("cache")
    }

    pub fn config_dir(&self) -> PathBuf {
        self.data.join("config")
    }

    pub fn gateway_dir(&self) -> PathBuf {
        self.data.join("gateway")
    }
}

fn args(items: &[&str]) -> DockerArgs {
    items.iter().map(|s| s.to_string()).collect()
}

/// A path that can safely be written into a `--mount` option, whose fields are separated by
/// commas.
pub fn check_mount_path(path: &Path) -> Result<&str, String> {
    let text = path
        .to_str()
        .ok_or_else(|| format!("{} is not valid UTF-8", path.display()))?;
    if !path.is_absolute() {
        return Err(format!("{text} is not an absolute path"));
    }
    if text.contains([',', '\n', '\r', '"']) || text.chars().any(|c| c.is_control()) {
        return Err(format!(
            "{text:?} contains a character that is not allowed in a mount path"
        ));
    }
    Ok(text)
}

fn bind(source: &Path, target: &str, read_only: bool) -> Result<String, String> {
    let source = check_mount_path(source)?;
    let mut mount = format!("type=bind,src={source},dst={target}");
    if read_only {
        mount.push_str(",readonly");
    }
    Ok(mount)
}

pub fn validate_grant(grant: &Grant, layout: &Layout) -> Result<(), String> {
    check_mount_path(&grant.path)?;
    let name_ok = (1..=32).contains(&grant.name.len())
        && grant
            .name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
    if !name_ok {
        return Err("the name must be 1 to 32 characters from a-z, 0-9 and '-'".into());
    }
    let forbidden = [
        Path::new("/"),
        Path::new("/var/run/docker.sock"),
        Path::new("/run/docker.sock"),
    ];
    if forbidden.contains(&grant.path.as_path()) {
        return Err(format!("{} cannot be shared", grant.path.display()));
    }
    // Neither the gateway's data (the operation log) nor anything that contains it.
    let gateway = layout.gateway_dir();
    if grant.path.starts_with(&gateway) || gateway.starts_with(&grant.path) {
        return Err(format!(
            "{} overlaps with the gateway's data directory",
            grant.path.display()
        ));
    }
    // Sharing the repository would let the email sandbox write code, and hand email data to
    // the coding sandbox.
    if grant.path.starts_with(&layout.repo) || layout.repo.starts_with(&grant.path) {
        return Err(format!(
            "{} overlaps with the app's repository",
            grant.path.display()
        ));
    }
    Ok(())
}

pub fn create_networks() -> Vec<DockerArgs> {
    vec![
        args(&["network", "create", "--internal", INTERNAL_NETWORK]),
        args(&["network", "create", EGRESS_NETWORK]),
        args(&["network", "create", CODING_NETWORK]),
    ]
}

/// Options shared by the two containers that handle email data.
fn hardened(name: &str, layout: &Layout) -> DockerArgs {
    let mut a = args(&[
        "--name",
        name,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--restart",
        "no",
    ]);
    a.extend([
        "--user".to_string(),
        format!("{}:{}", layout.uid, layout.gid),
    ]);
    a
}

/// Creates (but does not start) the gateway container. The caller's environment must contain
/// the two tokens, and the LLM API key if there is an LLM.
pub fn create_gateway(layout: &Layout) -> Result<DockerArgs, String> {
    let mut a = args(&["create"]);
    a.extend(hardened(GATEWAY, layout));
    a.extend(args(&[
        "--network",
        EGRESS_NETWORK,
        "--add-host",
        "host.docker.internal:host-gateway",
        "--pids-limit",
        "256",
        "--memory",
        "512m",
    ]));
    for port in [(layout.control_port, 8101), (layout.ui_port, 8102)] {
        // Published on the loopback interface only.
        a.extend([
            "--publish".to_string(),
            format!("127.0.0.1:{}:{}", port.0, port.1),
        ]);
    }
    a.extend([
        "--mount".to_string(),
        bind(&layout.gateway_dir(), "/data", false)?,
    ]);
    a.extend(args(&[
        "--env",
        ENV_CONTROL_TOKEN,
        "--env",
        ENV_UI_TOKEN,
        "--env",
        "TRIAGE_APP_URL=http://app:8080",
        "--env",
        "TRIAGE_DATA_DIR=/data",
    ]));
    if let Some(port) = layout.llm_port {
        a.extend([
            "--env".to_string(),
            format!("TRIAGE_LLM_URL=http://host.docker.internal:{port}"),
            "--env".to_string(),
            ENV_LLM_API_KEY.to_string(),
        ]);
    }
    a.push(GATEWAY_IMAGE.to_string());
    Ok(a)
}

/// Attaches the gateway to the internal network, under the name by which the app finds it.
pub fn connect_gateway() -> DockerArgs {
    args(&[
        "network",
        "connect",
        "--alias",
        "gateway",
        INTERNAL_NETWORK,
        GATEWAY,
    ])
}

/// Runs the email sandbox from the given image.
pub fn run_app(layout: &Layout, image: &str) -> Result<DockerArgs, String> {
    let mut a = args(&["run", "--detach"]);
    a.extend(hardened(APP, layout));
    a.extend(args(&[
        // The only network is the internal one: no internet, no host, no other containers.
        "--network",
        INTERNAL_NETWORK,
        "--network-alias",
        "app",
        "--dns",
        UNROUTABLE_DNS,
        "--pids-limit",
        "512",
        "--memory",
        "2g",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=256m",
    ]));
    a.extend([
        "--mount".to_string(),
        bind(&layout.cache_dir(), "/data/cache", false)?,
    ]);
    a.extend([
        "--mount".to_string(),
        bind(&layout.config_dir(), "/data/config", false)?,
    ]);
    for grant in &layout.grants {
        validate_grant(grant, layout)?;
        let target = format!("/data/extra/{}", grant.name);
        a.extend([
            "--mount".to_string(),
            bind(&grant.path, &target, grant.read_only)?,
        ]);
    }
    a.extend(args(&[
        "--env",
        "TRIAGE_GATEWAY_URL=http://gateway:8100",
        "--env",
        "TRIAGE_CACHE_DIR=/data/cache",
        "--env",
        "TRIAGE_CONFIG_DIR=/data/config",
        "--env",
        "HOME=/tmp",
    ]));
    a.push(image.to_string());
    Ok(a)
}

/// Runs the coding sandbox. It has internet access and the repository, and nothing else: no
/// email data, no configuration, no credentials apart from the one for the coding model.
pub fn run_coding(layout: &Layout, with_api_key: bool) -> Result<DockerArgs, String> {
    let mut a = args(&[
        "run",
        "--detach",
        "--name",
        CODING,
        "--network",
        CODING_NETWORK,
        "--restart",
        "no",
        "--pids-limit",
        "4096",
    ]);
    a.extend([
        "--mount".to_string(),
        bind(&layout.repo, "/workspace", false)?,
    ]);
    a.extend([
        "--mount".to_string(),
        format!("type=volume,src={CODING_HOME_VOLUME},dst=/home/coder"),
    ]);
    if with_api_key {
        a.extend(args(&["--env", ENV_CODING_API_KEY]));
    }
    a.extend(args(&[CODING_IMAGE, "sleep", "infinity"]));
    Ok(a)
}

/// Builds the gateway image from a reviewed checkout of the trusted base.
pub fn build_gateway(trusted_source: &Path) -> Result<DockerArgs, String> {
    let source = check_mount_path(trusted_source)?;
    Ok(args(&[
        "build",
        "--file",
        &format!("{source}/docker/gateway.Dockerfile"),
        "--tag",
        GATEWAY_IMAGE,
        source,
    ]))
}

/// Builds the coding sandbox image. The build context is the `coding` directory of the working
/// tree; building only reads those files inside Docker's build sandbox.
pub fn build_coding(repo: &Path) -> Result<DockerArgs, String> {
    let repo = check_mount_path(repo)?;
    Ok(args(&[
        "build",
        "--tag",
        CODING_IMAGE,
        &format!("{repo}/coding"),
    ]))
}

/// Builds an app image from a build context that is supplied on standard input as a tar
/// archive (the committed `app` directory, exported inside the coding sandbox). No build
/// arguments, secrets, SSH agents or host networking are passed, whatever the Dockerfile asks
/// for.
pub fn build_app(image: &str) -> DockerArgs {
    args(&["build", "--tag", image, "-"])
}

/// Exports the committed state of the `app` directory as a tar archive on standard output.
/// Runs inside the coding sandbox, so that Git never runs on the host in a working tree that
/// the coding agent controls.
pub fn export_app_source() -> DockerArgs {
    args(&[
        "exec",
        CODING,
        "git",
        "-C",
        "/workspace",
        "archive",
        "--format=tar",
        "HEAD:app",
    ])
}

/// Asks the coding sandbox which Git tree the committed `app` directory is.
pub fn tree_of_app_source() -> DockerArgs {
    args(&[
        "exec",
        CODING,
        "git",
        "-C",
        "/workspace",
        "rev-parse",
        "HEAD:app",
    ])
}

pub fn remove(container: &str) -> DockerArgs {
    args(&["rm", "--force", container])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn layout() -> Layout {
        Layout {
            data: PathBuf::from("/Users/robin/.triage"),
            repo: PathBuf::from("/Users/robin/src/triage"),
            uid: 501,
            gid: 20,
            control_port: 8101,
            ui_port: 8102,
            llm_port: Some(8012),
            grants: vec![],
        }
    }

    fn values<'a>(args: &'a [String], option: &str) -> Vec<&'a str> {
        args.windows(2)
            .filter(|w| w[0] == option)
            .map(|w| w[1].as_str())
            .collect()
    }

    fn has(args: &[String], option: &str, value: &str) -> bool {
        values(args, option).contains(&value)
    }

    #[test]
    fn the_internal_network_is_internal() {
        let networks = create_networks();
        let internal = networks
            .iter()
            .find(|n| n.contains(&INTERNAL_NETWORK.to_string()))
            .unwrap();
        assert!(internal.contains(&"--internal".to_string()));
        for other in [EGRESS_NETWORK, CODING_NETWORK] {
            let network = networks
                .iter()
                .find(|n| n.last().unwrap() == other)
                .unwrap();
            assert!(!network.contains(&"--internal".to_string()));
        }
    }

    #[test]
    fn the_email_sandbox_is_locked_down() {
        let a = run_app(&layout(), "triage-app:7").unwrap();
        assert_eq!(
            values(&a, "--network"),
            vec![INTERNAL_NETWORK],
            "exactly one network"
        );
        assert!(has(&a, "--cap-drop", "ALL"));
        assert!(has(&a, "--security-opt", "no-new-privileges"));
        assert!(a.contains(&"--read-only".to_string()));
        assert!(has(&a, "--user", "501:20"));
        assert!(has(&a, "--dns", "192.0.2.1"));
        assert!(has(&a, "--network-alias", "app"));
        assert_eq!(a.last().unwrap(), "triage-app:7");
        // Nothing that would open a way out.
        for forbidden in [
            "--publish",
            "-p",
            "--privileged",
            "--cap-add",
            "--add-host",
            "--volume",
            "-v",
            "--pid",
            "--ipc",
            "--device",
            "--volumes-from",
            "--env-file",
        ] {
            assert!(!a.contains(&forbidden.to_string()), "{forbidden}");
        }
        // Exactly the cache and the configuration are mounted.
        assert_eq!(
            values(&a, "--mount"),
            vec![
                "type=bind,src=/Users/robin/.triage/cache,dst=/data/cache",
                "type=bind,src=/Users/robin/.triage/config,dst=/data/config",
            ]
        );
        let joined = a.join(" ");
        assert!(!joined.contains("docker.sock"));
        assert!(
            !joined.contains("TOKEN") && !joined.contains("API_KEY"),
            "no secrets"
        );
        assert!(
            !joined.contains("/Users/robin/src"),
            "no access to the repository"
        );
        assert!(
            !joined.contains(".triage/gateway"),
            "no access to the operation log"
        );
    }

    #[test]
    fn granted_directories_are_mounted_where_the_user_said() {
        let mut layout = layout();
        layout.grants = vec![
            Grant {
                path: PathBuf::from("/Users/robin/Documents/Invoices"),
                name: "invoices".into(),
                read_only: true,
            },
            Grant {
                path: PathBuf::from("/Users/robin/Export"),
                name: "export".into(),
                read_only: false,
            },
        ];
        let a = run_app(&layout, "triage-app:1").unwrap();
        let mounts = values(&a, "--mount");
        assert!(mounts.contains(
            &"type=bind,src=/Users/robin/Documents/Invoices,dst=/data/extra/invoices,readonly"
        ));
        assert!(mounts.contains(&"type=bind,src=/Users/robin/Export,dst=/data/extra/export"));
    }

    #[test]
    fn dangerous_grants_are_refused() {
        let layout = layout();
        let grant = |path: &str, name: &str| Grant {
            path: PathBuf::from(path),
            name: name.into(),
            read_only: true,
        };
        validate_grant(&grant("/Users/robin/Documents", "docs"), &layout).unwrap();
        for (path, name) in [
            ("/", "root"),
            ("/var/run/docker.sock", "docker"),
            ("/Users/robin/.triage/gateway", "log"),
            ("/Users/robin/.triage", "all"),
            ("/Users/robin", "home"),
            ("/Users/robin/src/triage", "repo"),
            ("/Users/robin/src/triage/app", "code"),
            ("relative/path", "rel"),
            ("/Users/robin/a,dst=/etc", "inject"),
            ("/Users/robin/a\nb", "newline"),
            ("/Users/robin/Documents", "Bad Name"),
            ("/Users/robin/Documents", "../up"),
            ("/Users/robin/Documents", ""),
        ] {
            assert!(
                validate_grant(&grant(path, name), &layout).is_err(),
                "{path:?} {name:?}"
            );
        }
        let mut with_bad_grant = layout.clone();
        with_bad_grant.grants = vec![grant("/Users/robin/src/triage", "repo")];
        assert!(run_app(&with_bad_grant, "triage-app:1").is_err());
    }

    #[test]
    fn the_gateway_publishes_only_on_loopback() {
        let a = create_gateway(&layout()).unwrap();
        assert_eq!(a[0], "create");
        assert_eq!(
            values(&a, "--publish"),
            vec!["127.0.0.1:8101:8101", "127.0.0.1:8102:8102"],
            "the sandbox API port 8100 is not published"
        );
        assert_eq!(values(&a, "--network"), vec![EGRESS_NETWORK]);
        assert!(has(&a, "--cap-drop", "ALL"));
        assert!(has(&a, "--security-opt", "no-new-privileges"));
        assert!(a.contains(&"--read-only".to_string()));
        assert_eq!(
            values(&a, "--mount"),
            vec!["type=bind,src=/Users/robin/.triage/gateway,dst=/data"]
        );
        // Secrets are passed by name only; their values come from the environment.
        let env = values(&a, "--env");
        assert!(env.contains(&"TRIAGE_CONTROL_TOKEN"));
        assert!(env.contains(&"TRIAGE_UI_TOKEN"));
        assert!(env.contains(&"TRIAGE_LLM_API_KEY"));
        assert!(env.contains(&"TRIAGE_LLM_URL=http://host.docker.internal:8012"));
        assert_eq!(a.last().unwrap(), GATEWAY_IMAGE);
        assert_eq!(
            connect_gateway(),
            vec![
                "network",
                "connect",
                "--alias",
                "gateway",
                INTERNAL_NETWORK,
                GATEWAY
            ]
        );

        let mut without_llm = layout();
        without_llm.llm_port = None;
        let a = create_gateway(&without_llm).unwrap();
        assert!(!a.join(" ").contains("LLM"));
    }

    #[test]
    fn the_coding_sandbox_sees_the_repository_and_nothing_else() {
        let a = run_coding(&layout(), false).unwrap();
        assert_eq!(values(&a, "--network"), vec![CODING_NETWORK]);
        assert_eq!(
            values(&a, "--mount"),
            vec![
                "type=bind,src=/Users/robin/src/triage,dst=/workspace",
                "type=volume,src=triage-coding-home,dst=/home/coder",
            ]
        );
        let joined = a.join(" ");
        assert!(
            !joined.contains(".triage"),
            "no email data, configuration, or log"
        );
        assert!(!joined.contains("docker.sock"));
        assert!(!joined.contains("--privileged") && !joined.contains("--publish"));
        assert!(values(&a, "--env").is_empty());
        let with_key = run_coding(&layout(), true).unwrap();
        assert_eq!(values(&with_key, "--env"), vec!["ANTHROPIC_API_KEY"]);
    }

    #[test]
    fn builds_take_nothing_from_the_host_but_their_context() {
        let app = build_app("triage-app:3");
        assert_eq!(app, vec!["build", "--tag", "triage-app:3", "-"]);
        let gateway = build_gateway(Path::new("/Users/robin/reviewed/trusted")).unwrap();
        assert_eq!(gateway.last().unwrap(), "/Users/robin/reviewed/trusted");
        let coding = build_coding(Path::new("/Users/robin/src/triage")).unwrap();
        assert_eq!(coding.last().unwrap(), "/Users/robin/src/triage/coding");
        for build in [app, gateway, coding] {
            for forbidden in [
                "--build-arg",
                "--secret",
                "--ssh",
                "--network",
                "--allow",
                "--privileged",
            ] {
                assert!(!build.contains(&forbidden.to_string()), "{forbidden}");
            }
        }
        assert!(export_app_source().starts_with(&[
            "exec".to_string(),
            CODING.to_string(),
            "git".to_string()
        ]));
    }
}
