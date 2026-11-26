Email Triage app
================

"Email Triage" is an AI app that helps people with overflowing inboxes to quickly work through
routine messages, helping to surface important messages requiring attention. It is designed around
a security-first mindset, while allowing the user to easily customise its functionality in order to
meet their particular needs.

Security requirements and architecture
--------------------------------------

The words "MUST", "MUST NOT", etc. are used as in RFC 2119.

### Overview

The app consists of four parts, which are trusted to different degrees:

* The *email sandbox* holds the user's email data and runs the app, including any tool calls made
  by the local LLM. It has no network access and cannot modify its own code. Everything inside it
  is assumed to be potentially hostile: email content is untrusted, and the app code is written by
  an agent without human review.
* The *coding sandbox* runs the coding agent that modifies the app. It has internet access, but no
  access to email data.
* The *local LLM* is the only model that sees email data. It may run outside of the sandboxes in
  order to use the GPU, but it is confined so that it can do nothing except inference.
* The *trusted base* is the small amount of fixed, human-reviewed code that creates the sandboxes
  and controls every channel between them and the outside world: the wrapper app, the gateway, and
  the sandbox configuration. The security requirements in this document are enforced by the trusted
  base, and they MUST continue to hold regardless of what the code and the models inside the two
  sandboxes do.

General requirements:

* Email contents, personal data (e.g. names and email addresses), and credentials MUST NOT leave
  the user's local machine and the IMAP server (configured by the user) from which it was fetched.
  When email data is exposed to an LLM, it MUST be an open-weights model running locally on the
  user's machine, NOT a remote model.
* The email sandbox and the coding sandbox MUST enforce network and file system access control as
  far as we know (barring zero-day vulnerabilities). Docker containers with appropriate
  configuration are considered sufficiently secure. The containers SHOULD run as a non-root user
  with all capabilities dropped and a read-only root file system, and they MUST NOT have access to
  the Docker socket.

### Email sandbox

* All processing of email content (parsing, character set conversion, extracting text from
  attachments, indexing, rendering, etc.) MUST be performed inside a sandbox called the *email
  sandbox*. To avoid the
  [lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/),
  since email data is both untrusted and sensitive, this sandbox MUST NOT have any network access:
  no route to the internet, the local network, or the host, and no resolution of external DNS
  names. Its only connection is to the gateway (see below), which provides access to the user's
  mailboxes, to the local LLM, and to the user interface.
* The email sandbox MUST NOT allow access to the user's file system and home directory except for
  a directory containing a local cache of the user's emails, a directory with the app's
  configuration, and any other directory to which the user has explicitly granted access. Since
  the mounted directories are part of the sandbox configuration, such a grant MUST be made through
  the wrapper app (e.g. a native file picker), not through the app's web interface. IMAP
  credentials MUST NOT be stored in any directory that is visible inside the email sandbox.
* The app MAY run arbitrary programs within the email sandbox, including scripts written by the
  agent. However, it MUST NOT be able to install software downloaded from the internet, due to the
  restriction on network access. Scripts written by the local LLM SHOULD run as a separate
  unprivileged user that cannot write to the app's configuration or the policy.
* The email app MAY modify its own code in order to adapt to the user's workflow. However, within
  the email sandbox the code MUST NOT be modifiable. Instead, all modifications to the app MUST be
  performed within a separate sandbox called the *coding sandbox*.

### Gateway

Allowing the email sandbox to connect to the user's IMAP servers directly would not be sufficient:
on a shared mail provider an adversary can have their own account on the same server, so code in
the sandbox could log in there and upload the user's mail. Moreover, since the agent may run
arbitrary scripts in the sandbox, any rule enforced only by app code (e.g. "drafts only") could be
bypassed by a script that speaks IMAP itself. Therefore:

* The email sandbox MUST NOT connect to IMAP servers directly and MUST NOT hold IMAP credentials.
  All mailbox access MUST go through the *gateway*, a component of the trusted base that runs
  outside of the email sandbox (e.g. in its own container) and is the email sandbox's only network
  peer.
* The gateway holds the credentials, which SHOULD be stored in the macOS Keychain, and performs the
  login to the IMAP server, including obtaining and refreshing OAuth 2 tokens for providers that
  require them. The gateway MUST connect only to the IMAP servers that the user has configured and
  to the OAuth endpoints of those providers. As defence in depth, its outbound network access
  SHOULD additionally be restricted to those hosts by a firewall.
