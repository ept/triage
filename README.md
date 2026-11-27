Email Triage
============

An AI app that helps you work through an overflowing inbox, built so that your email never
leaves your machine and your mail server. The design and its security requirements are in
[`SPEC.md`](SPEC.md).

How it fits together
--------------------

```
                         your Mac
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  triage (host tool)      local LLM server            web view / browser  │
 │   │ creates containers,   (llama-server, GPU,          │                 │
 │   │ holds secrets,         no network access)          │                 │
 │   │ asks for approval           ▲                      │                 │
 │ ┌─┼─────────────────────────────┼──────────────────────┼───────────────┐ │
 │ │ ▼            Docker           │                      ▼               │ │
 │ │  ┌───────────────┐   ┌────────┴───────────────────────────────┐      │ │
 │ │  │ email sandbox │──▶│ gateway (trusted)                      │──────┼─┼──▶ IMAP servers
 │ │  │  the app      │   │  holds credentials, allows only a      │      │ │
 │ │  │  no network   │   │  fixed set of mailbox operations,      │      │ │
 │ │  └───────────────┘   │  relays to the LLM and the web view    │      │ │
 │ │         ▲            └────────────────────────────────────────┘      │ │
 │ │         │ app image, built from committed source                     │ │
 │ │  ┌───────────────┐                                                   │ │
 │ │  │ coding sandbox│───────────────────────────────────────────────────┼─┼──▶ internet,
 │ │  │  coding agent │   no email data, no credentials                   │ │    coding model
 │ │  └───────────────┘                                                   │ │
 │ └──────────────────────────────────────────────────────────────────────┘ │
 └──────────────────────────────────────────────────────────────────────────┘
```

| Directory  | Contents                                                                    |
|------------|-----------------------------------------------------------------------------|
| `app/`     | The app that runs in the email sandbox. The coding agent changes this.      |
| `trusted/` | The trusted base: `gateway/`, the host tool `host/`, the LLM sandbox `llm/` |
| `coding/`  | The image of the coding sandbox                                             |
| `docs/`    | [`gateway-api.md`](docs/gateway-api.md): the contract between app and gateway |

Status
------

This is an early version. What exists, and how far it has been tested:

| Part                         | State                                                                  |
|------------------------------|------------------------------------------------------------------------|
| Gateway                      | Implemented; tested against a real IMAP server (Dovecot)               |
| App: sync, parsing, search   | Implemented; tested against the real gateway and Dovecot               |
| App: assistant, policy, analysis, web interface | Implemented; tested with a scripted stand-in for the model, and in a browser through the real gateway's relay. **Not yet tried with a real local model** |
| Host tool `triage`           | Implemented; its logic is unit-tested, but it has **not yet been run against Docker or on macOS** |
| Container images             | Dockerfiles written, **not yet built** (the gateway does build as a static binary) |
| LLM sandbox profile          | Written, **not yet tested on macOS**                                   |
| Wrapper app (Tauri)          | Not started; `triage up` and a normal browser stand in for it          |
| OAuth login (Gmail, Microsoft 365) | Gateway side implemented, untested against real providers; no consent flow in the host tool yet |
| Scripts written by the local LLM, text from attachments, automatic dependency updates | Not started |

Until the wrapper app exists, two of the specification's requirements are not met: the web
interface runs in a normal browser, which does not stop a page from navigating elsewhere, and the
approval of change requests happens in the terminal rather than in a native dialog.

Other known gaps: the policy is applied to each new message when it arrives, so rules that depend
on something happening later ("when I have sent the draft, archive the original") are not carried
out yet; server-side search only accepts ASCII; and text that an email hides from human readers
is still visible to the model.

To see the interface without any of the setup below, with made-up mail and a pretend model:

```sh
cd app && uv run python -m triage.testing.demo --port 8765
```

Setting it up (macOS)
---------------------

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/), the Rust toolchain
([rustup.rs](https://rustup.rs)), and, for the local model, `llama.cpp` (`brew install llama.cpp`).

### 1. Two copies of the repository

The coding agent can write to the working tree it is given, including its `.git` directory. The
trusted base must therefore be built from a copy that the agent cannot touch:

```sh
git clone <url> ~/src/triage            # the working copy; the coding agent works here
git clone <url> ~/src/triage-trusted    # a reviewed copy, for the trusted base only
```

Never run Git or build tools on the host inside `~/src/triage` once the coding agent has been
working in it: Git hooks, Git configuration and build scripts can run code. To look at what the
agent did, fetch into the reviewed copy (`git -C ~/src/triage-trusted fetch`) and read it there.

### 2. Build the trusted base from the reviewed copy

```sh
cd ~/src/triage-trusted/trusted
cargo install --locked --path host      # installs the `triage` command
triage init --repo ~/src/triage
triage build-gateway                    # builds the gateway image from this reviewed copy
```

When the trusted base changes upstream or the coding agent proposes a change to `trusted/`,
review the difference in the reviewed copy, then repeat this step. Nothing else ever rebuilds it.

### 3. Add your mail accounts

```sh
triage account add
```

Credentials are stored in your macOS Keychain and handed to the gateway when it starts. They are
never visible inside the email sandbox or the coding sandbox. For Gmail, use an app password for
now.

### 4. Start the local model

Download a model in GGUF format that supports tool calls, choose an API key (any long random
string), and run:

```sh
triage secret llm-api-key               # paste the key
triage init --repo ~/src/triage --llm-port 8012
TRIAGE_LLM_API_KEY=<the key> ~/src/triage-trusted/trusted/llm/run-llm.sh ~/models/<model>.gguf 8012
```

The script runs `llama-server` in a macOS sandbox without network access. Which model works best
depends on your machine's memory; a model of around 20 billion parameters is a reasonable start
with 16–24 GB.

### 5. Start everything

```sh
triage up
```

This builds the coding sandbox and the app, starts the containers, and prints the address of the
web interface. Keep it running: it is where you approve (or refuse) every message that the app
wants to send to the coding agent, after reading its exact text. `triage coding-login` logs the
coding agent in to your Claude account the first time; alternatively store an API key with
`triage secret coding-api-key`.

Your data lives in `~/.triage`: `cache/` (local copy of your mail and its index), `config/`
(settings and your policy), `gateway/` (the log of every change made to your mailboxes).

Development
-----------

The coding sandbox image (`coding/Dockerfile`) has everything needed:

```sh
cd trusted && cargo test && cargo clippy --all-targets && cargo fmt --check
cd trusted && cargo build -p triage-gateway
cd app && TRIAGE_GATEWAY_BIN=../trusted/target/debug/triage-gateway uv run pytest
cd app && uv run ruff check && uv run ruff format --check && uv run mypy
```

* The gateway's integration tests start a throwaway Dovecot server and need the `dovecot` binary
  (Debian package `dovecot-imapd`).
* With `TRIAGE_GATEWAY_BIN` set, the app's tests include end-to-end tests against the real gateway
  and Dovecot. Without it they are skipped.
* The interface tests drive Chromium (`/usr/bin/chromium`) and are skipped if it is missing.

All test data is synthetic; never add real email to this repository.
