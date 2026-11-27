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
| App: assistant, policy, analysis, web interface | Implemented; tested with a scripted stand-in for the model, and in a browser through the real gateway's relay. With a real model it has only been tried on made-up mail, with a small model (see "Trying a model" below) |
| Host tool `triage`           | Implemented; its logic is unit-tested and it builds on macOS, but `triage up` has **not yet been run against Docker** |
| Container images             | **Not yet built with Docker.** The build steps of all three Dockerfiles have been replayed inside the real base images without Docker (except the Rust installation in the coding sandbox image), and the resulting gateway and app start and talk to each other |
| LLM sandbox profile          | Written, **not yet tested on macOS**                                   |
| Wrapper app `email-triage`   | First version (see "The wrapper app" below). Its windows, the approval of a request and the confinement of the web view have been tried on Linux with stand-ins for Docker and the gateway. On macOS only its self-test has been run, and **failed** twice; the fix for the second failure has not been run there yet |
| OAuth login (Gmail, Microsoft 365) | Gateway side implemented, untested against real providers; no consent flow in the host tool yet |
| Scripts written by the local LLM, text from attachments, automatic dependency updates | Not started |

With `triage up` and a normal browser, two of the specification's requirements are not met: a
browser does not stop a page from navigating elsewhere, and the approval of change requests
happens in the terminal rather than in a window. The wrapper app is meant to meet both, once it
has passed its self-test on your machine.

Other known gaps: the policy is applied to each new message when it arrives, so rules that depend
on something happening later ("when I have sent the draft, archive the original") are not carried
out yet; server-side search only accepts ASCII; text that an email hides from human readers
is still visible to the model; and a drafted reply quotes the message it answers as plain text,
also when that message is in HTML (the specification requires drafts to be plain text).

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

The model runs directly on your Mac (containers cannot use the GPU), in a macOS sandbox that
gives it no network access. Four steps:

**Download a model** in GGUF format that supports tool calls, into a directory that contains
nothing else: the sandboxed model server can read that directory, and nothing else in your home
directory. On a Mac with 32 GB of memory, of which the model should use less than half, a
reasonable first choice is gpt-oss-20b (a 12 GB download, about 13 GB of memory while running):

```sh
mkdir -p ~/models
curl -L -C - -o ~/models/gpt-oss-20b-MXFP4.gguf \
    https://huggingface.co/ggml-org/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-MXFP4.gguf
```

(If the download is interrupted, the same command resumes it.) No model has been tried with
this app yet, so expect to experiment. A smaller alternative is
`gemma-4-12b-it-Q6_K.gguf` from `unsloth/gemma-4-12b-it-GGUF` (10 GB).

**Make up an API key.** It is a password that you invent, not something you get from a provider:
the gateway presents it to the model server, so that nothing else on your machine (the coding
sandbox, for instance) can use the model. Generate one and store it in the Keychain:

```sh
openssl rand -hex 32                    # prints a random key; copy it
triage secret llm-api-key               # paste it at the prompt
```

You do not need to keep the key anywhere else.

**Tell the host tool** which port the model server will use:

```sh
triage init --repo ~/src/triage --llm-port 8012
```

**Run the model server** in a terminal window of its own, and leave it running:

```sh
~/src/triage-trusted/trusted/llm/run-llm.sh ~/models/gpt-oss-20b-MXFP4.gguf 8012
```

The script reads the key from the Keychain (macOS may ask you to allow that) and runs
`llama-server` in the sandbox. It is ready when it prints that it is listening. The script and
its sandbox profile have not been tested on macOS yet; if the server fails to start, its output
says what the sandbox refused.

Docker Desktop has a memory limit of its own for the containers (Settings → Resources). About
8 GB should be enough for this app; lower the limit if it is set higher and memory is tight.

### 5. Start everything

```sh
triage up
```

This builds the coding sandbox and the app, starts the containers, and prints the address of the
web interface. Keep it running: it is where you approve (or refuse) every message that the app
wants to send to the coding agent, after reading its exact text. `triage coding-login` logs the
coding agent in to your Claude account the first time; alternatively store an API key with
`triage secret coding-api-key`.

`triage account add` and `triage account remove` take effect at once, also while everything is
running: the app starts downloading a new account's mail by itself, and drops its copy of a
removed account's mail. Other settings (`triage init`, `triage secret`, `triage grant`) take
effect when `triage up` next starts. Each start prints a new address: a browser window from
before the restart is no longer connected, and has to be closed or pointed at the new address.

Your data lives in `~/.triage`: `cache/` (local copy of your mail and its index), `config/`
(settings and your policy), `gateway/` (the log of every change made to your mailboxes).

The wrapper app
---------------

`email-triage` does what `triage up` does, in a window instead of a terminal and a browser:

* It shows the app's web interface in a window that can reach the app and nothing else. All
  traffic of that window goes through a small proxy inside the wrapper, which passes on requests
  for the app and refuses the rest. On macOS, where the web view does not use a proxy for
  addresses on the same machine, WebKit is additionally given a content rule list that blocks
  every address but the app's, and the app is only loaded once that list is in place. WebKit
  is also told not to open connections ahead of time (it otherwise connects to a link's address
  the moment the link is clicked, before anyone is asked), and to keep nothing on disk; the
  first of these uses a WebKit setting that Apple has not published, and the wrapper refuses to
  show the app if the setting is not there.
  Navigating elsewhere, opening windows, downloads, camera, microphone and the interfaces that
  do not go through proxies (WebRTC, WebTransport) are switched off, and pages have no channel
  to the wrapper. Links to other sites therefore do nothing yet.
