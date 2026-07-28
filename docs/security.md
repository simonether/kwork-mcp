# Security model

## Trust boundaries

Kwork API, web pages и любой marketplace content считаются внешним недоверенным
вводом. Поля сохраняются как данные и маркируются
`meta.content_trust=external_untrusted`; server instructions запрещают трактовать
тексты проектов, пользователей, сообщений и уведомлений как инструкции.

MCP transport — локальный stdio. Сервер не открывает HTTP port и не реализует
удалённую MCP-аутентификацию. Некоторые MCP hosts сериализуют configured env в
собственный argv; дочерний server не может стереть утечку из parent process.
Поэтому production entrypoint вообще отклоняет secret-bearing auth/proxy env.
Безопасность запуска Codex, доступа к state directory и шифрования диска остаётся
ответственностью оператора.

## State directory trust

Проверка не ограничивается final `0700`: gateway fail-closed обходит всю
физическую ancestor chain от `/` через directory FDs. Допускаются только каталоги
текущего OS user или root; group/other-writable ancestor без sticky bit, чужой
owner, special bits/final mode не равный `0700`, final symlink и symlink под
writable parent отклоняются. Один стандартный sticky shared boundary (`/tmp` и
эквиваленты) допустим, но вложенный shared-writable boundary — нет.

Каждый ещё не разрешённый компонент открывается относительно проверенного parent
с `O_DIRECTORY|O_NOFOLLOW`; missing path не проходит через weak
`resolve(strict=False)`. Поэтому race, заменяющий отсутствующий child на symlink,
не может перенаправить store на старый credential/ledger directory. Обычный
non-sticky `0777/child-0700` намеренно неприемлем.

Модель рассчитана на локальный POSIX filesystem с корректными owner/mode,
`flock`/`fcntl`, directory `fsync` и SQLite locking. NFS, FUSE с ослабленной
семантикой, ACL/MAC rules, дающие третьей стороне запись вопреки mode bits, и
компрометация root/current OS user находятся вне гарантии. Для таких окружений
нужен отдельный private local state volume и операционная проверка ACL/mount.

## Credentials и account identity

- `.env` из cwd никогда не загружается.
- Login/password/phone/proxy вводятся только separate `kwork-mcp-bootstrap` через
  настоящий TTY/getpass; server не принимает их ни через argv, ни через env.
- Runtime-loaded token, full proxy URL и raw/decoded/yarl-canonical proxy URL,
  authority/host, userinfo, username/password fragments динамически
  регистрируются для redaction в логах и external MCP payload. Percent escapes
  canonicalized по hex case при сопоставлении. Все exact совпадения ищутся в
  исходной строке, после чего overlapping/touching интервалы объединяются до
  общего authority parser, который отделяет userinfo по последнему `@`.
- Ошибки MCP input validation не отражают переданные значения: строгая проверка
  advertised JSON Schema выполняется sanitizing middleware до FastMCP function
  validation.
- Credential record содержит подтверждённые `user_id`/`username`, token и optional
  proxy URL. Его dataclass repr скрывает credential fields.
- Token и lock открываются с `O_NOFOLLOW`, проверкой regular file, owner и mode.
- Запись record использует private tempfile, `fsync`, atomic `os.replace` и directory
  `fsync` под межпроцессным `flock`.
- До replace сбой сохраняет старый record побайтно; после replace сбой durability
  классифицируется `credential_update_unknown`, потому что новый record уже может
  быть видим.
- Отвергнутый cached token не удаляется до verified replacement и не проверяется
  повторно в том же process; normal server не выполняет fresh password login.
- Первый подтверждённый account ID неизменяем до завершения server process;
  неожиданный identity switch при relogin завершается `account_mismatch`.

Bootstrap сначала берёт account writer lock, затем credential flock. Он не может
ротировать token/proxy посреди multi-step write; concurrent bootstrap получает
typed `write_in_progress`. Legacy `~/.kwork_token` читается только после явного
согласия и проверок regular-file/current-owner/exact-0600/no-symlink/inode
stability/size/UTF-8/single-line. Normal startup его никогда не импортирует.
Закрытие bootstrap client имеет абсолютный пятисекундный deadline и bounded
cancel-drain. После deadline account/token locks освобождаются; если store уже
успешно или потенциально committed, наружу возвращается
`credential_update_unknown`. Это же правило действует, если cancellation впервые
приходит при освобождении token/account lock после commit; повторная cancellation
не может заменить уже активную typed ambiguity. Обычный release exception имеет
тот же низкий precedence: cleanup ограничен абсолютным deadline, а его secondary
ошибка логируется без текста exception и не заменяет body error. Если body
успешен, но release после credential commit завершился ошибкой, bootstrap
возвращает безопасный `credential_update_unknown`.

Credential files защищены permissions, но не application-level encryption.
Угроза компрометации текущего OS user не устраняется. Backups также должны
считаться секретными.

## Write safety

Write разрешён только после свежей проверки ожидаемого account ID. Prepare выполняет
action-specific read preflight и сохраняет canonical exact payload. Commit требует
совпадения `write_id`, SHA-256 `payload_hash` и confirmation token до TTL.

SQLite unique constraint обеспечивает shared idempotency по account scope;
atomic lease вместе с удерживаемым process-level account `flock` обеспечивает
one-writer. Сетевой write выполняется без автоматического retry.
Параллельные prepare с одинаковым клиентским request делят запись, даже если их
динамические preflight observations различались; другой request с тем же key
остаётся `idempotency_conflict`.
Confirmation token не хранится открытым текстом: он детерминированно выводится
через server-side HMAC из `write_id`, scope, exact payload hash и expiry. Поэтому
повтор того же prepare после потерянного ответа возвращает тот же token, но только
пока запись остаётся `prepared`.
После durable marker timeout, proxy disconnect, 5xx, пустой/non-JSON submit
response, отсутствие подтверждённого offer ID или потеря lease дают
`submission_unknown`. Ошибка admission/rate/auth до marker освобождает claim и
сохраняет возможность commit с тем же confirmation.

