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

assert kwork_mcp.__version__ == "1.0.0rc1"
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
[[ "$("${runner[@]}" kwork-mcp-bootstrap --version)" == "1.0.0rc1" ]]

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
