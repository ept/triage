Email Triage app
================

"Email Triage" is an AI app that helps people with overflowing inboxes to quickly work through
routine messages, helping to surface important messages requiring attention. It is designed around
a security-first mindset, while allowing the user to easily customise its functionality in order to
meet their particular needs.

Security requirements and architecture
--------------------------------------

The words "MUST", "MUST NOT", etc. are used as in RFC 2119.

* Email contents, personal data (e.g. names and email addresses), and credentials MUST NOT leave
  the user's local machine and the IMAP server (configured by the user) from which it was fetched.
  When email data is exposed to an LLM, it MUST be an open-weights model running locally on the
  user's machine, NOT a remote model.
* All email processing (IMAP client, parsing, extracting text from attachments, etc.) MUST be
  performed inside a sandbox called the *email sandbox*. To avoid the
  [lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/),
  since email data is both untrusted and sensitive, this sandbox MUST block all network access
  except to the user's IMAP servers.
* The email sandbox MUST NOT allow access to the user's file system and home directory except for
  a directory containing a local cache of the user's emails, a directory with the app's
  configuration, and any other directory explicitly to which the user has explicitly granted access.
* The app MAY run arbitrary programs within the email sandbox, including scripts written by the
  agent. However, it MUST NOT be able to install software downloaded from the internet, due to the
  restriction on network access.
* The email app MAY modify its own code in order to adapt to the user's workflow. However, within
  the email sandbox the code MUST NOT be modifiable. Instead, all modifications to the app MUST be
  performed within a separate sandbox called the *coding sandbox*.
* The coding sandbox has full access to the internet, and arbitrary software may be installed and
  run within it. However, it MUST NOT be able to access any email content, any data extracted from
  emails, or any credentials. It MUST NOT have access to the user's file system or home directory
  except for the directory containing the code of the app itself (the directory containing this
  specification file). The coding sandbox SHOULD run a coding agent such as Claude Code or another
  agent harness, and it MAY connect to a frontier model such as Claude as a remote service.
* The email sandbox and the coding sandbox MUST enforce network and file system access control as
  far as we know (barring zero-day vulnerabilities). Docker containers with appropriate
  configuration are considered sufficiently secure.
* The LLM on the local user's machine, through which email content is processed, MAY run outside of
  the sandboxes if this is required in order to use the GPU on the user's machine and other hardware
  resources. However, any tool calls made by this LLM MUST be executed inside the email sandbox. The
  local LLM MUST NOT run any processes outside of the email sandbox, MUST NOT make any network
  connections to remote hosts, and MUST NOT open any files outside of the email sandbox except those
  that are intrinsically required for the operation of the LLM itself.
* The user interface for the app MUST be a web app running inside the email sandbox. It SHOULD be
  presented through a simple wrapper app on the user's operating system (e.g. using Tauri). This
  wrapper app SHOULD also manage the two sandboxes and the local LLM, but it SHOULD minimise what
  is done outside of the sandboxes, and do as much as possible within the appropriate sandbox.
* If the user interface web app or the local LLM wishes to request a modification to the app's
  functionality, it MAY make a request to the coding agent inside the coding sandbox. This coding
  agent MAY autonomously implement the requested feature and reload the app to use the updated
  code without requiring review from the user. However, the coding agent MUST allow only changes
  to the app that ensure the security requirements in this document continue to be met.
* The app MUST NOT be able to send email autonomously, and it MUST NOT have SMTP access. Instead,
  when the user requests that the AI should write an email, the app should place that message in
  the user's drafts folder via IMAP, so that the user can open, review, and send it.

The rationale between this architecture is a defence-in-depth design:

* If a maliciously crafted email succeeds at prompt injection against the local LLM, the blast
  radius is limited by the restrictions on code running inside the email sandbox. Although the
  prompt injection could result in a request to the coding agent that was not made by the user
  themselves, it seems unlikely that the malicious email could "chain exploits" by first causing
  prompt injection on the local LLM, and then persuading the coding agent to change the app in an
  unsafe way.
* If a maliciously crafted email manages to trigger, say, a buffer overflow in a memory-unsafe
  parser, the adversary would additionally have to find a sandbox escape before they could do much
  damage. Remote code execution limited to the email sandbox would allow the adversary to corrupt
  data on the IMAP server (e.g. delete lots of emails), but not much more than that. This risk can
  be mitigated with email backups, which users ought to have anyway.