Claim и remote boundary разделены durable marker. Cancellation во время identity
check, read-only preflight или ожидания exclusive client возвращает claim в
`prepared`; cancellation после marker немедленно фиксирует
`submission_unknown`. Marker записывается shielded непосредственно перед первой
side-effect/write-flow операцией, после auth/rate admission. Финальная запись
success/failure также завершается shielded, после чего cancellation обязательно
пробрасывается вызывающей стороне. Если process
погиб, истёкший lease без marker восстанавливается в `prepared`/`expired`, а с
marker — только в `submission_unknown`.

`submission_unknown` означает: **не повторять commit и не создавать новый
idempotency key для того же действия**. Используйте `reconcile_write`; если
read-back недостаточен для однозначного ответа, оператор должен проверить Kwork
вручную. До разрешения такого состояния durable account barrier блокирует все
новые writes. Терминальное отсутствие требует нескольких полных отрицательных
read-back наблюдений через настраиваемый interval.
Неизвестные или новые upstream states никогда не считаются отрицательным
доказательством: для order approval закреплены `1` («в работе») и `4` («на
проверке»); arbitration/cancelled/completed/payment-required и любой новый status
оставляют запись неоднозначной.
Malformed/falsy collections и неполная либо дробная pagination на read-back
маршрутах завершаются `contract_drift` и не засчитываются как отсутствие.

## Error taxonomy

Все ошибки имеют безопасное сообщение, correlation ID и признаки `retryable`,
`safe_to_retry`, `reconciliation_required`. `safe_to_retry` относится к текущему
tool-вызову; remote write после начала отправки безопасным retry не считается.

| Код | Значение / действие |
|---|---|
| `auth_required`, `auth_expired` | Запустить/повторить TTY bootstrap; normal server не логинится сам |
| `auth_in_progress` | Другой process держит account auth/store lock; повторить после retry hint |
| `account_binding_required` | Задать expected account для writes |
| `account_mismatch` | Остановить write; проверить token/account |
| `captcha` | Пройти captcha вне MCP и обновить авторизацию |
| `permission`, `ip_blocked`, `csrf` | Проверить права, IP/proxy или web session |
| `rate_limit`, `circuit_open` | Соблюдать `retry_after_seconds`; retry только если `safe_to_retry` |
| `proxy`, `timeout`, `upstream_unavailable` | Transport failure; для started write требуется reconcile |
| `closed_project` | Проект больше не принимает оффер |
| `duplicate` | Side effect уже существует/операция повторна |
| `insufficient_connects` | Не отправлять оффер до пополнения connects |
| `contract_drift` | Закреплённый API/schema нарушен; обновлять gateway осознанно |
| `credential_update_unknown` | Replace мог состояться; не считать старый record сохранённым, проверить bootstrap/store |
| `ambiguous_write` | Не retry; выполнить reconciliation/read-back |
| `idempotency_conflict` | Key уже связан с другим payload |
| `preparation_expired`, `invalid_confirmation` | Подготовить заново после проверки intent |
| `write_in_progress` | Другой process владеет lease; читать status |
| `write_disabled` | Writes не включены явно |
| `not_found`, `validation` | Исправить object ID/input |
| `internal` | Использовать correlation ID; внешние детали намеренно скрыты |

## Rate limiting и circuit breaker

Account и route token buckets хранятся в shared SQLite. Это ограничивает совокупную
нагрузку нескольких MCP процессов, а не только один event loop. Circuit state также
общий: повторные transport/upstream failures временно блокируют route. SQLite
transactions короткие; locks не удерживаются во время network I/O или sleep.
Shared half-open probe lease исключает одновременный восстановительный burst.
Bootstrap auth/get-me calls проходят через те же account/route buckets и circuit
records (`signIn`, `actor`), хотя выполняются вне MCP transport.
Общая policy (bucket capacities/rates, timeout, circuit и write/reconciliation
параметры) закреплена SHA-256 fingerprint в state DB. Процесс с другим fingerprint
не может ослабить limiter или изменить ledger semantics. Remote `Retry-After`
проверяется на конечность и ограничивается; он не может навсегда отравить circuit
или приостановить event loop.

## Upstream contract

Runtime закреплён на `kwork==0.2.0`. При старте сверяются distribution version,
метод-сигнатуры и наличие generic route methods. Gateway использует фактические
контракты:

- paginated orders — `worker_orders(filter="all", page=...)`, а не
  `get_worker_orders(page=...)`;
- approval не принимает несуществующий `comment`;
- offer submission не считается успешной без подтверждённого ID/read-back.

Web-flow дополнительно разрешает только HTTPS host `kwork.ru` или его настоящие
поддомены. Redirects обрабатываются вручную: post-login запрос не переходит на
другой origin, а любой redirect после mutating POST отвергается, поскольку первый
запрос уже мог сработать. Каждый FAQ/draft/template prerequisite обязан вернуть
2xx до финального create; structured web errors сохраняют HTTP status/payload для
типизации captcha/IP/CSRF/permission без публикации внешнего текста.

Любой drift должен завершаться `contract_drift`, а не молчаливой потерей поля или
ложным успехом.

## Reporting

Не прикладывайте к issue token files, `coordination.sqlite3`, environment dump,
proxy URL, raw HTTP headers/cookies или ignored `docs/api_responses.md`. Для
диагностики достаточно версии приложения, error code, correlation ID и
санитизированного описания сценария.
