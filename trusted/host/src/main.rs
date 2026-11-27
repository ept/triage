//! `triage`: the host tool that sets up and supervises the sandboxes.
//!
//! This is the command-line stand-in for the wrapper app. It is part of the trusted base: build
//! it from a fresh clone of the repository at a commit you have reviewed (see README.md).

use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};

use clap::{Parser, Subcommand};
use serde_json::{Value, json};
use triage_host::control::{self, Applied, Control};
use triage_host::docker::{Cli as DockerCli, Docker};
use triage_host::run::{self, Frontend, describe_kind, down, load_accounts, load_config};
use triage_host::sandbox::{self, Grant};
use triage_host::secrets::{self, SecretStore};
use triage_host::settings::{self, HostConfig, Session};

/// The `trusted` directory from which this program was built.
const TRUSTED_SOURCE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/..");

#[derive(Parser)]
#[command(
    name = "triage",
    version,
    about = "Sets up and supervises the Email Triage sandboxes"
)]
struct Arguments {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Choose the repository that the coding agent works on, and create the data directory
    Init {
        /// Working tree of the app's repository (the directory containing SPEC.md)
        #[arg(long)]
        repo: PathBuf,
        /// Port of the local LLM server on this machine
        #[arg(long)]
        llm_port: Option<u16>,
        /// Model for the coding agent
        #[arg(long)]
        coding_model: Option<String>,
    },
    /// Manage IMAP accounts
    Account {
        #[command(subcommand)]
        command: AccountCommand,
    },
    /// Store or remove an API key
    Secret {
        #[command(subcommand)]
        command: SecretCommand,
    },
    /// Give the email sandbox access to a directory
    Grant {
        path: PathBuf,
        /// Name under which it appears in the sandbox (/data/extra/NAME)
        name: String,
        /// Allow the sandbox to write to the directory
        #[arg(long)]
        read_write: bool,
    },
    /// Take away access to a directory
    Revoke { name: String },
    /// Build the gateway image from the reviewed source this program was built from
    BuildGateway,
    /// Start everything and supervise it until interrupted
    Up {
        /// Rebuild the app image even if its source has not changed
        #[arg(long)]
        rebuild: bool,
    },
    /// Stop and remove the containers
    Down,
    /// Show the state of the gateway
    Status,
    /// Log the coding agent in (interactive)
    CodingLogin,
}

#[derive(Subcommand)]
enum AccountCommand {
    /// Add an account, or replace the one with the same id
    Add,
    List,
    Remove {
        id: String,
    },
}

#[derive(Subcommand)]
enum SecretCommand {
    /// API key of the local LLM server
    LlmApiKey {
        #[arg(long)]
        remove: bool,
    },
    /// API key for the coding agent (instead of a subscription login)
    CodingApiKey {
        #[arg(long)]
        remove: bool,
    },
}

fn main() {
    let arguments = Arguments::parse();
    if let Err(message) = run(arguments.command) {
        eprintln!("error: {message}");
        std::process::exit(1);
    }
}

fn prompt(question: &str, default: Option<&str>) -> Result<String, String> {
    match default {
        Some(default) => print!("{question} [{default}]: "),
        None => print!("{question}: "),
    }
    std::io::stdout().flush().ok();
    let mut answer = String::new();
    std::io::stdin()
        .lock()
        .read_line(&mut answer)
        .map_err(|e| format!("could not read from the terminal: {e}"))?;
    let answer = answer.trim();
    Ok(match (answer.is_empty(), default) {
        (true, Some(default)) => default.to_string(),
        _ => answer.to_string(),
    })
}

/// Stores the account list in the Keychain and, if the gateway is running, hands it over at
/// once. A list that the running gateway rejects is not stored.
fn save_accounts(store: &dyn SecretStore, accounts: &[Value]) -> Result<(), String> {
    let list = json!({"accounts": accounts});
    let data = settings::data_dir()?;
    let config: Option<HostConfig> = settings::read(&data.join("host.json"))?;
    let session: Option<Session> = settings::read(&data.join("session.json"))?;
    let applied = match &config {
        Some(config) => control::apply_accounts(config, session.as_ref(), &list)
            .map_err(|reason| format!("the gateway does not accept this: {reason}. Not saved."))?,
        None => Applied::AtNextStart,
    };
    store.set(secrets::ACCOUNTS, &list.to_string())?;
    match applied {
        Applied::Now => println!(
            "Saved, and handed to the running gateway: the app picks the change up by itself."
        ),
        Applied::AtNextStart => println!("Saved. It takes effect at the next `triage up`."),
        Applied::NotNow(reason) => println!(
            "Saved, but the running gateway could not be updated ({reason}). \
             It takes effect at the next `triage up`."
        ),
    }
    Ok(())
}