* IMAP accounts (server names, credentials, OAuth consent) MUST be configured through a user
  interface provided by the trusted base, not through the app's web interface, so that code in the
  email sandbox can neither read credentials nor add a server of the adversary's choosing.
* The gateway MUST permit only the following mailbox operations: listing folders, searching,
  fetching messages, changing message flags (such as unread status), creating folders, moving
  messages between the user's own folders (including to the trash), and creating a draft as
  described below. It MUST reject everything else, in particular:
    - permanent deletion of messages (expunging, except where the gateway itself does so as a step
      of moving a message on servers that lack a move command);
    - deleting or renaming folders;
    - reading or changing access control lists, and any access to shared folders or other users'
      mailboxes;
    - appending, copying, or moving a message into the drafts folder, the sent folder, an outbox,
      or any other folder from which a message may be sent or mistaken for something the user
      wrote, other than through the draft operation.
* The app MUST NOT be able to send email autonomously, and it MUST NOT have SMTP access. Instead,
  when the user requests that the AI should write an email, the app should place that message in
  the user's drafts folder via the gateway, so that the user can open, review, and send it.
* A draft is submitted to the gateway as structured fields (recipients, subject, the message being
  replied to, body text), from which the gateway constructs the message. Drafts MUST be plain text
  without attachments (an HTML draft could contain a remote image that leaks data as soon as the
  user opens the draft in their mail client), and MUST NOT contain Bcc or Reply-To headers. If the
  draft is a reply, its recipients MUST be a subset of the sender and recipients of the message
  being replied to. A draft that is not a reply MUST only be created in direct response to a
  request by the user.
* The gateway MUST record every operation that modifies a mailbox in an append-only log, which the
  email sandbox can read but not modify. The app SHOULD use this log to show the user what was done
  automatically, and to offer undoing it. If messages are moved to the trash at a high rate (say,
  more than 20 messages per hour), the trusted base MUST warn the user.
* The gateway also relays the email sandbox's connections to the local LLM and the wrapper app's
  connections to the user interface, since the email sandbox has no other network peer.
* The gateway MUST be written in a memory-safe language and it MUST treat message contents as
  opaque bytes, leaving all parsing of messages to the email sandbox. It MAY be implemented either
  as a filtering IMAP proxy or as a service with a narrow, purpose-built API; the latter is
  preferred if it results in a smaller attack surface.

### Coding sandbox

* The coding sandbox has full access to the internet, and arbitrary software may be installed and
  run within it. However, it MUST NOT be able to access any email content, any data extracted from
  emails, the policy, or any credentials other than the credential for the coding model. It MUST
  NOT have access to the user's file system or home directory except for the directory containing
  the code of the app itself (the directory containing this specification file). The coding sandbox
  SHOULD run a coding agent such as Claude Code or another agent harness, and it MAY connect to a
  frontier model such as Claude as a remote service.
* The credential for the coding model is the only secret in the coding sandbox. Since the coding
  agent reads untrusted content from the internet, this credential should be regarded as exposed to
  that content, and it SHOULD therefore be limited in scope and easy to revoke.

### Trusted base

* The trusted base consists of the wrapper app, the gateway, the parameters with which the
  sandboxes are created (mounts, networks, privileges, image build options), and the confinement
  of the local LLM. It SHOULD be as small as possible: whatever can be done inside one of the
  sandboxes SHOULD be done there.
* The source code of the trusted base lives in a designated directory of the app's repository. The
  coding agent writes it initially and MAY propose changes to it, but changes to the trusted base
  MUST NOT take effect automatically: they MUST be reviewed by the user, who then builds and
  installs the new version manually. The wrapper app MUST NOT build, update, or reconfigure any
  part of the trusted base at the request of either sandbox.
* The parameters with which the sandboxes are created MUST be fixed within the trusted base. The
  wrapper app MUST NOT take mounts, network settings, privileges, or build options from files that
  the coding agent can modify (so no agent-written Compose files or launch scripts). The coding
  agent controls only what is inside an image, via the `Dockerfile` and the app code.
