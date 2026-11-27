#!/bin/sh
# Runs the local LLM server inside a macOS sandbox (see llama-server.sb).
#
#   run-llm.sh <model.gguf> [port]
#
# The API key is read from the environment variable TRIAGE_LLM_API_KEY; store the same key with
# `triage secret llm-api-key`, and tell the host tool the port with `triage init --llm-port`.
#
# Part of the trusted base. NOTE: this script and the sandbox profile have not yet been tested
# on macOS; check the server's output for sandbox violations the first time you use it.
set -eu

model=${1:?usage: run-llm.sh <model.gguf> [port]}
port=${2:-8012}
: "${TRIAGE_LLM_API_KEY:?set TRIAGE_LLM_API_KEY to the key stored with: triage secret llm-api-key}"

here=$(cd "$(dirname "$0")" && pwd)
server=$(command -v llama-server) || { echo "llama-server not found (brew install llama.cpp)" >&2; exit 1; }
# The sandbox profile needs real paths, without symbolic links.
server=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$server")
model=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$model")
model_dir=$(dirname "$model")

# --host 127.0.0.1: never listen on other interfaces.
# --jinja: use the model's chat template, which is what makes tool calls work.
# --offline: never download anything.
# --no-webui: only the API is needed.
# The key is passed through the environment, so that it does not show up in the process list.
export LLAMA_API_KEY="$TRIAGE_LLM_API_KEY"
exec sandbox-exec \
    -D "LISTEN=localhost:$port" \
    -D "MODEL_DIR=$model_dir" \
    -D "SERVER=$server" \
    -f "$here/llama-server.sb" \
    "$server" \
    --model "$model" \
    --host 127.0.0.1 --port "$port" \
    --jinja --offline --no-webui \
    --ctx-size 16384
