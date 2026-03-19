# kwork-mcp

MCP server exposing 25 tools for managing Kwork freelance marketplace via Claude. Wraps pykwork (`kwork>=0.2.0`) with FastMCP 3.x.

## Commands

- `uv run ruff check .` — lint
- `uv run ruff format --check .` — format check
- `uv run python -m pytest tests/ -x -v` — run tests
- `uv run python -m pytest tests/ -x -v --cov=kwork_mcp --cov-report=term-missing` — tests + coverage
- `uv run python -m kwork_mcp` — start server (stdio)

## Project Map

```
src/kwork_mcp/
  __init__.py        — main() entry point (loguru -> stderr, then server.run stdio)
  __main__.py        — python -m kwork_mcp
  config.py          — KworkConfig (pydantic-settings, KWORK_ env prefix)
  session.py         — KworkSessionManager (lazy auth, token persistence, web login, get_exchange_info)
  server.py          — FastMCP("kwork", lifespan=lifespan), tool registration
  errors.py          — api_guard() context manager (KworkException -> ToolError, auto-relogin on 401)
  rate_limiter.py    — InProcessRateLimiter (sliding window, deque + asyncio.Lock)
  utils.py           — shared utilities (formatting, validation, response helpers, tool annotations)
  tools/             — 8 modules, 28 tools (profile, projects, offers, orders, dialogs, kworks, categories, notifications)
tests/               — config, rate_limiter, session, errors, utils, tools tests (92 tests)
```

## Architecture

- **FastMCP 3.x lifespan** yields `{"session": KworkSessionManager}` — tools access via `ctx.lifespan_context["session"]`
- **Auth priority:** KWORK_TOKEN env -> `~/.kwork_token` file -> fresh login (KWORK_LOGIN + KWORK_PASSWORD)
- **Rate limiter:** in-process sliding window (no Redis) — call `session.rate_limit()` before every API call
- **Web flow:** `submit_offer` requires `ensure_web_client()` (triggers `web_login("/exchange")` once)
- **Transport:** stdio only — all logging to stderr via loguru (stdout = MCP JSON-RPC)
- **pykwork direct:** use `kwork.Kwork` (aka `KworkClient`) directly, NOT Pikka's wrapper
- **Auto-relogin:** `api_guard(session=session)` catches 401 and calls `session.relogin()`
- **Error sanitization:** error details go to loguru (stderr), not to the agent; `mask_error_details=True` on FastMCP
- **Tool annotations:** ANNO_READ / ANNO_WRITE / ANNO_DESTRUCTIVE from `utils.py`
- **Input validation:** `validate_positive_int()` (alias `validate_positive_id`), `validate_page()`, `validate_not_empty()` from `utils.py`

## Tool Pattern

Every tool follows this exact structure:
```python
from kwork_mcp.utils import ANNO_READ, validate_positive_id

@mcp.tool(annotations=ANNO_READ)
async def tool_name(param: int, ctx: Context) -> str:
    validate_positive_id(param, "param")
    session: KworkSessionManager = ctx.lifespan_context["session"]
    client = await session.ensure_client()  # or ensure_web_client() for submit
    await session.rate_limit()
    async with api_guard("operation_name", session=session):
        result = await client.some_method(param_name=param)
    return "formatted Cyrillic string"
```

## Adding a New Tool

1. Create `src/kwork_mcp/tools/new_module.py` with `def register(mcp: FastMCP)` containing `@mcp.tool()` functions
2. Add import + `new_module.register(mcp)` call in `src/kwork_mcp/tools/__init__.py`

## pykwork Method Types

- **Typed** (`get_me`, `get_user`, `get_connects`, `get_projects`, `get_categories`, `get_dialogs_page`, `get_dialog_with_user_page`, `send_message`) — return dataclass/model objects, access fields directly (e.g., `actor.username`)
- **Generic** (`project()`, `offer()`, `start_kwork()`, `kworks_status_list()`, etc.) — accept `**params`, return raw `dict`, response typically nested under `"response"` key

## Gotchas

- `Kwork(login, password)` — both are required positional args, even for token-based auth (pass empty strings)
- `client.get_token()` takes NO arguments — uses `self._login`/`self._password` from constructor
- pykwork generic methods (`project()`, `offer()`, `start_kwork()`) accept `**params` passed directly as HTTP POST params — param names must match Kwork API exactly (e.g., `kwork_id=`, NOT `id=` or `kworkId=`)
- `KworkHTTPException` has `.status` and `.response_json` — use these for error classification, NOT string matching
- `submit_exchange_offer` is a 4-step web flow (open page -> CSRF -> draft -> create) — any step can fail
- Captcha (error code 118 in `response_json`): no interactive recovery in MCP, raise ToolError with instructions
- Token file: write with `os.open(..., 0o600)` for atomic permissions, never write-then-chmod
- Private pykwork fields: use `_set_client_token()` / `_get_client_token()` helpers from `session.py`

## Boundaries

- **NEVER:** log to stdout (breaks MCP JSON-RPC), commit .env or .kwork_token
- **ALWAYS:** use native Cyrillic in all user-facing strings, call `rate_limit()` before API calls, wrap API calls in `api_guard(session=session)`
- **ALWAYS:** pass `session=session` to `api_guard()` for auto-relogin on 401
- **ALWAYS:** add input validation (`validate_positive_id`, `validate_page`, `validate_not_empty`) to new tools
- **ALWAYS:** run `uv run ruff check . && uv run python -m pytest tests/ -x` before commit

## Commit Rules

- **NEVER** add `Co-Authored-By` lines to commit messages
- **NEVER** mention Claude, AI, or co-authorship in commit messages or PR descriptions
- Commit messages should be written as if authored solely by the developer
