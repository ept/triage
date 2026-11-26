Gateway API
===========

The gateway (`trusted/gateway`) is the email sandbox's only network peer. This document is the
contract between the gateway and the app running in the email sandbox (`app/`). The gateway is part
of the trusted base, so changes to this contract need review by the user (see `SPEC.md`).

The gateway has two listeners:

* the **sandbox API**, reachable only from the email sandbox, described in the first part of this
  document;
* the **host listener**, published on the host's loopback interface and used only by the wrapper
  app. It carries the control API and the relay to the app's web interface.

Conventions
-----------

* Base URL of the sandbox API: the environment variable `TRIAGE_GATEWAY_URL` inside the email
  sandbox (e.g. `http://gateway:8100`). There is no authentication on this listener: it is
  protected by network isolation.
* Requests and responses are JSON (`application/json`, UTF-8) unless stated otherwise. Operations
  that take parameters are `POST` requests with a JSON object as body, even when they only read.
* Folder names are Unicode strings (the gateway converts from and to IMAP's modified UTF-7). A
  folder can only be addressed if the gateway has seen it in the folder list of the account, and
  it is inside the user's personal namespace.
* UIDs are IMAP UIDs, unique within a folder for a given `uid_validity`. Every operation that
  changes a mailbox requires the caller to pass the `uid_validity` it believes to be current. If it
  does not match, the operation fails with `uid_validity_changed`, and the caller must resync the
  folder.
* Timestamps are RFC 3339 strings. Dates are `YYYY-MM-DD`.

Errors have an HTTP status and the body `{"error": {"code": "...", "message": "..."}}`:

| Status | `code`                 | Meaning                                                          |
|--------|------------------------|------------------------------------------------------------------|
| 400    | `bad_request`          | Malformed request or a value out of range                        |
| 403    | `forbidden`            | The operation is not permitted by the gateway's rules            |
| 404    | `not_found`            | Unknown account, folder, message, or change request              |
| 409    | `uid_validity_changed` | The folder's `uid_validity` differs from the one passed          |
| 501    | `unsupported`          | The IMAP server lacks a capability that the operation needs      |
| 502    | `imap_error`           | The IMAP server rejected the command                             |
| 503    | `unavailable`          | The IMAP server or the local LLM cannot be reached, or login failed |

Accounts and folders
--------------------

### `GET /v1/health`

`{"status": "ok", "version": "0.1.0"}`

### `GET /v1/accounts`

```json
{"accounts": [{"id": "work", "address": "user@example.org", "display_name": "A. User"}]}
```

Accounts are configured by the user in the wrapper app. The `id` is a short stable identifier
(`[a-z0-9-]{1,32}`).

### `POST /v1/folders/list`

Request `{"account": "work"}`. Response:

```json
{"folders": [
  {"name": "INBOX", "delimiter": "/", "role": "inbox", "selectable": true, "protected": false},
  {"name": "Drafts", "delimiter": "/", "role": "drafts", "selectable": true, "protected": true}
]}
```

`role` is one of `inbox`, `drafts`, `sent`, `trash`, `junk`, `archive`, `all`, `flagged`, or
`null`. Messages cannot be moved into a `protected` folder (drafts, sent, outbox, templates, and
the like).

### `POST /v1/folders/create`

Request `{"account": "work", "name": "Triage/Newsletters"}`. Response `{}`. The name must not
collide with a protected folder name. Folders cannot be deleted or renamed.

### `POST /v1/folders/status`

Request `{"account": "work", "folder": "INBOX"}`. Response:

```json
{"uid_validity": 1700000000, "uid_next": 4321, "messages": 1234, "unseen": 17}
```

Reading messages
----------------

None of the reading operations change any flags (the gateway opens folders read-only and fetches
with `BODY.PEEK`).

### `POST /v1/messages/list`

Lists message metadata in ascending UID order. Request:

```json
{"account": "work", "folder": "INBOX", "uid_min": 1, "uid_max": null, "limit": 2000}
```

`uid_min` defaults to 1, `uid_max` to no upper bound, `limit` to 2000 (which is also the maximum).
Response:

```json
{"uid_validity": 1700000000,
 "messages": [{"uid": 17, "flags": ["\\Seen"], "size": 5120,
               "internal_date": "2026-11-01T12:00:00+00:00"}],
 "next_uid": 2018}
```

`next_uid` is `null` when there are no more messages in the range; otherwise pass it as `uid_min`
to continue.

### `POST /v1/messages/fetch`

Fetches the raw content of up to 100 messages. Request:

```json
{"account": "work", "folder": "INBOX", "uids": [17, 18]}
```

The response has the content type `application/vnd.triage.messages` and is a stream of frames. Each
frame is one line of JSON terminated by `\n`. A message frame is followed by exactly `size` bytes
of raw RFC 5322 message and then a single `\n`:

```
{"uid":17,"flags":["\\Seen"],"size":5120,"internal_date":"2026-11-01T12:00:00+00:00"}\n
<5120 bytes>\n
{"end":true,"uid_validity":1700000000}\n
```

UIDs that do not exist are silently omitted. Frames may arrive in any order. If the stream ends
without an `end` frame, the response was cut short and the caller should retry. If something fails
after the response has started, the last frame is `{"error": {"code": ..., "message": ...}}`.

### `POST /v1/messages/search`

Server-side search (the app should normally search its local cache instead). Request:

```json
{"account": "work", "folder": "INBOX",
 "query": {"and": [{"since": "2026-08-01"}, {"from": "alice"}, {"seen": false}]}}
```

Response `{"uid_validity": 1700000000, "uids": [17, 18]}`, ascending.

A query is one of:

| Query                                   | Matches                                             |
|-----------------------------------------|-----------------------------------------------------|
| `{"all": true}`                         | every message                                       |
| `{"and": [q, ...]}`, `{"or": [q, ...]}` | all / any of the subqueries (1 to 20 of them)       |
| `{"not": q}`                            | messages not matching `q`                           |
| `{"since": date}`, `{"before": date}`   | by date of arrival (internal date)                  |
| `{"sent_since": date}`, `{"sent_before": date}` | by `Date` header                            |
| `{"from": s}`, `{"to": s}`, `{"cc": s}`, `{"subject": s}` | substring of that header          |
| `{"body": s}`, `{"text": s}`            | substring of the body / of headers or body          |
| `{"header": ["Name", s]}`               | substring of the named header                       |
| `{"seen": b}`, `{"flagged": b}`, `{"answered": b}`, `{"draft": b}` | flag set or not set      |
| `{"keyword": k}`                        | messages with that keyword                          |
| `{"larger": n}`, `{"smaller": n}`       | size in bytes                                       |
| `{"uid": [lo, hi]}`                     | UID range, inclusive                                |

Search strings are at most 200 characters. For now they must be printable ASCII; a non-ASCII
string fails with `unsupported`. Queries may nest at most 5 levels deep.

Changing mailboxes
------------------

Every operation in this section is recorded in the operation log.

### `POST /v1/messages/flags`

```json
{"account": "work", "folder": "INBOX", "uid_validity": 1700000000, "uids": [17, 18],
 "add": ["\\Seen"], "remove": ["$Triage-Pending"]}
```

Response `{}`. The permitted flags are `\Seen`, `\Flagged`, `\Answered`, and keywords matching
`[A-Za-z$][A-Za-z0-9$_.-]{0,63}`. `\Deleted` and `\Draft` are forbidden. At most 500 UIDs per
request.

### `POST /v1/messages/move`

```json
{"account": "work", "folder": "INBOX", "uid_validity": 1700000000, "uids": [17, 18],
 "destination": "Archive"}
```

Response:

```json
{"destination_uid_validity": 1700000001,
 "moved": [{"uid": 17, "new_uid": 301}, {"uid": 18, "new_uid": 302}]}
```

`new_uid` and `destination_uid_validity` are `null` if the server does not report them. The
destination must not be a protected folder. Moving to the trash or junk folder counts as a
deletion for the purpose of the deletion rate warning. Messages can never be deleted permanently.
At most 500 UIDs per request.

### `POST /v1/drafts`

Creates a plain-text draft in the account's drafts folder. Request:

```json
{"account": "work",
 "to": [{"name": "Alice Example", "address": "alice@example.org"}],
 "cc": [],
 "subject": "Re: Lunch",
 "body": "Hi Alice,\n\n...",
 "in_reply_to": {"folder": "INBOX", "uid_validity": 1700000000, "uid": 17}}
```

Response `{"folder": "Drafts", "message_id": "<...@example.org>"}`.

* The gateway sets `From` (the account's address), `Date`, `Message-ID`, and, for replies,
  `In-Reply-To` and `References`. No other headers can be set; in particular there is no Bcc or
  Reply-To.
* `in_reply_to` is optional. If it is present, every recipient must be among the addresses in the
  From, Sender, Reply-To, To, and Cc headers of the message being replied to; otherwise the request
  fails with `forbidden`.
* `name` is optional. `subject` is at most 500 characters, `body` at most 100,000 characters, and
  there are at most 50 recipients, at least one of them in `to`.

### `GET /v1/log?after=<seq>&limit=<n>`

The operation log: every change made to a mailbox through the gateway, oldest first. `after`
defaults to 0, `limit` to 500 (maximum 1000).

```json
{"entries": [
  {"seq": 1, "time": "2026-11-27T10:00:00Z", "account": "work", "op": "move",
   "folder": "INBOX", "uid_validity": 1700000000, "destination": "Archive",
   "destination_uid_validity": 1700000001,
   "moved": [{"uid": 17, "new_uid": 301}]},
  {"seq": 2, "time": "...", "account": "work", "op": "flags", "folder": "INBOX",
   "uid_validity": 1700000000, "uids": [18], "add": ["\\Seen"], "remove": []},
  {"seq": 3, "time": "...", "account": "work", "op": "create_folder", "folder": "Triage"},
  {"seq": 4, "time": "...", "account": "work", "op": "draft", "folder": "Drafts",
   "message_id": "<...>", "in_reply_to": {"folder": "INBOX", "uid_validity": 1700000000, "uid": 17}}
 ],
 "last_seq": 4}
```

The log contains no message content. It can only be appended to by the gateway.

Change requests and the coding agent
------------------------------------

### `POST /v1/change-requests`

Submits a message for the coding agent. It is **not** sent anywhere until the user has read and
approved it in a dialog shown by the wrapper app.

```json
{"kind": "feature", "text": "Please add a button that ..."}
```

`kind` is `feature`, `bug`, or `reply` (an answer to a question from the coding agent). `text` is
plain text of at most 4,000 characters without control characters other than newline and tab. At
most 5 requests can be pending at a time. Response (status 202):

```json
{"id": "cr-7f3a...", "state": "pending"}
```

### `GET /v1/change-requests/<id>`

`{"id": "...", "kind": "feature", "state": "pending"}` where `state` is `pending`, `approved`,
`rejected`, or `expired`.

### `GET /v1/coding/events?after=<seq>&wait=<seconds>`

Output of the coding agent, to be displayed in the user interface. This text comes from a sandbox
with internet access: treat it as untrusted, never render remote content from it, and do not put
it into the local LLM's context. `wait` (at most 60) makes the request block until an event with a
higher sequence number is available.

```json
{"events": [{"seq": 12, "time": "...", "request_id": "cr-7f3a...", "kind": "text",
             "text": "I have added the button ..."}],
 "last_seq": 12}
```

`kind` is `text`, `status` (a short progress note), `question` (the coding agent is waiting for a
reply), or `done`.

Local LLM
---------

### `/v1/llm/<path>`

`GET` and `POST` requests are relayed to the local LLM's OpenAI-compatible API, with `<path>`
appended to its base URL: for example `POST /v1/llm/v1/chat/completions`. Streaming responses are
passed through. The gateway adds the credentials for the LLM server. If no LLM is configured or it
cannot be reached, the response is 503 `unavailable`.

Notifications
-------------

### `GET /v1/events?after=<seq>&wait=<seconds>`

Notifications from the trusted base to the app, with the same long-polling behaviour as the coding
events:

```json
{"events": [{"seq": 3, "time": "...", "kind": "deletion_rate",
             "account": "work", "count": 23, "window_minutes": 60}],
 "last_seq": 3}
```

Kinds: `accounts_changed` (the account list was changed by the user), `deletion_rate` (the user
has been warned about a high deletion rate), `change_request` (with `id` and the new `state`).

Host listener
-------------

Everything on the host listener requires the secret that the wrapper app generated when it started
the gateway (environment variable `TRIAGE_HOST_TOKEN`). Requests whose `Host` header is not a
loopback address are rejected, to defeat DNS rebinding.

### Control API (`Authorization: Bearer <token>`)

* `PUT /control/v1/accounts` — replaces the account configuration, including credentials, which
  the gateway keeps only in memory. Body: `{"accounts": [AccountConfig, ...]}` (see
  `trusted/gateway/src/config.rs`).
* `PUT /control/v1/llm` — `{"base_url": "http://host.docker.internal:8012", "api_key": "..."}`.
* `GET /control/v1/status` — connection state of each account, deletion rate warnings, and the
  number of pending change requests.
* `GET /control/v1/change-requests?state=pending` — change requests including their full text.
* `POST /control/v1/change-requests/<id>/decision` — `{"approve": true}`. The wrapper app then
  delivers the approved text to the coding sandbox itself.
* `POST /control/v1/coding/events` — `{"request_id": ..., "kind": ..., "text": ...}`, appended to
  the coding events that the app can read.

### Web interface relay

All other paths on the host listener are relayed to the app's web server in the email sandbox
(`TRIAGE_APP_URL`, e.g. `http://app:8080`), including WebSocket and streaming responses, provided
that the request carries the session cookie. The wrapper app obtains the cookie by loading
`/_triage/session?token=<token>` in the web view, which sets an `HttpOnly`, `SameSite=Strict`
cookie and redirects to `/`.

The app must serve its web interface on port 8080 over plain HTTP, using only relative URLs.
