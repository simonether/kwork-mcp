# Конфигурация

Normal MCP читает только allowlisted safe policy/identity settings с префиксом
`KWORK_`. Автозагрузка `.env` отключена: текущая рабочая директория не считается
доверенной configuration boundary. Secret-bearing environment не является
поддерживаемой конфигурацией сервера.

## Авторизация и account binding

| Переменная | Default | Назначение |
|---|---:|---|
| `KWORK_EXPECTED_USER_ID` | нет | Обязательный positive ID для normal startup и writes |
| `KWORK_EXPECTED_USERNAME` | пусто | Дополнительная проверка username; ведущий `@` нормализуется |
| `KWORK_ENABLE_WRITES` | `false` | Явно включает safe write protocol |
| `KWORK_PERSIST_TOKEN` | `true` | Обязательный account-bound steady-state store |

До первого запуска выполните отдельную CLI в настоящем TTY:

```bash
KWORK_EXPECTED_USER_ID=123456 kwork-mcp-bootstrap
```

Login, password, optional last-four phone digits и optional proxy URL считываются
через `getpass`, никогда не принимаются в argv и не выводятся. Bootstrap выполняет
только authentication + `get_me`, проверяет exact numeric ID и затем сохраняет
record. Normal `kwork-mcp` требует `EXPECTED_USER_ID + PERSIST_TOKEN=true` и
проверяет сохранённый token через `get_me`; если record отсутствует, возвращается
`auth_required`, если token отвергнут — `auth_expired`. Интерактивного fallback и
циклического fresh login нет.

Normal entrypoint намеренно отклоняет непустые `KWORK_LOGIN`, `KWORK_PASSWORD`,
`KWORK_TOKEN`, `KWORK_PHONE_LAST`, `KWORK_PROXY_URL`. Эти имена нельзя добавлять в
Codex/Claude MCP configuration, даже если host помечает их как secrets.

## Transport и state

