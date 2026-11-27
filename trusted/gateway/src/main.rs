//! The gateway process. It is configured through environment variables by the wrapper app:
//!
//! | Variable                     | Meaning                                              | Default            |
//! |------------------------------|------------------------------------------------------|--------------------|
//! | `TRIAGE_CONTROL_TOKEN`       | secret for the control API (at least 32 characters)  | required           |
//! | `TRIAGE_UI_TOKEN`            | secret for the web interface relay (ditto)           | required           |
//! | `TRIAGE_SANDBOX_ADDR`        | listener for the email sandbox                       | `0.0.0.0:8100`     |
//! | `TRIAGE_CONTROL_ADDR`        | listener for the control API                         | `0.0.0.0:8101`     |
//! | `TRIAGE_UI_ADDR`             | listener for the web interface relay                 | `0.0.0.0:8102`     |
//! | `TRIAGE_APP_URL`             | the app's web server in the email sandbox            | `http://app:8080`  |
//! | `TRIAGE_DATA_DIR`            | directory for the operation log                      | `/data`            |
//! | `TRIAGE_DELETION_LIMIT`      | deletions per hour above which the user is warned    | `20`               |
//! | `TRIAGE_ACCOUNTS_FILE`       | JSON file with accounts, instead of the control API  | none               |
//! | `TRIAGE_LLM_URL`             | base URL of the local LLM server                     | none               |
//! | `TRIAGE_LLM_API_KEY`         | API key for the local LLM server                     | none               |
//! | `TRIAGE_ALLOW_INSECURE_IMAP` | `1` permits accounts without TLS (for tests)         | off                |
//!
//! The control and web interface listeners must only be published on the host's loopback
//! interface, and the sandbox listener must only be reachable from the email sandbox.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use triage_gateway::config::{AccountsConfig, LlmConfig, Secret};
use triage_gateway::state::{Gateway, Settings};
use triage_gateway::{api, host};

fn var(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|value| !value.is_empty())
}

fn secret(name: &str) -> Result<Secret, String> {
    let value = var(name).ok_or_else(|| format!("{name} is not set"))?;
    if value.len() < 32 {
        return Err(format!("{name} must have at least 32 characters"));
    }
    Ok(Secret::new(value))
}

fn address(name: &str, default: &str) -> Result<SocketAddr, String> {
    var(name)
        .unwrap_or_else(|| default.to_string())
        .parse()
        .map_err(|_| format!("{name} is not a socket address"))
}

async fn run() -> Result<(), String> {
    let control_token = secret("TRIAGE_CONTROL_TOKEN")?;
    let ui_token = secret("TRIAGE_UI_TOKEN")?;
    if control_token == ui_token {
        return Err("TRIAGE_CONTROL_TOKEN and TRIAGE_UI_TOKEN must differ".into());
    }
    let settings = Settings {
        data_dir: PathBuf::from(var("TRIAGE_DATA_DIR").unwrap_or_else(|| "/data".into())),
        control_token,
        ui_token,
        app_url: var("TRIAGE_APP_URL").unwrap_or_else(|| "http://app:8080".into()),
        allow_insecure_imap: var("TRIAGE_ALLOW_INSECURE_IMAP").as_deref() == Some("1"),
        deletion_limit: match var("TRIAGE_DELETION_LIMIT") {
            Some(value) => value
                .parse()
                .map_err(|_| "TRIAGE_DELETION_LIMIT is not a number".to_string())?,
            None => 20,
        },
    };
    let sandbox_addr = address("TRIAGE_SANDBOX_ADDR", "0.0.0.0:8100")?;
    let control_addr = address("TRIAGE_CONTROL_ADDR", "0.0.0.0:8101")?;
    let ui_addr = address("TRIAGE_UI_ADDR", "0.0.0.0:8102")?;

    let gateway = Arc::new(
        Gateway::new(settings).map_err(|e| format!("could not open the data directory: {e}"))?,
    );
    if let Some(path) = var("TRIAGE_ACCOUNTS_FILE") {
        let text = std::fs::read_to_string(&path).map_err(|e| format!("{path}: {e}"))?;
        // Parse errors are not printed in detail: they may quote parts of the credentials.
        let accounts: AccountsConfig = serde_json::from_str(&text)
            .map_err(|_| format!("{path} is not a valid accounts file"))?;
        gateway
            .set_accounts(accounts)
            .map_err(|e| format!("{path}: {}", e.message))?;
    }
    if let Some(base_url) = var("TRIAGE_LLM_URL") {
        let config = LlmConfig {
            base_url,
            api_key: var("TRIAGE_LLM_API_KEY").map(Secret::new),
        };
        gateway
            .set_llm(Some(config))
            .map_err(|e| format!("TRIAGE_LLM_URL: {}", e.message))?;
    }

    let listen = async |addr: SocketAddr| {
        tokio::net::TcpListener::bind(addr)
            .await
            .map_err(|e| format!("could not listen on {addr}: {e}"))
    };
    let sandbox = listen(sandbox_addr).await?;
    let control = listen(control_addr).await?;
    let ui = listen(ui_addr).await?;
    tracing::info!(
        "sandbox API on {sandbox_addr}, control API on {control_addr}, web interface relay on {ui_addr}"
    );

    let serve = async |listener, router| {
        axum::serve(listener, router)
            .with_graceful_shutdown(shutdown())
            .await
            .map_err(|e| format!("server error: {e}"))
    };
    tokio::try_join!(
        serve(sandbox, api::router(gateway.clone())),
        serve(control, host::control_router(gateway.clone())),
        serve(ui, host::ui_router(gateway.clone())),
    )?;
    Ok(())
}

async fn shutdown() {
    use tokio::signal::unix::{SignalKind, signal};
    let mut terminate = signal(SignalKind::terminate()).expect("install the SIGTERM handler");
    tokio::select! {
        _ = terminate.recv() => {}
        _ = tokio::signal::ctrl_c() => {}
    }
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_ansi(false)
        .init();
    if let Err(message) = run().await {
        tracing::error!("{message}");
        std::process::exit(1);
    }
}
