#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 || ! -f "$1" ]]; then
  echo "usage: smoke-wheel.sh /absolute/path/to/kwork_mcp.whl" >&2
  exit 2
fi

wheel_dir="$(cd "$(dirname "$1")" && pwd -P)"
wheel="${wheel_dir}/$(basename "$1")"
smoke_dir="$(mktemp -d)"
cleanup() {
  rm -r -- "$smoke_dir"
}
trap cleanup EXIT
cd "$smoke_dir"

runner=(uv run --isolated --no-project --with "$wheel" --)

"${runner[@]}" python -I - <<'PY'
from importlib.metadata import distribution

import kwork_mcp
from kwork_mcp.bootstrap import run_bootstrap_cli
from kwork_mcp.server import create_server

assert kwork_mcp.__version__ == "1.5.2"
assert callable(run_bootstrap_cli)
create_server()
scripts = {
    entry.name: entry.value
    for entry in distribution("kwork-mcp").entry_points
    if entry.group == "console_scripts"
}
assert scripts == {
    "kwork-mcp": "kwork_mcp:main",
    "kwork-mcp-bootstrap": "kwork_mcp.bootstrap:main",
}
PY

"${runner[@]}" kwork-mcp-bootstrap --help >/dev/null
[[ "$("${runner[@]}" kwork-mcp-bootstrap --version)" == "1.5.2" ]]
"${runner[@]}" kwork-mcp --help >/dev/null
[[ "$("${runner[@]}" kwork-mcp --version)" == "1.5.2" ]]

set +e
"${runner[@]}" kwork-mcp login </dev/null >login.stdout 2>login.stderr
login_status="$?"
set -e
[[ "$login_status" -eq 2 ]]
[[ ! -s login.stdout ]]

# status is offline: with no stored login it explains and creates nothing.
set +e
KWORK_STATE_DIR="${smoke_dir}/state" "${runner[@]}" kwork-mcp status >status.stdout 2>status.stderr
status_status="$?"
set -e
[[ "$status_status" -eq 2 ]]
grep -q "kwork-mcp 1.5.2" status.stdout
[[ ! -e "${smoke_dir}/state" ]]

# Directory inspections start the bare server with no account: it must list the
# tools, answer every call with auth_required and create no state.
KWORK_STATE_DIR="${smoke_dir}/state" "${runner[@]}" python -I - <<'PY'
import json
import subprocess
import sys

messages = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "account_status", "arguments": {}}},
]
server = subprocess.Popen(
    [sys.executable, "-I", "-c", "import kwork_mcp; kwork_mcp.main([])"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
)
assert server.stdin is not None and server.stdout is not None
answers = {}
for message in messages:
    server.stdin.write(json.dumps(message) + "\n")
    server.stdin.flush()
    if "id" in message:
        answers[message["id"]] = json.loads(server.stdout.readline())
server.stdin.close()
server.wait(timeout=30)
assert len(answers[2]["result"]["tools"]) == 22
assert answers[3]["result"]["structuredContent"]["error"]["code"] == "auth_required"
PY
[[ ! -e "${smoke_dir}/state" ]]

set +e
"${runner[@]}" kwork-mcp-bootstrap </dev/null >bootstrap.stdout 2>bootstrap.stderr
bootstrap_status="$?"
set -e
[[ "$bootstrap_status" -eq 2 ]]
[[ ! -s bootstrap.stdout ]]

argv_sentinel="synthetic-wheel-argv-sentinel"
set +e
argv_output="$("${runner[@]}" kwork-mcp-bootstrap "--token=${argv_sentinel}" 2>&1)"
argv_status="$?"
set -e
[[ "$argv_status" -eq 2 ]]
[[ "$argv_output" != *"$argv_sentinel"* ]]

server_argv_sentinel="synthetic-server-argv-sentinel"
set +e
server_argv_output="$("${runner[@]}" kwork-mcp "--token=${server_argv_sentinel}" 2>&1)"
server_argv_status="$?"
set -e
[[ "$server_argv_status" -eq 2 ]]
[[ "$server_argv_output" != *"$server_argv_sentinel"* ]]

server_env_sentinel="synthetic-server-env-sentinel"
set +e
server_env_output="$(
  KWORK_PASSWORD="$server_env_sentinel" "${runner[@]}" kwork-mcp </dev/null 2>&1
)"
server_env_status="$?"
set -e
[[ "$server_env_status" -eq 2 ]]
[[ "$server_env_output" != *"$server_env_sentinel"* ]]

echo "installed wheel smoke: ok"
