# Миграция 0.2.x → 1.0.0

1.0.0 — намеренный SemVer major: MCP tool names, input/output contract, write
семантика, configuration и state layout несовместимы с 0.2.x.

## До обновления

1. Остановите все процессы 0.2.x.
2. Запишите фактический Kwork `user_id` через доверенный UI/API.
3. Сохраните backup старого state, если он нужен; не публикуйте его.
4. Для release candidate обновите Codex command до exact
   `kwork-mcp==1.0.0rc1`; после stable release замените его на `1.0.0`.

## Конфигурация

| 0.2.x | 1.0.0 |
|---|---|
| `.env` мог загружаться из cwd | `.env` отключён; normal server принимает только safe steady-state env |
| Login/password/token в MCP config | Запрещены; отдельный TTY `kwork-mcp-bootstrap` |
| `KWORK_TOKEN_FILE=~/.kwork_token` | Явный validated import в account-scoped store |
| Token мог использоваться без identity binding | Каждый candidate проверяется через `get_me` |
| Writes были включены при наличии credentials | Нужны `KWORK_ENABLE_WRITES=true` и `KWORK_EXPECTED_USER_ID` |
| In-process rate limiter | Shared SQLite account/route coordination |

Удалите `KWORK_TOKEN_FILE`, `KWORK_LOGIN`, `KWORK_PASSWORD`, `KWORK_TOKEN`,
`KWORK_PHONE_LAST` и `KWORK_PROXY_URL` из Codex/Claude MCP configuration до
запуска 1.0. Normal entrypoint отклоняет их fail-closed; это не только cleanup, но
и защита от hosts, которые помещают env map в process argv.

Выполните bootstrap вне MCP host:

```bash
KWORK_EXPECTED_USER_ID=123456 kwork-mcp-bootstrap
```

Если fixed legacy `~/.kwork_token` существует, CLI предложит импорт. Согласие не
означает доверие: файл должен быть regular, принадлежать текущему user, иметь exact
`0600` и не быть symlink; затем token проверяется `get_me` и exact expected ID.
Обычный server legacy source никогда не читает. При decline файл даже не
открывается; после success он сохраняется для отдельного ручного удаления. Если
legacy token отвергнут, повторите bootstrap с hidden password login.

Steady-state MCP config содержит только `KWORK_EXPECTED_USER_ID`,
`KWORK_PERSIST_TOKEN=true`, optional safe policy keys и initially
`KWORK_ENABLE_WRITES=false`. После `account_status` можно осознанно включить
writes. Proxy также вводится bootstrap-команде и хранится в private account record,
а не в host config.

## Tools

Read tools переименованы/сведены к стабильному набору. Основные замены:

| 0.2.x | 1.0.0 |
|---|---|
| `get_me` | `account_status` |
| `list_projects` / `search_projects` | `discover_projects(mode=..., query=...)` |
| `get_favorite_categories` | `list_favorite_categories` |
| `submit_offer`, `delete_offer` | `prepare_write` → `commit_write` |
| `send_message`, `edit_message`, `delete_message` | `prepare_write` → `commit_write` |
| `mark_dialog_read` | `prepare_write` → `commit_write` |
| `send_order_for_approval` | action `submit_order_approval` через safe write-flow |
| `start_kwork`, `pause_kwork` | action `set_kwork_state` через safe write-flow |

Strings больше не являются контрактом. Читайте `structuredContent` envelope;
`content[0]` — краткое резюме, `content[1]` — JSON-копия для совместимых клиентов.
Пустой список теперь `known_empty`, а сбой — `unknown_error` с `isError=true`.

## Discovery

Нельзя полагаться на неявные favorite categories. Укажите:

- `mode="favorites"`;
- `mode="all"`;
- `mode="category_ids"` и непустой `category_ids`.

Не конструируйте cursor самостоятельно. Сохраняйте `next_cursor` и используйте его
только с теми же filters/query. Для checkpoints можно хранить `high_watermark`, но
это не отдельный delta API.

## Writes

Любой старый прямой write workflow необходимо заменить:

1. сгенерировать стабильный idempotency key на пользовательский intent;
2. `prepare_write`;
3. показать/проверить exact payload и account;
4. `commit_write` с тремя точными confirmation fields;
5. при `submission_unknown` — только `reconcile_write`.

Не интерпретируйте timeout как failure и не создавайте второй оффер/сообщение.
`get_write_status` безопасно восстанавливает durable state после перезапуска Codex.
Если ответ первого `prepare_write` потерян, exact replay с тем же
idempotency key/request возвращает ту же `prepared` запись и тот же confirmation
token. Не создавайте новый key. После claim/terminal state token повторно не
выдаётся.

## State и эксплуатация

Создайте локальный абсолютный state directory на надёжном filesystem. Он должен
быть общим для процессов одного account, иначе межпроцессные rate limits и
idempotency не будут общими. Не делите его между OS users или разными Kwork
accounts. Вся ancestor chain должна быть root/current-owned и не writable для
group/other; допустим один sticky temp boundary. Обычный `0777` parent даже с
private `0700` child теперь fail-closed отклоняется.

После первого запуска проверьте:

1. `account_status.user_id == KWORK_EXPECTED_USER_ID`;
2. `binding_state == "bound"` и `write_ready` соответствует intent;
3. `meta.upstream_contract == "kwork==0.2.0"`;
4. read-only discovery возвращает ожидаемый account scope;
5. write-flow сначала испытан на fake/test environment — не на live side effect.

При `auth_required` снова выполните bootstrap: account record отсутствует. При
`auth_expired` token был отвергнут и normal server не пытается password login.
`credential_update_unknown` означает сбой после commit point atomic replace:
новый record уже может быть видим, поэтому сначала проверьте store повторным
bootstrap, а не делайте вывод, что старый token гарантированно сохранился.
