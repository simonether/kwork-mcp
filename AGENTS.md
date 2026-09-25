# kwork-mcp

Stdio MCP gateway for the Kwork freelance marketplace: 22 tools (18 typed reads + a
durable `prepare → commit → reconcile` write protocol) on FastMCP 3.x over the pinned
`kwork==0.2.0` client. Python 3.12–3.14, managed with `uv`.

## Commands

- `uv run ruff check .` — lint
- `uv run ruff format --check .` — format check
- `uv run mypy` — strict type check
- `uv run python -m pytest tests/ -q` — tests
- `uv run python -m pytest tests/ -q --cov=kwork_mcp --cov-report=term-missing` — tests + coverage (gate: 92% branch)
- `uv run kwork-mcp` — start the server (stdio); needs a bootstrapped account
- `uv run kwork-mcp-bootstrap` — human TTY CLI: account auth, `pending-writes`, `resolve-write`

## Project map

```
src/kwork_mcp/
  __init__.py       main(): rejects argv/secret env, validates config, runs stdio without banner
  bootstrap.py      kwork-mcp-bootstrap: TTY auth → account-bound credential store; operator write resolution
  config.py         KworkConfig (pydantic-settings, KWORK_ prefix), proxy validation, redaction secrets
  server.py         create_server(): FastMCP app, lifespan, SERVER_INSTRUCTIONS, unknown-tool guard
  session.py        KworkSessionManager: lazy auth, account identity checks, call_read / call_write_step
  coordination.py   CoordinationStore: shared SQLite — rate limits, circuits, cursors, write ledger, writer lock
  contracts.py      pinned pykwork signatures and generic route params (enforce_route_params)
  security.py       SecureTokenStore, sanitize_external / redact_text, log redaction
  errors.py         GatewayError taxonomy, classify_upstream_error
  models.py         ResultEnvelope, records, write requests, WriteState
  middleware.py     strict, sanitized tool-input validation
  upstream.py       GatewayKworkClient (keeps success:false payloads), secure web client
  gateway/          layered gateway, each module builds on the previous one:
    parsing.py      envelope/paging/scalar helpers
    base.py         shared state
    reads.py        typed read operations
    lookups.py      exhaustive scans for preflight and read-back
    offer_flow.py   submit_offer web flow (form → FAQ → draft → template check → create)
    actions.py      one ActionHandler per WriteAction: preflight / execute / read_back
    writes.py       durable prepare / commit / status / reconcile protocol
  tools/            read_tools.py, write_tools.py, common.py (envelope helpers, annotations)
tests/              pytest suite; test_live_contracts.py holds anonymized live response shapes
docs/               architecture, configuration, security, migration and release notes (Russian)
```

## Architecture

- **Secretless steady state.** The server accepts only safe env (`KWORK_EXPECTED_USER_ID`,
  `KWORK_PERSIST_TOKEN=true`, `KWORK_ENABLE_WRITES`, limits, `KWORK_STATE_DIR`). Login, password,
  token, phone and proxy go only through `kwork-mcp-bootstrap` into the account store.
- **Tools** get the gateway via `gateway_from_context(ctx)` and return a `ResultEnvelope` through
  `success()` / `failure()` / `unexpected_failure()`; `knowledge_state` is `known_data`,
  `known_empty` or `unknown_error`.
- **Remote calls** go through `session.call_read(route, op)` (retries, circuit, relogin on 401)
  or `session.call_write_step(route, op, before_remote_attempt=...)` (never retried).
- **Write ledger.** `prepare_write` runs the action's preflight and stores the exact payload;
  `commit_write` claims it under a per-account file lock, re-runs preflight, marks the durable
  remote boundary right before the side-effecting call, and records `succeeded`, `failed_known`
  or `submission_unknown`. A `submission_unknown` write blocks further commits for the account
  until `reconcile_write` or the operator resolves it.
- **Read-back is evidence-based.** A handler returns present only when the side effect is
  proven, absent only when absence is proven (unchanged object, prepare-time fingerprint), and
  raises `AmbiguousWriteError` otherwise. A missing object or an unrelated status is never
  evidence of absence.
- **Contracts are fail-loud.** Unexpected response shapes raise `ContractDriftError`; generic
  pykwork routes must pass `enforce_route_params`.

## Adding things

- **Read tool:** method in `gateway/reads.py` → registration in `tools/read_tools.py` with
  `output_schema=ResultEnvelope[...]`. Generic routes need `enforce_route_params` and an entry
  in `contracts.py`. Test against a realistic (anonymized) response shape.
- **Write action:** request model + `WriteAction` + `WriteRequest` union in `models.py`, an
  `ActionHandler` in `gateway/actions.py` registered in `ACTION_HANDLERS`, route params in
  `contracts.py`, tests in the style of `tests/test_write_safety.py` (prepare/commit/read-back).

## Live Kwork API quirks

- `exchangeInfo` answers with a bare object that has no `success` flag.
- `kworksStatusList` ends with an aggregate "all" group (id 0) that repeats every kwork; older
  responses nested status groups inside a group's `kworks`.
- `notifications` with nothing to report is `{"success": true}` without `response`.
- `userKworks`, `offers`, `dialogs`, `inboxes` and `projects` carry `paging` with
  `page/total/limit/pages`.
- pykwork generic methods pass `**params` as POST params; names must match the Kwork API exactly.
- `KworkHTTPException` exposes `.status` and `.response_json`; classify with those.

## Boundaries

- NEVER write to stdout from the server (stdout is MCP JSON-RPC); logs go to stderr via loguru.
- NEVER accept secrets in server env/argv, and never put secret values into errors, logs or tool
  output — errors carry only safe messages; details go to `diagnostic`.
- NEVER perform a remote write outside the ledger, and never auto-retry a remote write.
- ALWAYS use native Russian in user-facing strings; keep code comments in English (ruff RUF003
  rejects Cyrillic look-alike letters in comments).
- ALWAYS run `uv run ruff check . && uv run mypy && uv run python -m pytest tests/ -q` before a
  commit.
- Live checks against a real account are read-only unless the owner explicitly allows a write;
  anonymize any captured response before it becomes a fixture.

## Commit rules

- NEVER add `Co-Authored-By` lines to commit messages.
- NEVER mention AI assistants (Claude, Codex or others) or co-authorship in commit messages or
  PR descriptions.
- Commit messages are written as if authored solely by the developer.
