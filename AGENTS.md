# kwork-mcp

Stdio MCP gateway for the Kwork freelance marketplace: 22 tools (18 typed reads + a
durable `prepare → commit → reconcile` write protocol) on FastMCP 4 / MCP SDK 2 over the pinned
`kwork==0.2.0` client. Python 3.12–3.14 on macOS, Linux and Windows, managed with `uv`.

## Commands

- `uv run ruff check .` — lint
- `uv run ruff format --check .` — format check
- `uv run mypy` — strict type check
- `uv run mypy --platform win32` — the same check for the Windows branches
- `uv run python -m pytest tests/ -q` — tests
- `uv run python -m pytest tests/ -q --cov=kwork_mcp --cov-report=term-missing` — tests + coverage (gate: 92% branch)
- `uv run kwork-mcp` — start the server (stdio); needs a bootstrapped account
- `uv run kwork-mcp login` — human TTY CLI: account auth; also `status`, `logout`, `pending-writes`,
  `resolve-write` (`kwork-mcp-bootstrap` is the old alias)

## Project map

```
src/kwork_mcp/
  __init__.py       main(): bare command = stdio server (rejects secret env, no banner); args → terminal CLI
  bootstrap.py      terminal CLI: login (TTY auth → store, prints client commands), status, logout, write resolution
  config.py         KworkConfig (pydantic-settings, KWORK_ prefix), bound-account selection, proxy validation
  server.py         create_server(): FastMCP app, lifespan, SERVER_INSTRUCTIONS, unknown-tool guard
  session.py        KworkSessionManager: lazy auth, account identity checks, call_read / call_write_step
  coordination.py   CoordinationStore: shared SQLite — rate limits, circuits, cursors, write ledger, writer lock
  contracts.py      pinned pykwork signatures and generic route params (enforce_route_params)
  security.py       SecureTokenStore, state directory checks, sanitize_external / redact_text, log redaction
  private_fs.py     platform layer: POSIX modes or Windows owner/DACL checks, locks, DPAPI sealing
  windows.py        Win32 calls through ctypes (imported only on Windows)
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
site/               GitHub Pages landing page (Russian), deployed by .github/workflows/pages.yml;
                    static, no build: index.html, style.css, app.js, theme.js, self-hosted fonts/;
                    og.png is rendered from assets/og.html; the pinned version is checked by tests
```

## Architecture

- **Secretless steady state.** The server accepts only safe env (`KWORK_PERSIST_TOKEN=true`,
  `KWORK_WRITES`, limits, `KWORK_STATE_DIR`, optional `KWORK_EXPECTED_USER_ID`). Login,
  password, token, phone and proxy go only through `kwork-mcp login` into the account store.
- **Platforms.** All OS differences go through `private_fs`. POSIX keeps state private with
  0700/0600 modes, an fd-anchored `O_NOFOLLOW` directory walk and `flock`. Windows has no mode
  bits: state directories are created with an owner-only protected DACL (ancestors outside the
  profile must not let others rename or re-permission them), token and lock files are checked
  by owner and DACL through their handles and the SQLite ledger by path before each
  connection, tokens are sealed with DPAPI bound to the account scope, and locks use
  `msvcrt.locking`. The default state stays in the profile (`~/.local/state/kwork-mcp`), not
  in AppData, which packaged (MSIX) clients see virtualized.
  Write `if sys.platform == "win32": ... else: ...` rather than an early return, so that
  `mypy --platform win32` sees no unreachable code; coverage skips the Windows branches and
  `windows.py`, which run on the Windows CI job.
- **Account selection.** Without `KWORK_EXPECTED_USER_ID` the server serves the single account
  that login stored a token for (`load_server_config`). With none or several it still starts
  without a session: every tool answers `auth_required` or `account_binding_required`
  (`AccountSelectionError.code`), so the agent can tell the user to run login; directory
  inspections (Glama, LobeHub) need the started server too. An unreadable token store stops
  startup with exit 2. The CLI never echoes argv.
- **Writes mode.** `KWORK_WRITES=confirm|auto|off` (default confirm). confirm: the agent gets
  the user's yes in chat (`prepare_write.confirmation=chat`, mode-specific server instructions);
  the server cannot verify it. auto: the agent commits on its own. off: prepare/commit are not
  registered. The removed `KWORK_ENABLE_WRITES` stops startup (`reject_removed_settings`).
  MCP elicitation dialogs were tried in 1.5.0 and removed in 1.5.1: Claude Desktop lacks them
  and Codex.app with `approval_policy = "never"` declines them on its own.
- **Tools** get the gateway via `gateway_from_context(ctx)` and return a `ResultEnvelope` through
  `success()` / `failure()` / `unexpected_failure()`; `knowledge_state` is `known_data`,
  `known_empty` or `unknown_error`.
- **Remote calls** go through `session.call_read(route, op)` (retries, circuit, relogin on 401)
  or `session.call_write_step(route, op, before_remote_attempt=...)` (never retried).
- **Write ledger.** `prepare_write` runs the action's preflight and stores the exact payload;
  `commit_write` claims it under a per-account file lock, re-runs preflight, marks the durable
  remote boundary right before the side-effecting call, and records `succeeded`, `failed_known`
  or `submission_unknown`. A `submission_unknown` write blocks further commits for the account
  until `reconcile_write` or the operator resolves it. A write commits and reconciles only on
  the `KWORK_SITE` it was prepared for (`prepared_site`; legacy rows count as `ru`).
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
- kwork.com (`KWORK_SITE=com`) shares the account and token with kwork.ru but has no project
  exchange: `projects` and `favoriteCategories` return `{"success": true}` without `response`.
  `project` by ID still works; worker orders differ between the sites.
- The web login (`getWebAuthToken` → login URL → landing page) reports the landing page's status.
  kwork.ru retired `/exchange` (404 since 2026-10); land on `/projects`.
- Kwork stores an offer's title and description HTML-escaped (`&laquo;`/`&raquo;`) with blank lines
  collapsed; compare offers with `_normalize_offer_text`. The createoffer response may carry no offer
  ID, so commit falls back to read-back.
- Messages read back HTML-escaped too (`"` as `&quot;`), but keep blank lines and `«»`; compare them
  with `_normalize_message_text`.
- `inboxes` numbers dialog pages from the oldest message and fills them from the newest end: the last
  page holds the latest messages, only page 1 may be short, and a call without `page` returns the last
  page.

## Boundaries

- NEVER write to stdout from the server (stdout is MCP JSON-RPC); logs go to stderr via loguru.
- NEVER accept secrets in server env/argv, and never put secret values into errors, logs or tool
  output — errors carry only safe messages; details go to `diagnostic`.
- NEVER perform a remote write outside the ledger, and never auto-retry a remote write.
- ALWAYS use native Russian in user-facing strings; keep code comments in English (ruff RUF003
  rejects Cyrillic look-alike letters in comments).
- ALWAYS run `uv run ruff check . && uv run mypy && uv run mypy --platform win32 &&
  uv run python -m pytest tests/ -q` before a commit.
- Live checks against a real account are read-only unless the owner explicitly allows a write;
  anonymize any captured response before it becomes a fixture.

## Commit rules

- NEVER add `Co-Authored-By` lines to commit messages.
- NEVER mention AI assistants (Claude, Codex or others) or co-authorship in commit messages or
  PR descriptions.
- Commit messages are written as if authored solely by the developer.
