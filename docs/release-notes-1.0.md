# Release notes — 1.0.0

## Почему major

1.0.0 — stable release, заменяющий endpoint-wrapper 0.2.x на безопасный MCP
gateway. Это breaking изменение tool API и configuration, поэтому SemVer
minor/patch был бы вводящим в заблуждение. Ему предшествовал release candidate
1.0.0rc1; изменения с тех пор перечислены в следующем разделе.

## Изменения после 1.0.0rc1

- Gateway стал package `kwork_mcp.gateway` со слоями `parsing → base → reads →
  lookups → offer_flow → actions → writes`; у каждого write action один handler
  (`preflight` / `execute` / `read_back` / `check_fresh_resolution`). Само
  разделение поведение не меняло.
- Read tools сверены с live-ответами Kwork: `list_my_kworks` пропускает
  aggregate status-группу с id `0` и обходит вложенные status-группы;
  `get_exchange_info` принимает голый exchangeInfo object без флага `success`;
  `list_notifications` на `{"success": true}` без `response` возвращает
  `known_empty`, а не `contract_drift`.
- Временные ошибки Kwork («повторите попытку позже», «временно недоступен», «try
  again later» и т.п.) классифицируются как retryable `upstream_unavailable`.
  `duplicate` и `permission` требуют явной формулировки, поэтому неудачный оффер
  больше не выдаётся за «уже отправлен».
- `submit_offer`: durable no-retry boundary пересекает только финальный create;
  сбой открытия формы, FAQ init, draft или template check оставляет запись
  `prepared`, а CSRF/auth failure сбрасывает web login. Оффер, создание которого
  Kwork подтвердил, но ID которого не найден, — `submission_unknown`, а не
  `failed_known`.
- Read-back не считает отсутствием исчезнувшие message, dialog, order или kwork
  и постороннюю kwork status-группу. `edit_message` сохраняет
  `message_text_sha256_at_prepare`, `set_kwork_state` —
  `kwork_status_group_id_at_prepare`, чтобы неизменившийся объект доказывал, что
  write не состоялся.
- Commit-time preflight, который не может прийти к выводу (неоднозначный read,
  contract drift, transient failure), всегда возвращает запись в `prepared` с
  ошибкой, а не в `failed_known`; неоднозначный read отдаётся как retryable
  `upstream_unavailable`.
  Окончательны только `not_found`, `closed_project`, `duplicate`,
  `insufficient_connects`, `validation`, `permission`.
- Account write barrier называет блокирующую запись в новом optional поле
  `ErrorInfo.related_write_id`; `account_status` возвращает
  `unresolved_write_ids`. Новые operator-команды `kwork-mcp-bootstrap
  pending-writes` и `kwork-mcp-bootstrap resolve-write <write_id>
  succeeded|absent` (TTY и явное «да») фиксируют проверенный вручную исход; их
  нужно запускать с тем же `KWORK_*` policy env, что и server.
- Конфигурация проверяется в `main()` до старта server: некорректные `KWORK_*`
  перечисляются по имени, exit code `2`, без traceback. Bootstrap называет
  введённое значение, не прошедшее проверку. Server запускается с
  `show_banner=False`, без непроксированного PyPI update check и cache вне
  `KWORK_STATE_DIR`.
- Proxy ограничен `http`, `socks4`, `socks5` с явным port; `https` и `socks5h`
  отклоняются, scheme приводится к нижнему регистру. Отдельные proxy user/host
  fragments короче 8 символов больше не редактируются сами по себе, а JSON keys
  теряют только secrets от 8 символов и URL userinfo; password и полные формы
  proxy URL редактируются всегда.
- `cryptography` 50.0.1 и `pip` 26.2.1 в `uv.lock` по security advisories.
  `SHA256SUMS` в release содержит голые имена файлов, поэтому
  `sha256sum --check SHA256SUMS` работает в каталоге скачанных assets.

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
  raw/decoded, yarl-canonical и case-normalized percent-escape формы proxy
  URL/authority/userinfo и password не попадают в logs, sanitized raw или MCP
  envelope. Отдельные user/host fragments редактируются от 8 символов.
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
- FastMCP 3.4.5 и MCP SDK 1.28.1 закреплены; application version 1.0.0 доступна в
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
  `release.published` и через protected environments: package публикуется в PyPI,
  а stable release (`1.0.0`, не prerelease) — также `server.json` в MCP Registry.
  Distributions и `SHA256SUMS` прикладываются к GitHub release.

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
write-tools заменены двухфазным protocol. При переходе с 1.0.0rc1 достаточно
раздела «Обновление с 1.0.0rc1» того же guide.

## Известные ограничения

- Kwork/pykwork не являются стабильным официальным публичным API; upstream drift
  может потребовать новый gateway release.
- Reconciliation зависит от доступного read-back. Если side effect нельзя
  идентифицировать однозначно, требуется ручная проверка Kwork и фиксация исхода
  через `kwork-mcp-bootstrap resolve-write`.
- State защищён POSIX permissions/locks, но не зашифрован приложением; runtime 1.0
  поддерживает Linux/macOS, не Windows.
- High watermark — checkpoint paginated snapshot, не гарантия CDC/delta delivery.
- Никакие live writes не входят в release verification; используются fakes и
  contract fixtures.
