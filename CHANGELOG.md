# Changelog

Формат основан на [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
проект следует [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-09-25

Первый стабильный релиз 1.0. Все read-tools проверены на реальном аккаунте Kwork.

### Added

- `account_status.unresolved_write_ids` и `error.related_write_id`: блокировка
  записей аккаунта теперь называет запись, которую нужно сверить.
- `kwork-mcp-bootstrap pending-writes` и `kwork-mcp-bootstrap resolve-write
  <write_id> succeeded|absent`: оператор фиксирует исход записи, которую
  read-back не может разрешить (TTY и явное подтверждение).
- Инструкции подключения для Claude Code, Claude Desktop и Cursor.

### Changed

- `gateway.py` разделён на пакет `kwork_mcp.gateway`: reads, lookups, offer web
  flow, отдельный handler на каждое write-действие и durable write protocol.
- Для `submit_offer` durable remote boundary пересекает только финальный вызов
  создания оффера: сбой открытия формы, черновика или CSRF оставляет запись
  `prepared`, а CSRF/auth сбрасывает web-сессию для повторного входа.
- Preflight, который не смог прийти к выводу, возвращает запись в `prepared` с
  retryable-ошибкой вместо `failed_known` или ложного требования reconcile.
- Прокси: только `http`, `socks4`, `socks5` с явным портом, как поддерживает
  коннектор.
- Сервер стартует без баннера FastMCP (он делал непроксированный запрос к PyPI и
  писал кэш вне `KWORK_STATE_DIR`); ошибки конфигурации выводятся без traceback.
- Классификатор Development Status — Production/Stable; MCP Registry
  публикуется для стабильного релиза.

### Fixed

- `list_my_kworks` падал на агрегатной группе «Все» (id 0); вложенные группы
  статусов тоже обрабатываются.
- `get_exchange_info` отвергал настоящий ответ `exchangeInfo` без флага
  `success`.
- `list_notifications` считал пустой ответ `{"success": true}` дрейфом контракта.
- «Повторите попытку позже» и «временно недоступен» классифицировались как
  `duplicate`/`permission`; теперь это retryable `upstream_unavailable`, и
  неотправленный оффер больше не выдаётся за уже существующий.
- Созданный оффер, чей ID не удалось найти из-за rate limit, записывался как
  `failed_known` с `safe_to_retry`; теперь это `submission_unknown`.
- Reconcile считал пропавшие сообщение, диалог, заказ или кворк и статус вроде
  «На модерации» доказательством отсутствия записи. Теперь это неоднозначно, а
  правка сообщения и смена статуса кворка сверяются с состоянием на момент prepare.
- Короткий логин прокси (например, `user`) портил ключи вроде `user_id` при
  redaction и ломал `list_dialogs`.
- Bootstrap называл виновником окружение при опечатке во введённом значении.
- `cryptography` 50.0.1 (PYSEC-2026-3552); в релизном `SHA256SUMS` теперь
  голые имена файлов.

## [1.0.0rc1] - 2026-07-28

### Added

- Typed MCP result envelope, structured content and stable error taxonomy.
- Account binding and write readiness status.
- Shared SQLite rate limits, circuits, signed cursors and durable write ledger.
- Safe prepare/commit/status/reconcile protocol for all remote writes.
- Full project discovery modes, opaque pagination cursor and high watermark.
- Contract verification, sanitized generic raw envelopes, and complete pinned
  typed-model fields.
- Separate TTY/getpass `kwork-mcp-bootstrap`, validated legacy token import and
  credentialless account-bound normal startup.

### Changed

- This release candidate is published to PyPI for opt-in validation; MCP Registry
  publication remains disabled until stable 1.0.0.
- All direct write tools were replaced by the safe write protocol.
- Project discovery and several read tool names/outputs are breaking changes.
- Normal MCP configuration is secretless and fail-closed for auth/proxy env;
  writes are disabled by default.
- Runtime and development dependencies are exact-pinned and updated.
- Python support is 3.12–3.14; application version is 1.0.0rc1.

### Fixed

- Correct paginated worker order route and approval parameters.
- Fully paginate own kworks through `userKworks` instead of treating the
  first-page `kworksStatusList` snapshot as complete.
- Reconcile order approval only against upstream status `4` (`on_review`).
- No false submit-offer success without a confirmed offer ID.
- Invalid explicit tokens are not silently reused during relogin.
- Token and optional proxy are stored atomically with private permissions,
  account/writer coordination and dynamic runtime redaction.
- Post-replace durability failures are reported as `credential_update_unknown`
  instead of claiming that the old credential record survived.
- Ambiguous write responses are never automatically retried.
- Durable remote-boundary markers make pre-remote cancellation/admission failures
  retryable with the same confirmation, while post-boundary cancellation becomes
  `submission_unknown`; legacy committing rows migrate conservatively.
- Exact prepare replays recover the same HMAC-derived confirmation token.
- State directories reject writable/foreign ancestor chains and nested symlink
  hops using FD-relative `O_NOFOLLOW` traversal.
- Secret-bearing programmatic server configs are rejected, redaction handles raw
  sensitive keys/overlapping values, and unknown tools return JSON-RPC `-32602`.
- Proxy redaction finds exact secrets against the original text, union-merges
  overlapping/touching spans before authority parsing, covers raw/decoded,
  yarl-canonical URL/authority/host forms, and independently case-normalized
  percent escapes in userinfo through the final `@`.
- Bootstrap close cancellation preserves the original record before commit and
  cannot mask typed `credential_update_unknown` after a successful or
  potentially committed credential-store replace.
- Cancellation during credential/account lock release cannot mask an active
  typed ambiguity; if it first arrives after store commit, bootstrap also
  returns `credential_update_unknown`.
- A synchronous lock-release failure likewise cannot replace an active body
  exception. Release cleanup is deadline-bounded; a lone release failure after
  credential commit becomes a sanitized `credential_update_unknown`.
- Bootstrap client cleanup has an absolute five-second deadline plus bounded
  cancel-drain, so a cancellation-resistant close cannot retain account locks.
- Web redirects cannot forward mutating payloads or CSRF headers off-origin.
- Offer reconciliation handles benign normalization and keeps conflicting
  same-project candidates ambiguous.
- Retry hints, shared circuit duration and read sleeps are explicitly bounded.
- Discovery cursors survive process restart without a prior account-status call.

### Removed

- Legacy `KWORK_TOKEN_FILE`, implicit `.env` loading and in-process-only limiter.
- Experimental MCP Tasks and out-of-scope pipeline/business integrations.

[1.0.0]: https://github.com/simonether/kwork-mcp/releases/tag/v1.0.0
[1.0.0rc1]: https://github.com/simonether/kwork-mcp/releases/tag/v1.0.0rc1