fn run(command: Command) -> Result<(), String> {
    let data = settings::data_dir()?;
    let docker = DockerCli;
    match command {
        Command::Init {
            repo,
            llm_port,
            coding_model,
        } => {
            let repo = repo
                .canonicalize()
                .map_err(|e| format!("{}: {e}", repo.display()))?;
            if !repo.join("SPEC.md").is_file() || !repo.join("app").is_dir() {
                return Err(format!(
                    "{} does not look like the app's repository",
                    repo.display()
                ));
            }
            sandbox::check_mount_path(&repo)?;
            settings::create_directories(&data)?;
            let previous: Option<HostConfig> = settings::read(&data.join("host.json"))?;
            let config = HostConfig {
                repo,
                llm_port: llm_port.or(previous.as_ref().and_then(|p| p.llm_port)),
                coding_model: coding_model
                    .or(previous.as_ref().and_then(|p| p.coding_model.clone())),
                control_port: previous.as_ref().map_or(8101, |p| p.control_port),
                ui_port: previous.as_ref().map_or(8102, |p| p.ui_port),
                grants: previous.map(|p| p.grants).unwrap_or_default(),
            };
            settings::write(&data.join("host.json"), &config)?;
            println!("Data directory: {}", data.display());
            println!("Repository:     {}", config.repo.display());
            Ok(())
        }
        Command::Account { command } => account(command),
        Command::Secret { command } => {
            let store = secrets::open()?;
            let (key, remove, what) = match command {
                SecretCommand::LlmApiKey { remove } => {
                    (secrets::LLM_API_KEY, remove, "LLM API key")
                }
                SecretCommand::CodingApiKey { remove } => {
                    (secrets::CODING_API_KEY, remove, "coding agent API key")
                }
            };
            if remove {
                store.delete(key)?;
                println!("Removed the {what}.");
            } else {
                let value = rpassword::prompt_password(format!("{what}: "))
                    .map_err(|e| format!("could not read from the terminal: {e}"))?;
                if value.trim().is_empty() {
                    return Err("nothing entered".into());
                }
                store.set(key, value.trim())?;
                println!("Stored the {what}. It takes effect at the next `triage up`.");
            }
            Ok(())
        }
        Command::Grant {
            path,
            name,
            read_write,
        } => {
            let mut config = load_config(&data)?;
            let path = path
                .canonicalize()
                .map_err(|e| format!("{}: {e}", path.display()))?;
            if !path.is_dir() {
                return Err(format!("{} is not a directory", path.display()));
            }
            let grant = Grant {
                path,
                name,
                read_only: !read_write,
            };
            sandbox::validate_grant(&grant, &settings::layout(&data, &config))?;
            config.grants.retain(|g| g.name != grant.name);
            println!(
                "The email sandbox will see {} as /data/extra/{} ({}) from the next `triage up`.",
                grant.path.display(),
                grant.name,
                if grant.read_only {
                    "read-only"
                } else {
                    "read-write"
                }
            );
            config.grants.push(grant);
            settings::write(&data.join("host.json"), &config)
        }
        Command::Revoke { name } => {
            let mut config = load_config(&data)?;
            config.grants.retain(|g| g.name != name);
            settings::write(&data.join("host.json"), &config)
        }
        Command::BuildGateway => {
            let source = Path::new(TRUSTED_SOURCE)
                .canonicalize()
                .map_err(|e| format!("{TRUSTED_SOURCE}: {e}"))?;
            println!("Building the gateway image from {} ...", source.display());
            let ok = docker.interactive(&sandbox::build_gateway(&source)?)?;
            if ok {
                Ok(())
            } else {
                Err("the build failed".into())
            }
        }
        Command::Up { rebuild } => {
            let mut running = run::start(&data, &docker, rebuild, &Terminal)?;
            println!();
            println!("Open the app at:\n  {}", running.control.session_url());
            println!(
                "(A normal browser does not restrict navigation the way the wrapper app will; \
                 do not follow links from the app to other sites in the same window.)"
            );
            println!("Press Ctrl-C to stop.\n");
            run::supervise(&docker, &mut running, &Terminal)
        }
        Command::Down => {
            down(&docker);
            std::fs::remove_file(data.join("session.json")).ok();
            Ok(())
        }
        Command::Status => {
            let config = load_config(&data)?;
            let session: Session = settings::read(&data.join("session.json"))?
                .ok_or("nothing is running (no session)")?;
            let control = Control::new(
                config.control_port,
                config.ui_port,
                &session.control_token,
                &session.ui_token,
            );
            let status = control.status()?;
            println!(
                "{}",
                serde_json::to_string_pretty(&status).expect("serializable")
            );
            println!("App healthy: {}", control.app_is_healthy());
            Ok(())
        }
        Command::CodingLogin => {
            let args: Vec<String> = ["exec", "--interactive", "--tty", sandbox::CODING, "claude"]
                .iter()
                .map(|s| s.to_string())
                .collect();
            println!(
                "Starting the coding agent; type /login, follow the instructions, then /exit."
            );
            docker.interactive(&args).map(|_| ())
        }
    }
}