* The working tree of the app's repository, including its `.git` directory, is writable by the
  coding agent. The wrapper app and the user therefore MUST NOT run Git commands or build tools on
  the host directly in that working tree, since Git hooks, Git configuration, and build scripts can
  execute code. The trusted base MUST be built from a fresh clone at the reviewed commit.

### Channels between the parts

* *Code (coding sandbox to email sandbox).* If the user requests a modification to the app's
  functionality, the coding agent MAY autonomously implement the requested feature and reload the
  app to use the updated code without requiring review from the user. The coding agent MUST make
  only changes to the app that are consistent with the security requirements in this document, but
  since the coding agent reads untrusted content from the internet, the requirements MUST NOT
  depend on it doing so: they are enforced by the trusted base.
* *Change requests (email sandbox to coding sandbox).* This is the only channel through which data
  can move from the email sandbox to a place with internet access, and whatever is sent through it
  is also sent to a remote model. A message to the coding agent (a feature request, a bug report,
  or a reply to a question from the coding agent) MUST be plain text of limited length. It MUST be
  displayed in full to the user by the trusted base (e.g. in a native dialog, not in the app's web
  interface, which may be compromised), and it MUST be forwarded only once the user has approved
  it. The local LLM MAY decide that a chat message should be routed to the coding agent, and it MAY
  write a bug report, but neither is sent without this approval. The wrapper app MAY additionally
  provide a means for the user to talk to the coding agent directly, without passing through the
  email sandbox; this is also how the app can be repaired if its user interface is broken.
* *Coding agent output (coding sandbox to email sandbox).* The interaction with the coding agent
  is displayed in the app's user interface. It MUST be treated as untrusted text, and it SHOULD NOT
  be placed into the context of the local LLM.
* *User interface.* The user interface for the app MUST be a web app running inside the email
  sandbox. It SHOULD be presented through a simple wrapper app on the user's operating system (e.g.
  using Tauri). This wrapper app SHOULD also manage the two sandboxes and the local LLM. Although
  the web app is served from the email sandbox, the web view that renders it runs on the host and
  would by default have network access, so it MUST be treated as part of the email sandbox: the
  wrapper app MUST prevent the web view from loading any resource from, navigating to, or opening
  a window on anything other than the app's own origin. This MUST be enforced by the wrapper app
  itself, not by headers or markup supplied by the app. The wrapper app MUST open links in the
  system browser only in response to a click by the user, and SHOULD show the destination first.
  The interface between the web app and the wrapper app MUST be limited to the few functions that
  are needed (e.g. submitting a change request, opening a link).
* *Email and model output in the user interface.* HTML emails MUST be rendered in an isolated frame
  in which scripts cannot run, after sanitisation. Remote images and other remote content MUST NOT
  be loaded, neither from emails nor from text generated by a model (e.g. a Markdown image whose
  URL encodes private data).

### Local LLM

* The LLM on the local user's machine, through which email content is processed, MAY run outside of
  the sandboxes if this is required in order to use the GPU on the user's machine and other hardware
  resources. However, any tool calls made by this LLM MUST be executed inside the email sandbox. The
  local LLM MUST NOT run any processes outside of the email sandbox, MUST NOT make any network
  connections to remote hosts, and MUST NOT open any files outside of the email sandbox except those
  that are intrinsically required for the operation of the LLM itself.
* These restrictions SHOULD be enforced by the operating system (e.g. a macOS sandbox profile that
  denies network access and limits file access) rather than relying on the inference server being
  well-behaved. The inference server MUST listen only on the loopback interface and SHOULD require
  an authentication token, so that other local processes and web pages cannot use it. It MUST NOT
  log prompts or completions outside of the email sandbox.
* Model weights are downloaded outside of the email sandbox, and their checksums MUST be verified.

### Rationale

The rationale behind this architecture is a defence-in-depth design:

* If a maliciously crafted email succeeds at prompt injection against the local LLM, the blast
  radius is limited by the restrictions on the email sandbox. The local LLM has no network access,
  it can act on the mailbox only through the operations that the gateway permits, and it cannot
  reach the coding agent on its own: a request to the coding agent is forwarded only after the user
  has read and approved its exact text. We therefore do not have to rely on it being difficult to
  "chain exploits" from the local LLM to the coding agent.