| Переменная | Default | Ограничения |
|---|---:|---|
| `KWORK_TIMEOUT` | `30` | 1–120 секунд |
| `KWORK_STATE_DIR` | XDG/`~/.local/state/kwork-mcp` | Только абсолютный путь |
| `KWORK_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

`KWORK_TOKEN_FILE` удалён и намеренно вызывает ошибку конфигурации. Один общий
безымянный файл позволял случайно использовать другой аккаунт. Bootstrap может
явно предложить импорт fixed path `~/.kwork_token`, но только после проверки
owner/mode/regular-file/no-symlink и обязательного `get_me`. Server startup
legacy-файл не читает и автоматически не импортирует. После validated import
legacy source сохраняется; удалите его вручную только после проверки нового flow.

State directory должен принадлежать текущему OS user и иметь exact mode `0700`.
Проверяется вся physical ancestor chain: допустимы только root/current-owner
каталоги без group/other write. Один sticky shared temp boundary разрешён, но
обычный `0777` parent, вложенный shared boundary, чужой owner, final symlink и
special bits отклоняются. Trusted non-final aliases раскрываются по одному hop с
повторной проверкой, а missing components открываются через FD-relative
`O_NOFOLLOW`, поэтому alias target/race не может скрыть writable ancestor.
`coordination.sqlite3`, credential/lock files должны иметь `0600`. Account record
содержит verified token и optional proxy URL.
Legacy record без proxy означает direct connection. Чтобы добавить, заменить или
удалить proxy, остановите процессы account/state, повторите bootstrap и
перезапустите MCP; уже открытый client record не перечитывает. Не размещайте общий
state на NFS или другом filesystem без надёжных POSIX locks/SQLite semantics.
Production runtime использует `fcntl`/`flock` и поддерживает Linux/macOS, но не
Windows. ACL/MAC/mount policy не должны давать другим principals write вопреки
mode bits; при сомнении используйте отдельный local private volume.

## Rate limits и resilience

| Переменная | Default | Ограничения/смысл |
|---|---:|---|
| `KWORK_RPS_LIMIT` | `2.0` | Account token refill, >0–100 |
| `KWORK_BURST_LIMIT` | `4` | Account capacity, 1–100 |
| `KWORK_ROUTE_RPS_LIMIT` | `1.0` | Route token refill, >0–100 |
| `KWORK_ROUTE_BURST_LIMIT` | `2` | Route capacity, 1–100 |
| `KWORK_RATE_WAIT_TIMEOUT` | `20` | Максимальное ожидание token, 0.1–120 s |
| `KWORK_AUTH_LOCK_TIMEOUT` | `90` | Ожидание межпроцессной auth/store lock, 5–300 s |
| `KWORK_READ_ATTEMPTS` | `3` | Только read attempts, 1–5 |
| `KWORK_RETRY_BACKOFF_BASE` | `0.5` | Начальный backoff, 0–30 s |
| `KWORK_RETRY_BACKOFF_MAX` | `8` | Максимальный backoff, 0–120 s |
| `KWORK_CIRCUIT_FAILURE_THRESHOLD` | `3` | Failures до open, 1–20 |
| `KWORK_CIRCUIT_OPEN_SECONDS` | `30` | Базовое open window, 1–900 s |

`RETRY_BACKOFF_MAX` не может быть меньше base. Не повышайте лимиты без фактического
подтверждения допустимой нагрузки Kwork. Все процессы, которые должны делить
лимит, обязаны указывать один state directory. Remote `Retry-After` не увеличивает
sleep сверх `RETRY_BACKOFF_MAX`; shared circuit никогда не открывается более чем
на 900 секунд одним сигналом.

## Write protocol

| Переменная | Default | Ограничения/смысл |
|---|---:|---|
| `KWORK_PREPARATION_TTL_SECONDS` | `600` | 30–3600 s |
| `KWORK_WRITE_LEASE_SECONDS` | `120` | One-writer lease, 10–900 s |
| `KWORK_RECONCILIATION_MIN_AGE_SECONDS` | `15` | Первое read-back не раньше этого возраста, 1–300 s |
| `KWORK_RECONCILIATION_ABSENCE_CONFIRMATIONS` | `2` | Полных наблюдений отсутствия, 2–5 |
| `KWORK_RECONCILIATION_ABSENCE_INTERVAL_SECONDS` | `15` | Интервал между отрицательными наблюдениями, 1–300 s |
| `KWORK_STATE_RETENTION_DAYS` | `30` | Retention терминальных ledger-записей, 1–3650 дней |

Слишком короткий lease может превратить медленный, но успешный write в
`submission_unknown`; это безопаснее дублирования, но требует reconcile. Увеличивайте
его только с учётом максимального upstream timeout. `reconciled_absent` возникает
лишь после настроенного числа полных read-back наблюдений, разделённых visibility
interval; до этого запись остаётся `submission_unknown` и блокирует новые writes
для данного аккаунта.

Cancellation до durable remote marker освобождает claim в `prepared`; после marker
немедленно сохраняется `submission_unknown`. При crash/stale lease действует то же
разделение. Exact replay `prepare_write` с тем же idempotency key/request возвращает
тот же HMAC-derived confirmation token, пока запись остаётся `prepared`.

При первом открытии state DB gateway закрепляет fingerprint общей policy:
`RPS_LIMIT`, `BURST_LIMIT`, route limits, `TIMEOUT`, circuit settings и все
write/reconciliation/retention settings. Все процессы с одним
`KWORK_STATE_DIR` должны использовать одинаковые значения этих полей. Их
осознанное изменение требует остановить все процессы и начать с нового пустого
state directory либо завершить/сохранить нужные ledger records и выполнить
контролируемую миграцию; молчаливое смешивание policy намеренно запрещено.

## Передача secrets

Secret bootstrap inputs не должны присутствовать в Codex/Claude host config.
Некоторые hosts сериализуют MCP env map в собственный `--mcp-config`, из-за чего
значения оказываются в process argv множества descendants. MCP-процесс не может
исправить argv родительского host, поэтому normal entrypoint fail-closed запрещает
secret env полностью.

Не используйте:

- `.env` в repository/cwd;
- token/login/password/phone/proxy в `codex mcp add --env`, `config.toml`,
  Claude config, shell command, process argv или service manifest;
- один state directory для разных OS users.

Bootstrap prompts идут в stderr, success — один allowlisted JSON object в stdout.
Логи MCP идут только в stderr. Runtime token, full proxy URL и proxy userinfo
динамически добавляются в redaction set для логов и внешних payload. Credential
record защищён mode `0600`, но не application-level encryption: используйте
шифрование диска, private backups и отдельную OS account.

Если filesystem error произошёл после atomic `os.replace`, но до подтверждённого
directory `fsync`, результат возвращается как `credential_update_unknown`: новый
record уже может быть видим. Не считайте это гарантированным сохранением старого
token и сначала повторно проверьте store/bootstrap.