* Before a message goes to the coding agent, a window of the wrapper's own shows its exact text
  and asks. Closing that window, or quitting, means no.
* Quitting (closing the main window, or Cmd-Q) stops and removes the containers.

It is part of the trusted base, so build it from the reviewed copy. It needs macOS 14 or later.
**Run the self-test first**: whether the confinement holds depends on how the system's web view
behaves. On Linux it passes. On macOS it has failed twice. First, requests for other ports of
the same machine got through; the content rule list stopped those. Then one thing remained: a
click on a link opened a connection to another port of the same machine, over which nothing was
sent. Switching off connections ahead of time is the fix for that, and it has **not yet been run
on macOS**.

```sh
cd ~/src/triage-trusted/trusted/wrapper
cargo run --release -- --self-test            # must end with PASSED
cargo run --release -- --self-test-control    # must end with "The control worked"
cargo install --locked --path .               # installs the `email-triage` command
email-triage                                  # instead of `triage up`; stop that first
```

The self-test shows a page that tries to reach a second server in every way a page has, in a web
view set up like the one for the app; the control does the same without the restrictions, to
show that the test can fail. The second server counts a connection as reached even if nothing
is sent over it. Both use no mail and no network beyond your machine, and their output is safe
to share. If the self-test does not pass, keep using `triage up`.

What the self-test cannot see is a name lookup (DNS): it would not notice if the web view
looked up the name of another machine without connecting to it.

The first start asks for access to the Keychain entries that `triage` created, because it is a
different program. Setting things up (`triage init`, `triage account add`, `triage secret`,
`triage build-gateway`) and `triage coding-login` still happen on the command line, and the
local model is still started by hand. If the coding sandbox image has to be built, that happens
during the first start without visible progress, and takes several minutes.

It is built on `wry` and `tao`, the web view and window libraries underneath Tauri, without the
Tauri framework: that is about a third of the dependencies, and no bridge between pages and
native code. Their versions are pinned exactly. `trusted/tools/dependency-age.py` lists locked
crates that were published less than seven days ago.

Trying a model
--------------

How well the assistant works depends on the model, and the tests cannot tell you: they use a
scripted stand-in. To see what your model does, run the assistant's evaluation against the
running model server. It uses made-up mail in an in-memory mailbox and touches nothing of yours,
so its output is safe to share when asking for help:

```sh
cd ~/src/triage-trusted/app             # the reviewed copy; needs uv (brew install uv)
TRIAGE_LLM_API_KEY=$(security find-generic-password -s email-triage -a llm-api-key -w) \
    uv run python -m triage.testing.evaluate --url http://127.0.0.1:8012
```

It gives the assistant a few tasks (answer a question, move one message, deal with every
request of a kind in a full inbox, analyse new messages), prints what the assistant did, and
checks the mailbox afterwards. Name one or more scenarios after the command to run only those.

How drafted replies look
------------------------

A reply that the assistant drafts has the new text on top and the message it answers quoted
below, laid out the way Apple Mail lays out a plain-text reply:

```
Thank you, Thursday works for me.

> On 19 Nov 2026, at 12:00, Kim Placeholder <kim@example.net> wrote:
> 
> Are you free for lunch on Thursday?
```

How the date in that line is written depends on the settings of your Mac, which the email
sandbox cannot see. The app therefore copies the form used by the newest replies in your sent
mail, and uses the time zone that your browser reports.

This layout has been compared with several hundred replies written by Mail 16 on a Mac (set to
British English with a 24-hour clock), and matches them in the form of the line above the
quotation, the quote marks and the empty lines. It has not been compared with other versions or
settings. To compare it with your own sent mail without showing that mail to anybody, run this
while `triage up` is running:

```sh
docker exec triage-app /opt/venv/bin/python -m triage.reply_report
```

It reads the local copy of your sent mail inside the email sandbox and prints a description of
structure only: counts, names of MIME types, header fields and HTML elements, and the shape of
the line above a quotation, in which every word is replaced by `Xx`, every digit by `9` and
every address by `ADDR`. It prints no text, name, address, date, subject or folder name. Look
through it before you pass it on.

Development
-----------

The coding sandbox image (`coding/Dockerfile`) has everything needed:

```sh
cd trusted && cargo test && cargo clippy --all-targets && cargo fmt --check
cd trusted && cargo build -p triage-gateway
cd trusted/wrapper && cargo test && cargo clippy --all-targets   # needs libwebkit2gtk-4.1-dev
cd trusted/wrapper && xvfb-run -a cargo run -- --self-test       # and xvfb, on Linux
cd app && TRIAGE_GATEWAY_BIN=../trusted/target/debug/triage-gateway uv run pytest
cd app && uv run ruff check && uv run ruff format --check && uv run mypy
```

* The gateway's integration tests start a throwaway Dovecot server and need the `dovecot` binary
  (Debian package `dovecot-imapd`).
* With `TRIAGE_GATEWAY_BIN` set, the app's tests include end-to-end tests against the real gateway
  and Dovecot. Without it they are skipped.
* The interface tests drive Chromium (`/usr/bin/chromium`) and are skipped if it is missing.

All test data is synthetic; never add real email to this repository.