* If a maliciously crafted email manages to trigger, say, a buffer overflow in a memory-unsafe
  parser, the adversary would additionally have to find a sandbox escape before they could do much
  damage. Remote code execution limited to the email sandbox would allow the adversary to misfile
  messages, change flags, and create bogus plain-text drafts, but not to delete messages
  permanently, send them, or copy them elsewhere. Everything it does is recorded in the gateway's
  log and can be undone.
* If the coding agent is subverted (by prompt injection from a web page, or by a malicious
  dependency), it can put hostile code into the email sandbox. That code has access to the email
  data, but it is in the same position as the adversary in the previous point, since the sandbox
  configuration, the gateway, and the web view restrictions are outside of the coding agent's
  control.
* Some risks remain. An adversary who controls the email sandbox can still misfile or hide
  individual messages (e.g. mark a security alert as read), can try to hide a little data in text
  that the user approves or in a draft that the user sends, or in a link that the user clicks. The
  design limits these to low-bandwidth channels that pass in front of the user, but it relies on
  the user paying attention. The trusted base is trusted only because the user has reviewed it.

Engineering practices
---------------------

* The app should be implemented according to best engineering practices, including good modularity,
  thorough test coverage, and effective use of existing libraries.
* The implementation language is not specified and can be chosen by the coding agent. It might make
  sense to choose a language for which good libraries (IMAP client, email parsing, character set
  conversion, etc.) are available. The choice of local LLM to use is also left to the coding agent.
* The implementation of the app will mostly take place inside a coding sandbox, but some things
  inevitably have to be done outside of the sandbox (e.g. building and running the wrapper app and
  the gateway, perhaps setting up the local LLM). For these things the coding agent should provide
  instructions for the steps that the user needs to do manually.
* When the coding agent modifies the app's code, it MUST commit it to the Git repository (the same
  repository that also contains this specification file) so that the changes to the app are tracked.
* The coding agent MAY install arbitrary software packages within either of the sandboxes, though it
  SHOULD prefer popular, well-maintained dependencies from reputable package repositories over
  random unknown projects. If Docker is used for the sandboxes, installation of dependencies SHOULD
  happen during image build and specified in a `Dockerfile`. Dependency versions MUST be pinned
  with a lock file.
* Data within a sandbox that should survive container restarts (e.g. configuration, local copy of
  downloaded emails) should be stored in a directory under `~/.triage` and mounted into the
  container.
* The coding agent SHOULD watch for new releases of any packages that the app depends upon, and
  automatically update dependencies without awaiting review by the user. Since a freshly published
  release is also the most likely to be malicious (compromised maintainer accounts are usually
  detected within days), the coding agent SHOULD adopt a new release only once it has been public
  for a cooldown period (say, 7 days), except when the release fixes a published security
  vulnerability that affects the app, in which case it SHOULD be adopted promptly. The app SHOULD
  be reloaded automatically if this can be done without losing the user interface state, or the
  user prompted to restart if not.
* The coding agent running inside a sandbox MUST NOT have the permission to build and run Docker
  images outside of the container it is running in. Instead, the coding agent SHOULD update the
  Dockerfile and then request to the wrapper app that it should build the new image and launch the
  container. The wrapper app builds the image with fixed options, and the build MUST NOT have access
  to email data, the app's configuration, or credentials.
* Before a new version of the app replaces the running one, its tests MUST pass. The wrapper app
  SHOULD keep the previous image, check that the new version starts up correctly, and roll back
  automatically if it does not. Changes to the format of stored data SHOULD be reversible, or the
  data SHOULD be backed up first, so that a rollback remains possible.
* If a bug is found, e.g. a crash occurs while parsing an email, the local LLM and its agent harness
  in the email sandbox SHOULD diagnose the issue and then write a bug report for the coding agent.
  The bug report MUST NOT contain any genuine email data, but it MAY contain synthetic test cases
  that trigger the same bug. Since this rule cannot be enforced mechanically (the local LLM that
  writes the report may itself be the victim of prompt injection), a bug report is a change request
  like any other, and is sent only after the user has read and approved it. The coding agent SHOULD
  try to fix the bug and update the app with the fix.
* Any code, data, or commit messages in the app code repository MUST NOT contain data from real
  emails or personal data, since the app is open source.


Functionality
-------------

This section is preliminary and will grow as use cases for the app are refined.

