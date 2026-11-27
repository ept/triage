//! The sandbox API: what the email sandbox can ask of the gateway. See `docs/gateway-api.md`.
//!
//! This listener has no authentication; it must only be reachable from the email sandbox.

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::rejection::{JsonRejection, QueryRejection};
use axum::extract::{
    DefaultBodyLimit, FromRequest, FromRequestParts, Path, Query as UrlQuery, Request, State,
};
use axum::http::{HeaderName, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use tokio::sync::mpsc;
use tokio_stream::StreamExt;
use tokio_stream::wrappers::ReceiverStream;

use crate::error::{ApiError, ApiResult};
use crate::imap::{DraftRequest, FetchItem};
use crate::relay;
use crate::rules::{self, Query, Role};
use crate::state::{ChangeRequestKind, EventLog, Gateway};

const MAX_BODY: usize = 1024 * 1024;
const MAX_LLM_BODY: usize = 16 * 1024 * 1024;
const MAX_WAIT_SECONDS: u64 = 60;

type Shared = State<Arc<Gateway>>;

/// A JSON request body; malformed input is reported in the API's own error format.
pub struct Input<T>(pub T);

impl<S: Send + Sync, T: DeserializeOwned> FromRequest<S> for Input<T> {
    type Rejection = ApiError;

    async fn from_request(request: Request, state: &S) -> Result<Self, ApiError> {
        match Json::<T>::from_request(request, state).await {
            Ok(Json(value)) => Ok(Input(value)),
            Err(rejection) => Err(ApiError::bad_request(match rejection {
                JsonRejection::JsonDataError(e) => format!("invalid request: {}", e.body_text()),
                JsonRejection::JsonSyntaxError(_) => "the request body is not valid JSON".into(),
                JsonRejection::MissingJsonContentType(_) => {
                    "the request must have the content type application/json".into()
                }
                other => other.body_text(),
            })),
        }
    }
}

/// Query string parameters, with the same error reporting.
pub struct Params<T>(pub T);

impl<S: Send + Sync, T: DeserializeOwned + Send> FromRequestParts<S> for Params<T> {
    type Rejection = ApiError;

    async fn from_request_parts(
        parts: &mut axum::http::request::Parts,
        state: &S,
    ) -> Result<Self, ApiError> {
        match UrlQuery::<T>::from_request_parts(parts, state).await {
            Ok(UrlQuery(value)) => Ok(Params(value)),
            Err(rejection @ QueryRejection::FailedToDeserializeQueryString(_)) => {
                Err(ApiError::bad_request(rejection.body_text()))
            }
            Err(other) => Err(ApiError::bad_request(other.body_text())),
        }
    }
}

pub fn router(gateway: Arc<Gateway>) -> Router {
    let llm = Router::new()
        .route("/v1/llm/{*path}", any(llm_relay))
        .layer(DefaultBodyLimit::max(MAX_LLM_BODY));
    Router::new()
        .route("/v1/health", get(health))
        .route("/v1/accounts", get(accounts))
        .route("/v1/folders/list", post(folders_list))
        .route("/v1/folders/create", post(folders_create))
        .route("/v1/folders/status", post(folders_status))
        .route("/v1/messages/list", post(messages_list))
        .route("/v1/messages/fetch", post(messages_fetch))
        .route("/v1/messages/search", post(messages_search))
        .route("/v1/messages/flags", post(messages_flags))
        .route("/v1/messages/move", post(messages_move))
        .route("/v1/drafts", post(drafts))
        .route("/v1/log", get(log))
        .route("/v1/change-requests", post(change_request_submit))
        .route("/v1/change-requests/{id}", get(change_request_get))
        .route("/v1/coding/events", get(coding_events))
        .route("/v1/events", get(events))
        .layer(DefaultBodyLimit::max(MAX_BODY))
        .merge(llm)
        .fallback(|| async { ApiError::not_found("no such endpoint") })
        .with_state(gateway)
}

async fn health() -> Json<Value> {
    Json(json!({"status": "ok", "version": env!("CARGO_PKG_VERSION")}))
}

async fn accounts(State(gateway): Shared) -> Json<Value> {
    let accounts: Vec<Value> = gateway
        .accounts()
        .iter()
        .map(|account| {
            json!({
                "id": account.config.id,
                "address": account.config.address,
                "display_name": account.config.display_name,
            })
        })
        .collect();
    Json(json!({"accounts": accounts}))
}

// ----- folders

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AccountInput {
    account: String,
}

async fn folders_list(
    State(gateway): Shared,
    Input(input): Input<AccountInput>,
) -> ApiResult<Json<Value>> {
    let folders = gateway.account(&input.account)?.list_folders().await?;
    Ok(Json(json!({"folders": folders})))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FolderCreateInput {
    account: String,
    name: String,
}

async fn folders_create(
    State(gateway): Shared,
    Input(input): Input<FolderCreateInput>,
) -> ApiResult<Json<Value>> {
    let account = gateway.account(&input.account)?;
    let outcome = account.create_folder(&input.name).await;
    gateway.record(
        &input.account,
        "create_folder",
        json!({"folder": input.name}),
        &outcome,
    );
    outcome?;
    Ok(Json(json!({})))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FolderInput {
    account: String,
    folder: String,
}

async fn folders_status(
    State(gateway): Shared,
    Input(input): Input<FolderInput>,
) -> ApiResult<Json<Value>> {
    let status = gateway
        .account(&input.account)?
        .status(&input.folder)
        .await?;
    Ok(Json(json!(status)))
}

// ----- reading messages

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ListInput {
    account: String,
    folder: String,
    #[serde(default)]
    uid_min: Option<u32>,
    #[serde(default)]
    uid_max: Option<u32>,
    #[serde(default)]
    limit: Option<usize>,
}

async fn messages_list(
    State(gateway): Shared,
    Input(input): Input<ListInput>,
) -> ApiResult<Json<Value>> {
    let list = gateway
        .account(&input.account)?
        .list_messages(
            &input.folder,
            input.uid_min.unwrap_or(1),
            input.uid_max,
            input.limit.unwrap_or(rules::MAX_LIST_LIMIT),
        )
        .await?;
    Ok(Json(json!(list)))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FetchInput {
    account: String,
    folder: String,
    uids: Vec<u32>,
}

async fn messages_fetch(
    State(gateway): Shared,
    Input(input): Input<FetchInput>,
) -> ApiResult<Response> {
    let account = gateway.account(&input.account)?;
    let (tx, mut rx) = mpsc::channel::<FetchItem>(8);
    tokio::spawn(async move {
        account.fetch_messages(&input.folder, &input.uids, tx).await;
    });
    // If the operation fails before producing anything, report it as an ordinary error.
    let first = match rx.recv().await {
        Some(Ok(bytes)) => bytes,
        Some(Err(error)) => return Err(error),
        None => return Err(ApiError::unavailable("the fetch was interrupted")),
    };
    // Later failures can only be reported inside the stream, as a final error frame.
    let rest = ReceiverStream::new(rx).map(|item| match item {
        Ok(bytes) => bytes,
        Err(error) => {
            let mut line = serde_json::to_vec(&error.body()).expect("serializable");
            line.push(b'\n');
            line.into()
        }
    });
    let stream = tokio_stream::once(first)
        .chain(rest)
        .map(Ok::<_, std::convert::Infallible>);
    Ok((
        [(header::CONTENT_TYPE, "application/vnd.triage.messages")],
        Body::from_stream(stream),
    )
        .into_response())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SearchInput {
    account: String,
    folder: String,
    query: Query,
}

async fn messages_search(
    State(gateway): Shared,
    Input(input): Input<SearchInput>,
) -> ApiResult<Json<Value>> {
    let result = gateway
        .account(&input.account)?
        .search(&input.folder, &input.query)
        .await?;
    Ok(Json(json!(result)))
}

// ----- changing mailboxes

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FlagsInput {
    account: String,
    folder: String,
    uid_validity: u32,
    uids: Vec<u32>,
    #[serde(default)]
    add: Vec<String>,
    #[serde(default)]
    remove: Vec<String>,
}

async fn messages_flags(
    State(gateway): Shared,
    Input(input): Input<FlagsInput>,
) -> ApiResult<Json<Value>> {
    let account = gateway.account(&input.account)?;
    let outcome = account
        .store_flags(
            &input.folder,
            input.uid_validity,
            &input.uids,
            &input.add,
            &input.remove,
        )
        .await;
    let (add, remove) = match &outcome {
        Ok((add, remove)) => (add.clone(), remove.clone()),
        Err(_) => (input.add.clone(), input.remove.clone()),
    };
    gateway.record(
        &input.account,
        "flags",
        json!({
            "folder": input.folder,
            "uid_validity": input.uid_validity,
            "uids": input.uids,
            "add": add,
            "remove": remove,
        }),
        &outcome,
    );
    outcome?;
    Ok(Json(json!({})))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MoveInput {
    account: String,
    folder: String,
    uid_validity: u32,
    uids: Vec<u32>,
    destination: String,
}

async fn messages_move(
    State(gateway): Shared,
    Input(input): Input<MoveInput>,
) -> ApiResult<Json<Value>> {
    let account = gateway.account(&input.account)?;
    let outcome = account
        .move_messages(
            &input.folder,
            input.uid_validity,
            &input.uids,
            &input.destination,
        )
        .await;
    let mut fields = json!({
        "folder": input.folder,
        "uid_validity": input.uid_validity,
        "destination": input.destination,
    });
    match &outcome {
        Ok(result) => {
            fields["destination_uid_validity"] = json!(result.destination_uid_validity);
            fields["moved"] = json!(result.moved);
        }
        Err(_) => fields["uids"] = json!(input.uids),
    }
    gateway.record(&input.account, "move", fields, &outcome);
    let result = outcome?;
    if matches!(
        result.destination_role,
        Some(Role::Trash) | Some(Role::Junk)
    ) {
        gateway.count_deletions(&input.account, result.moved.len() as u32);
    }
    Ok(Json(json!(result)))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DraftInput {
    account: String,
    #[serde(flatten)]
    draft: DraftRequest,
}

async fn drafts(State(gateway): Shared, Input(input): Input<Value>) -> ApiResult<Json<Value>> {
    // `flatten` and `deny_unknown_fields` do not combine, so take the account out by hand.
    let Value::Object(mut fields) = input else {
        return Err(ApiError::bad_request("the request body must be an object"));
    };
    let account_id = match fields.remove("account") {
        Some(Value::String(id)) => id,
        _ => return Err(ApiError::bad_request("missing field `account`")),
    };
    let draft: DraftRequest = serde_json::from_value(Value::Object(fields))
        .map_err(|e| ApiError::bad_request(format!("invalid request: {e}")))?;
    let input = DraftInput {
        account: account_id,
        draft,
    };

    let account = gateway.account(&input.account)?;
    let outcome = account.create_draft(&input.draft).await;
    let mut fields = json!({"in_reply_to": input.draft.in_reply_to});
    if let Ok(result) = &outcome {
        fields["folder"] = json!(result.folder);
        fields["message_id"] = json!(result.message_id);
    }
    gateway.record(&input.account, "draft", fields, &outcome);
    Ok(Json(json!(outcome?)))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LogParams {
    #[serde(default)]
    after: u64,
    #[serde(default)]
    limit: Option<usize>,
}

async fn log(State(gateway): Shared, Params(params): Params<LogParams>) -> ApiResult<Json<Value>> {
    let limit = params.limit.unwrap_or(500).clamp(1, 1000);
    let (entries, last_seq) = gateway
        .oplog
        .read(params.after, limit)
        .map_err(|_| ApiError::unavailable("the operation log could not be read"))?;
    Ok(Json(json!({"entries": entries, "last_seq": last_seq})))
}

// ----- change requests, coding agent, notifications

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ChangeRequestInput {
    kind: ChangeRequestKind,
    text: String,
}

async fn change_request_submit(
    State(gateway): Shared,
    Input(input): Input<ChangeRequestInput>,
) -> ApiResult<Response> {
    let request = gateway.submit_change_request(input.kind, &input.text)?;
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({"id": request.id, "state": request.state})),
    )
        .into_response())
}

async fn change_request_get(
    State(gateway): Shared,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let request = gateway.change_request(&id)?;
    Ok(Json(json!({
        "id": request.id,
        "kind": request.kind,
        "state": request.state,
    })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EventParams {
    #[serde(default)]
    after: u64,
    #[serde(default)]
    wait: u64,
}

async fn read_events(log: &EventLog, params: EventParams) -> Json<Value> {
    let wait = Duration::from_secs(params.wait.min(MAX_WAIT_SECONDS));
    let (events, last_seq) = log.read(params.after, wait).await;
    Json(json!({"events": events, "last_seq": last_seq}))
}

async fn coding_events(State(gateway): Shared, Params(params): Params<EventParams>) -> Json<Value> {
    read_events(&gateway.coding_events, params).await
}

async fn events(State(gateway): Shared, Params(params): Params<EventParams>) -> Json<Value> {
    read_events(&gateway.events, params).await
}

// ----- local LLM

async fn llm_relay(State(gateway): Shared, request: Request) -> ApiResult<Response> {
    let method = request.method().as_str();
    if method != "GET" && method != "POST" {
        return Err(ApiError::bad_request(
            "only GET and POST are relayed to the LLM",
        ));
    }
    let llm = gateway
        .llm()
        .ok_or_else(|| ApiError::unavailable("no local LLM is configured"))?;
    let path_and_query = request
        .uri()
        .path_and_query()
        .map(|pq| pq.as_str())
        .unwrap_or("");
    let rest = path_and_query
        .strip_prefix("/v1/llm/")
        .ok_or_else(|| ApiError::not_found("no such endpoint"))?;
    let url = relay::join_url(&llm.base_url, rest);
    let mut add: Vec<(HeaderName, HeaderValue)> = Vec::new();
    if let Some(key) = &llm.api_key {
        let value = HeaderValue::from_str(&format!("Bearer {}", key.expose()))
            .map_err(|_| ApiError::unavailable("the LLM credentials are not usable"))?;
        add.push((header::AUTHORIZATION, value));
    }
    relay::forward(
        &gateway.http,
        &url,
        request,
        &["authorization", "cookie"],
        add,
    )
    .await
}
