<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.png">
    <img src="assets/banner-light.png" alt="kwork-mcp: MCP-сервер для Kwork. Агент готовит отклики и ответы, отправляете вы." width="100%">
  </picture>
</p>

[![CI](https://github.com/simonether/kwork-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/simonether/kwork-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/kwork-mcp?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/kwork-mcp/)
[![Python](https://img.shields.io/badge/python-3.12%E2%80%933.14-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP Badge](https://lobehub.com/badge/mcp/simonether-kwork-mcp)](https://lobehub.com/mcp/simonether-kwork-mcp)

`kwork-mcp` подключает ваш аккаунт [Kwork](https://kwork.ru) к ИИ-агенту: Claude
Code, Claude Desktop, Codex, Cursor и другим MCP-клиентам. Агент ищет проекты на
бирже, читает диалоги, заказы и офферы, готовит отклики. По умолчанию агент
отправляет что-то на Kwork только после вашего «да» на точный текст.

Сайт проекта: [simonether.github.io/kwork-mcp](https://simonether.github.io/kwork-mcp/).

## Что можно попросить у агента

- «Найди свежие проекты про Telegram-ботов с бюджетом от 20 000 ₽ и скажи, какие
  мне подходят».
- «Покажи диалоги с непрочитанными сообщениями и предложи ответы».
- «Что с моими заказами? Где что-то ждёт моих действий?»
- «Сколько у меня коннектов и какие мои отклики ещё висят?»
- «Подготовь отклик на проект 3257238: 15 000 ₽, 5 дней» — агент покажет точный
  текст и цену и отправит только после вашего «да».

## Быстрый старт

Нужны macOS, Linux или Windows 10/11, Python 3.12–3.14 и [uv](https://docs.astral.sh/uv/).
На Windows uv ставится в PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Если агент работает внутри WSL, для kwork-mcp это Linux со своим домашним каталогом:
выполните `login` там же, где запускается агент.

### 1. Войдите в Kwork (один раз)

В обычном терминале (на Windows в PowerShell или Windows Terminal):

```bash
uvx kwork-mcp@1.6.0 login
```

Команда скрыто спросит логин и пароль Kwork, а также, по желанию, последние 4 цифры
телефона и прокси, и покажет найденный аккаунт:

```text
Найден аккаунт Kwork: your_name (user_id=123456). Привязать его? [y/N]: да
```

После подтверждения токен сохраняется в защищённое хранилище на вашем компьютере:
`~/.local/state/kwork-mcp`, на Windows `%USERPROFILE%\.local\state\kwork-mcp`. Логин и
пароль нигде не сохраняются.

Если на компьютере есть Claude Desktop или Cursor, команда спросит, подключить ли к ним
kwork-mcp, и сама допишет сервер в их конфиг. Остальные серверы в конфиге не меняются,
прежняя версия файла остаётся рядом как `.bak`. После этого перезапустите клиент. Для
Claude Code и Codex команда напечатает готовые команды подключения.

### 2. Подключите агента

**Claude Code:**

```bash
claude mcp add kwork --scope user -- uvx kwork-mcp@1.6.0
```

**Codex:**

```bash
codex mcp add kwork -- uvx kwork-mcp@1.6.0
```

**Cursor:** [![Add to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/install-mcp?name=kwork&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJrd29yay1tY3BAMS42LjAiXX0%3D)

**Claude Desktop:** `login` подключает его сам, после этого перезапустите приложение.

<details>
<summary><b>Подключить вручную: Claude Desktop и другие клиенты</b></summary>

Claude Desktop: Settings → Developer → Edit Config, файл `claude_desktop_config.json`.
Cursor без кнопки: `~/.cursor/mcp.json` (на Windows `%USERPROFILE%\.cursor\mcp.json`)
или `.cursor/mcp.json` проекта.

```json
{
  "mcpServers": {
    "kwork": {
      "command": "uvx",
      "args": ["kwork-mcp@1.6.0"]
    }
  }
}
```

Claude Desktop не видит `uvx` из терминала, поэтому возьмите блок из вывода `login`:
в нём уже указан полный путь к `uvx`.
</details>

Сервер сам найдёт аккаунт, с которым вы вошли. Если вы входили в несколько
аккаунтов, укажите нужный: `-e KWORK_EXPECTED_USER_ID=123456` или блок `"env"` в
JSON.

Логин, пароль, токен и прокси в конфиг клиента не добавляйте: сервер с ними
откажется запускаться.

### 3. Проверьте

Перезапустите клиент и попросите агента: «проверь статус аккаунта Kwork». Он
вызовет `account_status` и покажет ваш `user_id` и имя.

Без клиента то же видно в терминале: `uvx kwork-mcp@1.6.0 status` покажет, с каким
аккаунтом и сайтом запустится сервер, не обращаясь к Kwork. Сменить аккаунт:
`uvx kwork-mcp@1.6.0 logout`, затем снова `login`.

## Отправка откликов и сообщений

Отправку на Kwork (отклики, сообщения, удаление офферов, сдача заказа, запуск и
пауза кворков) задаёт переменная `KWORK_WRITES`:

| `KWORK_WRITES` | Что происходит |
|---|---|
| `confirm` (по умолчанию) | Перед каждой отправкой агент показывает точный текст, цену и получателя и ждёт вашего «да» |
| `auto` | Агент отправляет сам, когда это нужно для вашей задачи, без отдельного подтверждения |
| `off` | Только чтение: инструменты отправки скрыты, агент их не видит |

В режиме `confirm` сервер в ответе `prepare_write` и в своих инструкциях
указывает агенту показать вам запрос и отправлять только после вашего «да». Сам
проверить это «да» сервер не может. Вторую кнопку даёт клиент: Claude Code и
Claude Desktop по умолчанию спрашивают разрешение перед инструментом отправки, а в
Codex то же включает `default_tools_approval_mode = "writes"` в настройках
сервера `[mcp_servers.kwork]`.

Режим выбирается в конфиге клиента, например для Claude Code:

```bash
claude mcp remove kwork --scope user
claude mcp add kwork --scope user -e KWORK_WRITES=auto -- uvx kwork-mcp@1.6.0
```

В режиме `auto` от чужих команд в текстах проектов и сообщений защищает только
поведение агента: сервер помечает эти тексты как внешние данные, но проверить,
что агент их не послушал, не может.

Каждая отправка идёт в два шага: сначала агент готовит точный запрос, затем
выполняет его. Сервер помнит каждую запись и никогда не повторяет её сам.

Если связь оборвалась в момент отправки, результат становится «неизвестным», и
агент сверяет его с Kwork, прежде чем делать что-то ещё. Пока такая запись не
сверена, новые отправки для аккаунта заблокированы, чтобы не создать дубль.

## kwork.com

По умолчанию сервер работает с kwork.ru. Для kwork.com добавьте в конфиг клиента
`KWORK_SITE=com`. Например, для Claude Code вторым сервером рядом с kwork.ru:

```bash
claude mcp add kwork-com --scope user -e KWORK_SITE=com -- uvx kwork-mcp@1.6.0
```

Аккаунт и токен у kwork.ru и kwork.com общие, поэтому заново входить не нужно.
Биржи проектов на kwork.com нет: поиск проектов, избранные категории и отклик на
проект там отвечают `site_unsupported`. Диалоги, заказы и кворки работают как
обычно, но заказы у каждого сайта свои. Запись, подготовленную для одного сайта,
отправляет и сверяет только сервер того же сайта.

## Если что-то не работает

| Что видите | Что делать |
|---|---|
| `auth_required` или `auth_expired` | Входа нет или он истёк: выполните `uvx kwork-mcp@1.6.0 login` и перезапустите клиент |
| `account_binding_required` | Вы входили в несколько аккаунтов: укажите нужный в `KWORK_EXPECTED_USER_ID` или удалите лишний вход командой `uvx kwork-mcp@1.6.0 logout <user_id>` |
| Непонятно, какой аккаунт и сайт использует сервер | `uvx kwork-mcp@1.6.0 status` покажет это без запросов к Kwork |
| Сервер не стартует, «некорректная конфигурация: …» | Проверьте названные переменные `KWORK_*` в конфиге клиента |
| «KWORK_ENABLE_WRITES удалена в 1.5.0» | Замените её на `KWORK_WRITES=off`, `confirm` или `auto` |
| `write_disabled` | Сервер запущен с `KWORK_WRITES=off`, доступно только чтение |
| `captcha` | Войдите в Kwork в браузере, пройдите капчу, затем повторите `login` |
| Claude Desktop не видит сервер | Возьмите блок для Claude Desktop из вывода `login` (в нём полный путь к `uvx`) или укажите путь из `which uvx` (на Windows `where uvx`), затем перезапустите приложение |
| `ambiguous_write` с `related_write_id` | Отправка с неизвестным результатом блокирует новые. Попросите агента выполнить `reconcile_write` для этого ID |
| Сверка долго не сходится | Проверьте операцию на сайте Kwork, указанном в поле `site` у `pending-writes`, и зафиксируйте исход вручную (команды ниже) |

Ручная фиксация исхода запускается с теми же `KWORK_*` переменными, что у сервера:

```bash
uvx kwork-mcp@1.6.0 pending-writes
uvx kwork-mcp@1.6.0 resolve-write <write_id> succeeded   # операция на Kwork прошла
uvx kwork-mcp@1.6.0 resolve-write <write_id> absent      # операции на Kwork нет
```

## Инструменты

**Чтение:**

| Инструмент | Что делает |
|---|---|
| `account_status` | Какой аккаунт подключён, включена ли запись, есть ли несверенные отправки |
| `get_connects` | Баланс коннектов |
| `discover_projects`, `get_project` | Проекты биржи: избранные категории, вся биржа или выбранные категории, фильтры по цене и откликам |
| `get_exchange_info` | Сводка по бирже |
| `list_my_offers`, `get_offer` | Ваши отклики |
| `list_worker_orders`, `get_order_details` | Ваши заказы как продавца |
| `list_dialogs`, `get_dialog` | Диалоги и сообщения |
| `list_my_kworks`, `get_kwork_details` | Ваши кворки |
| `get_user_info`, `search_users` | Профили пользователей |
| `list_categories`, `list_favorite_categories` | Категории |
| `list_notifications` | Уведомления |

**Запись:** `prepare_write` → `commit_write`, а также `get_write_status` и
`reconcile_write`. Поддерживаются отклик на проект, удаление отклика, отправка,
правка и удаление сообщения, отметка диалога прочитанным, сдача заказа на проверку
и запуск или пауза кворка.

## Безопасность

- Логин, пароль и прокси вводятся только в `kwork-mcp login` через скрытый ввод
  и не попадают в конфиг клиента.
- Сервер работает только с аккаунтом, который вы подтвердили при входе (если
  аккаунтов несколько, с указанным в `KWORK_EXPECTED_USER_ID`), и проверяет его
  перед каждой записью.
- Тексты проектов, профилей и сообщений помечаются как внешние данные, а не
  инструкции для агента.
- На macOS и Linux токен лежит в файлах с правами `0600`. Приложение его не
  шифрует, поэтому используйте шифрование диска.
- На Windows каталог с токеном открыт только вашей учётной записи и SYSTEM, а сам
  токен зашифрован DPAPI: прочитать его может только ваш пользователь Windows.
- Лимиты запросов к Kwork общие для всех процессов одного аккаунта.

Подробно: [модель безопасности](docs/security.md), [настройки](docs/configuration.md),
[архитектура](docs/architecture.md), [переход с 0.2.x](docs/migration-1.0.md).

## Разработка

```bash
git clone https://github.com/simonether/kwork-mcp.git
cd kwork-mcp
uv sync --locked --dev
uv run ruff check . && uv run ruff format --check .
uv run mypy && uv run mypy --platform win32
uv run pytest tests/ -q --cov=kwork_mcp
```

Для запуска из исходников в конфиге клиента используйте
`uv --directory /path/to/kwork-mcp run kwork-mcp` вместо `uvx`. Правила для
изменений и устройство кода описаны в [AGENTS.md](AGENTS.md).

## Лицензия

[MIT](LICENSE)

<!-- mcp-name: io.github.simonether/kwork-mcp -->