* For now, let's make the app only for macOS, while keeping open the option of more OSes in the
  future. Use Docker for the sandboxes, so it's Linux inside the sandboxes. Docker has no built-in
  way of restricting a container to particular destination hosts, but none is needed: the email
  sandbox is attached only to an internal Docker network whose only other member is the gateway.
  The security requirements above are written in terms of what the sandbox must guarantee, so that
  a different runtime (e.g. Apple's `container`, which uses one lightweight VM per container) could
  be substituted later.
* Let's start with a typical chatbot user interface. The user should be able to use this interface
  to ask questions about their emails (e.g., what are all the time-critical things I need to respond
  to this week?), which are answered by the local model. The user can use the same interface to also
  request changes to the app itself, which are handled by the agent in the coding sandbox. Chat
  input goes to the local model first. If it concerns changes to functionality, the local model
  hands the message over to the coding agent as a change request, which the user confirms as
  described above. The user's own words SHOULD be forwarded unchanged rather than paraphrased by
  the local model, and the user interface SHOULD make clear at all times whether a message is going
  to the local model or to the remote one.
* For email access, the app should support multiple IMAP accounts. The user should be able to
  specify the IMAP configuration and credentials in the account settings provided by the wrapper
  app. Both password login and OAuth 2 (as required by Gmail and Microsoft 365) should be
  supported. Within the email sandbox, the app should download via the gateway a complete local
  copy of each account's inbox, and the last few months worth of messages from other IMAP folders.
  This may take a while on first load, so the app should report progress.
* A small local model is too slow to read a large mailbox at the time a question is asked. The app
  should therefore process messages in the background as they arrive, storing whatever it extracts
  (e.g. a summary, whether a reply is needed, deadlines, a search index) in the local cache, and
  answer questions from that.
* The email agent is free to query the IMAP server for searches as needed, it is allowed to move
  messages from one folder to another (including trash), and it is allowed to update message
  properties such as unread status. It is also allowed to create new messages in the drafts folder,
  but not in other folders. These limits, and the warning if messages are deleted at a high rate,
  are enforced by the gateway as described above.
* For the coding model, let's start by using Claude with subscription login and a choice of model
  that is configurable in the app preferences. An API key should be supported as an alternative.
  I don't mind which agent harness is used as long as it's good at writing and maintaining
  software.
* As part of its configuration the app stores a *policy*, which is a prose document describing
  general rules for what the user wants to do automatically with their email. For example: "When
  somebody asks me about X, please draft an answer saying Y, and place it in my drafts folder. When
  I have either sent or deleted the draft, move the original incoming email to the archive folder."
  The policy is private and it MUST NOT be added to the app's code repository (not even excerpts).
  The user may edit the policy text within the app, and the local LLM may also propose an update
  based on chatbot input when the user requests a certain behaviour to be carried out
  automatically. Since a malicious email could otherwise plant a lasting instruction in the policy,
  an update proposed by the local LLM MUST be shown to the user as a diff and takes effect only
  once the user has confirmed it.
* The policy is saved as part of the app configuration and is versioned with Git (with every update
  within the app triggering an automatic commit) so that the user can roll back unwanted policy
  changes if needed. This Git repository MUST be separate from the repository containing the app
  code, since the app code is open source but the policy is private.
* The user interface should have facilities for nicely rendering emails, so that they can be
  displayed inline when desired. This should include changing the weird charsets that the email
  might be using into UTF-8, showing just the most important header fields, rendering both
  plain-text and HTML emails in a way that matches how other email clients display them, hiding
  quoted text in top-posting emails (but allowing it to be shown if necessary).
* Remote images and other remote content in emails should not be displayed, to avoid this being used
  as a tracking mechanism.
* When the chatbot is used to request changes to the app code, the interaction with the coding agent
  should also be displayed within the app's user interface.

Open questions
--------------

* Since every installation modifies its own copy of the app, each user's copy gradually becomes a
  fork. How do improvements and security fixes made upstream (especially to the trusted base) reach
  an installation that has diverged, and do installations ever push their changes anywhere?
* OAuth 2 login requires a client registration with each provider. Google requires a security
  assessment for apps that request full mailbox access, so users may have to supply their own
  client ID, and organisations may have to approve the app for their tenant.
* Whether a Claude subscription login may be used inside this app depends on Anthropic's terms at
  the time, which have changed repeatedly; this needs checking before it is relied upon.