Engineering practices

* The app should be implemented according to best engineering practices, including good modularity,
  thorough test coverage, and effective use of existing libraries.
* The implementation language is not specified and can be chosen by the coding agent. It might make
  sense to choose a language for which good libraries (IMAP client, email parsing, character set
  conversion, etc.) are available. The choice of local LLM to use is also left to the coding agent.
* The implementation of the app will mostly take place inside a coding sandbox, but some things
  inevitably have to be done outside of the sandbox (e.g. running the wrapper app, perhaps setting
  up the local LLM). For these things the coding agent should provide instructions for the steps
  that the user needs to do manually.
* When the coding agent modifies the app's code, it MUST commit it to the Git repository (the same
  repository that also contains this specification file) so that the changes to the app are tracked.
* The coding agent MAY install arbitrary software packages within either of the sandboxes, though it
  SHOULD prefer popular, well-maintained dependencies from reputable package repositories over
  random unknown projects. If Docker is used for the sandboxes, installation of dependencies SHOULD
  happen during image build and specified in a `Dockerfile`.
* Data within a sandbox that should survive container restarts (e.g. configuration, local copy of
  downloaded emails) should be stored in a directory under `~/.triage` and mounted into the
  container.
* The coding agent SHOULD watch for new releases of any packages that the app depends upon, and
  automatically update dependencies so that any fixes to security vulnerabilities are quickly
  incorporated without awaiting review by the user. It SHOULD automatically reload the app if this
  can be done without losing the user interface state, or prompt the user to restart if not.
* The coding agent running inside a sandbox MUST NOT have the permission to build and run Docker
  images outside of the container it is running in. Instead, the coding agent SHOULD update the
  Dockerfile and then request to the wrapper app that it should build the new image and launch the
  container.
* If a bug is found, e.g. a crash occurs while parsing an email, the local LLM and its agent harness
  in the email sandbox SHOULD diagnose the issue and then send a bug report to the coding agent. The
  bug report MUST NOT contain any genuine email data, but it MAY contain synthetic test cases that
  trigger the same bug. The coding agent SHOULD try to fix the bug and update the app with the fix.
* Any code, data, or commit messages in the app code repository MUST NOT contain data from real
  emails or personal data, since the app is open source.


Functionality
-------------

This section is preliminary and will grow as use cases for the app are refined.

* For now, let's make the app only for macOS, while keeping open the option of more OSes in the
  future. Use Docker for the sandboxes, so it's Linux inside the sandboxes.
* Let's start with a typical chatbot user interface. The user should be able to use this interface
  to ask questions about their emails (e.g., what are all the time-critical things I need to respond
  to this week?), which are answered by the local model. The user can use the same interface to also
  request changes to the app itself, which are handled by the agent in the coding sandbox. Chat
  input goes to the local model first, which forwards it to the coding agent if it concerns changes
  to functionality.
* For email access, the app should support multiple IMAP accounts. The user should be able to
  specify the IMAP configuration and credentials in the app preferences. Within the email sandbox
  an IMAP client should download a complete local copy of each account's inbox, and the last few
  months worth of messages from other IMAP folders. This may take a while on first load, so the app
  should report progress.
* The email agent is free to query the IMAP server for searches as needed, it is allowed to move
  messages from one folder to another (including trash), and it is allowed to update message
  properties such as unread status. It is also allowed to create new messages in the drafts folder,
  but not in other folders. If the email agent deletes messages at a high rate (say, more than 20
  messages per hour), the app should warn the user.
* For the coding model, let's start by using Claude with subscription login and a choice of model
  that is configurable in the app preferences. I don't mind which agent harness is used as long as
  it's good at writing and maintaining software.
* As part of its configuration the app stores a *policy*, which is a prose document describing
  general rules for what the user wants to do automatically with their email. For example: "When
  somebody asks me about X, please draft an answer saying Y, and place it in my drafts folder. When
  I have either sent or deleted the draft, move the original incoming email to the archive folder."
  The policy is private and it MUST NOT be added to the app's code repository (not even excerpts). 
  The user may edit the policy text within the app, and the local LLM may also update it based on
  chatbot input when the user requests a certain behaviour to be carried out automatically.
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
