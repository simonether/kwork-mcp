# Release notes — 1.0.0rc1

## Почему major

1.0.0rc1 — release candidate версии 1.0.0, заменяющей endpoint-wrapper 0.2.x
на безопасный MCP gateway. Это breaking
изменение tool API и configuration, поэтому SemVer minor/patch был бы вводящим в
заблуждение.

## Основные изменения

- Stable typed `ResultEnvelope` для всех tools: `outputSchema`,
  `structuredContent`, `isError`, human-readable summary.
- Явные read states: `known_data`, `known_empty`, `unknown_error`.
- Полная error taxonomy для auth/account, captcha, permission/IP/CSRF, rate/proxy/
  timeout, business failures, contract drift и ambiguous writes.
- Account-bound session: фактический Kwork identity проверяется для каждого token
  candidate и заново перед writes; первый ID закрепляется на весь server process.
- Separate `kwork-mcp-bootstrap` получает credentials только через TTY/getpass,
  выполняет auth + `get_me` и сохраняет account-bound record. Explicit validated
  import legacy `~/.kwork_token` доступен только по подтверждению; normal startup
  legacy source не читает.
- Normal `kwork-mcp` запускается credentialless по
  `KWORK_EXPECTED_USER_ID + protected store` и fail-closed отклоняет
  login/password/token/phone/proxy environment.
- Private atomic account-scoped record хранит token и optional proxy. Runtime
  credentials динамически редактируются; post-replace durability ambiguity имеет
  отдельный код `credential_update_unknown`.
- Proxy redaction ищет все exact runtime secrets в исходном тексте, объединяет
  overlapping/touching интервалы, затем разбирает authority по последнему `@`;
  raw/decoded, yarl-canonical URL/authority/host и независимо case-normalized
  percent-escape варианты userinfo/fragments не попадают в logs, sanitized raw
  или MCP envelope.
- Child self-cancel при закрытии bootstrap client до store commit сохраняет
  прежний record и первичную ошибку; caller cancellation пробрасывается только
  после cleanup. После успешного либо потенциально committed replace оба случая
  возвращают typed `credential_update_unknown`, не маскируя post-replace
  durability ambiguity.
- Bootstrap client close ограничен абсолютным пятисекундным deadline и коротким
  bounded cancel-drain: зависший или сопротивляющийся cancellation cleanup не
  удерживает account writer/token locks без ограничения.
- Cancellation при освобождении token/account lock сохраняет активную typed
  ambiguity, а после успешного commit без предшествующей ошибки преобразуется в
  `credential_update_unknown`; обе блокировки гарантированно освобождаются.
- Sync release exception до/после unlock также не маскирует body exception.
  Bounded cleanup пишет только безопасный fixed diagnostic; lone post-commit
  release failure классифицируется `credential_update_unknown`.
- Shared SQLite token buckets, route circuits с single half-open probe,
  account-bound HMAC cursors, idempotency ledger, process-held one-writer locks и
  account-level unknown-write barrier. Shared policy закреплена fingerprint и не
  может быть ослаблена другим процессом с тем же state directory.
- Concurrent prepare сравнивает клиентский request, а не изменчивые preflight
  observations; exact replay возвращает одну snapshot-запись и тот же
  HMAC-derived confirmation token после потерянного prepare-response.
- Durable remote marker отделяет safe admission/cancellation от возможного side
  effect: до marker claim возвращается в `prepared`, после marker немедленно
  сохраняется `submission_unknown`. Legacy committing rows без marker semantics
  при schema migration консервативно становятся unknown.
- Вся physical state-dir ancestor chain проверяется по owner/mode через
  FD-relative `O_NOFOLLOW`; допускается один sticky temp boundary, но не обычный
  writable/foreign parent или скрытая nested symlink chain.
- Универсальный `prepare_write`/`commit_write`/`get_write_status`/
  `reconcile_write` для восьми write actions.
- Remote write не retry; ambiguous outcomes получают durable
  `submission_unknown`, включая best-effort recovery после локального ledger
  failure при подтверждённом remote response.
- Discovery разделён на `favorites`, `all`, `category_ids`; добавлены opaque cursor,
  query fingerprint, атомарная scope-проверка pagination и delta-ready high
  watermark.
- Нормализованные ID дополнены санитизированным `raw`: generic routes сохраняют
  весь JSON, а typed routes — все поля закреплённых моделей, включая descriptions;
  собственные offers всегда содержат `project_id`.
- `kwork==0.2.0` закреплён, реальные signatures/routes проверяются при старте и в
  contract tests.
- FastMCP 3.4.5 и MCP SDK 1.28.1 закреплены; application version 1.0.0rc1 доступна в
  handshake.
- Строгая MCP input validation вынесена в sanitizing middleware, чтобы invalid
  offer/message payload не отражался целиком в protocol error.
- Unknown tool возвращается protocol-level JSON-RPC `-32602`, не business
  `isError`; публичный `create_server(config=...)` соблюдает тот же secretless
  steady-state boundary, что и console entrypoint.
- MCP Tasks осознанно отключены: experimental lifecycle не нужен для коротких
  Kwork API calls и не улучшает durable write safety.
- Runtime/dev libraries обновлены и exact-pinned; Python support — 3.12–3.14.
- CI закрепляет action commit SHAs и uv version, проверяет lint, format, strict
  mypy, coverage, package metadata, pinned MCP Registry schema, wheel, dependency
  audit и secrets.
- Release publication выполняется только после явного GitHub
  `release.published` и через protected environments. Для GitHub prerelease
  workflow публикует Python package в PyPI, но намеренно пропускает MCP Registry
  до stable `1.0.0`.

## Исправленные upstream-расхождения

- `list_worker_orders` больше не передаёт unsupported `page` в
  `get_worker_orders`; используется реальный generic paginated route.
- Approval больше не передаёт unsupported `comment`.
- Own-kworks read-back полностью вычитывает `userKworks` по status-группам и
  сверяет первую страницу с `kworksStatusList`.
- Approval reconciliation считает успехом только upstream status `4` («на
  проверке»), а arbitration и другие состояния оставляет неоднозначными.
- Submit offer не сообщает success для HTTP 500, пустого/non-JSON response или
  отсутствующего `offer_id`.
- Web POST не переносит CSRF headers/body через redirects; неуспешный prerequisite
  останавливает flow до финального create.
- Reconciliation не объявляет offer отсутствующим при нормализации Kwork или
  другом candidate того же проекта.
- Неизвестный post-submit order status не используется как отрицательное
  reconciliation evidence.
- Safety-critical collections и pagination валидируются fail-loud; malformed
  falsy values не могут стать отрицательным read-back evidence.
- Test doubles сверяются с реальными signatures закреплённого distribution.

## Обновление

Перед обновлением прочитайте [migration guide](migration-1.0.md). Особое внимание:
удалите secrets из MCP host config, выполните bootstrap (с validated legacy import
либо fresh hidden login), оставьте writes disabled до `account_status`; все прямые
write-tools заменены двухфазным protocol.

## Известные ограничения

- Kwork/pykwork не являются стабильным официальным публичным API; upstream drift
  может потребовать новый gateway release.
- Reconciliation зависит от доступного read-back. Если side effect нельзя
  идентифицировать однозначно, требуется ручная проверка Kwork.
- State защищён POSIX permissions/locks, но не зашифрован приложением; runtime 1.0
  поддерживает Linux/macOS, не Windows.
- High watermark — checkpoint paginated snapshot, не гарантия CDC/delta delivery.
- Никакие live writes не входят в release verification; используются fakes и
  contract fixtures.