fn account(command: AccountCommand) -> Result<(), String> {
    let store = secrets::open()?;
    let mut accounts = load_accounts(store.as_ref())?;
    match command {
        AccountCommand::List => {
            for account in &accounts {
                println!(
                    "{}\t{}\t{}:{}",
                    account["id"].as_str().unwrap_or("?"),
                    account["address"].as_str().unwrap_or("?"),
                    account["host"].as_str().unwrap_or("?"),
                    account["port"]
                );
            }
            Ok(())
        }
        AccountCommand::Remove { id } => {
            accounts.retain(|a| a["id"] != id.as_str());
            save_accounts(store.as_ref(), &accounts)
        }
        AccountCommand::Add => {
            println!("IMAP account (password login; OAuth is not available in this tool yet).");
            let id = prompt("Short name for the account (a-z, 0-9, -)", Some("mail"))?;
            let address = prompt("Email address", None)?;
            let display_name = prompt("Your name, as it should appear in drafts", Some(""))?;
            let host = prompt("IMAP server", None)?;
            let port: u16 = prompt("Port", Some("993"))?
                .parse()
                .map_err(|_| "not a port number".to_string())?;
            let security = if port == 143 { "starttls" } else { "tls" };
            let security = prompt("Security (tls or starttls)", Some(security))?;
            if security != "tls" && security != "starttls" {
                return Err("security must be tls or starttls".into());
            }
            let username = prompt("User name", Some(&address))?;
            let password = rpassword::prompt_password("Password (or app password): ")
                .map_err(|e| format!("could not read from the terminal: {e}"))?;
            if password.is_empty() {
                return Err("no password entered".into());
            }
            let mut account = json!({
                "id": id, "address": address, "host": host, "port": port,
                "security": security, "username": username,
                "auth": {"type": "password", "password": password},
            });
            if !display_name.is_empty() {
                account["display_name"] = json!(display_name);
            }
            accounts.retain(|a| a["id"] != id.as_str());
            accounts.push(account);
            save_accounts(store.as_ref(), &accounts)
        }
    }
}

const RULE: &str = "────────────────────────────────────────────────────────────────────────";

/// The terminal as the place where the user is told and asked things.
struct Terminal;

impl Frontend for Terminal {
    fn say(&self, text: &str) {
        println!("{text}");
    }

    fn warn(&self, text: &str) {
        println!("\n\x07WARNING: {text}\nIf you did not ask for this, stop the app with Ctrl-C.\n");
        notify(text);
    }

    fn approve(&self, kind: &str, text: &str) -> Result<bool, String> {
        approve(kind, text)
    }
}

/// Shows the exact text that the app wants to send to the coding agent, and asks the user.
fn approve(kind: &str, text: &str) -> Result<bool, String> {
    let what = describe_kind(kind);
    println!("\n\x07{RULE}");
    println!("The app wants to send {what} to the coding agent.");
    println!("If you approve, the text below leaves this computer and is sent to a remote model.");
    println!("Check that it contains nothing from your email that you want to keep private.");
    println!("{RULE}");
    // The gateway only accepts printable text, so nothing here can disguise the terminal output.
    println!("{text}");
    println!("{RULE}");
    let answer = prompt("Send it? (yes/no)", Some("no"))?;
    Ok(matches!(answer.to_ascii_lowercase().as_str(), "y" | "yes"))
}

/// Shows a desktop notification, where that is possible.
fn notify(text: &str) {
    #[cfg(target_os = "macos")]
    {
        // The text is built from numbers and an account id, which cannot contain quotes.
        let script = format!(
            "display notification \"{}\" with title \"Email Triage\"",
            text.replace(['"', '\\'], "")
        );
        std::process::Command::new("osascript")
            .args(["-e", &script])
            .status()
            .ok();
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = text;
    }
}
