# kwork-mcp: План улучшений

> Дата: 2026-03-15 | Обновлено: 2026-03-18 | Текущая версия: 0.2.0b1 | Оценка: 7.5/10 → 9/10

Документ содержит полный анализ текущего состояния проекта и конкретные рекомендации по улучшению — от архитектуры до отдельных строк кода. Основан на аудите всей кодовой базы, исследовании MCP спецификации (ноябрь 2025), FastMCP 3.0, лучших практик дизайна MCP-инструментов, и анализе 259+ endpoints pykwork.

---

## Содержание

- [1. Архитектура и дизайн инструментов](#1-архитектура-и-дизайн-инструментов)
- [2. Миграция на FastMCP 3.0](#2-миграция-на-fastmcp-30)
- [3. Новые инструменты](#3-новые-инструменты)
- [4. Качество кода](#4-качество-кода)
- [5. Тестирование](#5-тестирование)
- [6. Безопасность](#6-безопасность)
- [7. Надёжность и отказоустойчивость](#7-надёжность-и-отказоустойчивость)
- [8. Деплой и дистрибуция](#8-деплой-и-дистрибуция)
- [9. DX (Developer Experience)](#9-dx-developer-experience)
- [10. Приоритизация](#10-приоритизация)

---

## 1. Архитектура и дизайн инструментов

### 1.1. Outcome-oriented tools вместо endpoint-обёрток

**Проблема:** Текущие 25 инструментов — тонкие обёртки над pykwork-методами. Каждый инструмент = один API-вызов. Агенту приходится оркестрировать цепочки вызовов самому, тратя контекстное окно.

**Best practice (Anthropic, Philipp Schmid):** "MCP is a User Interface for AI agents." Инструменты должны решать задачи пользователя, а не зеркалить API. Один высокоуровневый инструмент лучше трёх атомарных.

**Рекомендуемые составные инструменты:**

| Составной инструмент | Что делает внутри | Заменяет |
|---|---|---|
| `respond_to_project(project_id, price, description, duration)` | `get_project` → проверка бюджета → `get_connects` → `submit_offer` | 3 вызова → 1 |
| `check_new_work()` | `list_projects` (избранные) + `list_notifications` + `list_worker_orders` (новые) | 3 вызова → 1 сводка |
| `complete_order(order_id)` | `get_order_details` → проверка статуса → `send_order_for_approval` | 2 вызова → 1 |
| `full_project_info(project_id)` | `get_project` + `get_exchange_info` → единый ответ | 2 вызова → 1 |

**Важно:** атомарные инструменты не удалять — они нужны для гибкости. Составные — дополнение, не замена.

### 1.2. Улучшение описаний инструментов

**Проблема:** Описания инструментов объясняют *что* делает инструмент, но не *когда* его использовать. LLM использует описания для выбора инструмента — каждое слово влияет на качество.

**Текущее:**
```python
"""Список проектов на бирже Kwork с фильтрацией."""
```

**Рекомендуемое:**
```python
"""Список проектов на бирже Kwork с фильтрацией.

Используйте для поиска новых проектов в категориях фрилансера.
Без фильтров возвращает проекты из избранных категорий пользователя.
Каждый проект содержит: ID, название, бюджет, количество предложений, процент найма.
Для подробностей конкретного проекта используйте get_project.
"""
```

**Паттерн для каждого описания:**
1. Что делает (1 строка)
2. Когда использовать (1-2 строки)
3. Что возвращает (1 строка)
4. Связь с другими инструментами (1 строка, если нужно)

### 1.3. Именование инструментов

**Текущее:** `list_my_kworks`, `get_kwork_details`, `list_worker_orders`

**Best practice:** `{service}_{action}_{resource}` — `slack_send_message`, `linear_list_issues`

**Рекомендуемое:** добавить префикс `kwork_` ко всем инструментам для предотвращения коллизий при использовании с другими MCP-серверами:

| Текущее | Рекомендуемое |
|---|---|
| `list_projects` | `kwork_list_projects` |
| `get_me` | `kwork_get_profile` |
| `send_message` | `kwork_send_message` |
| `submit_offer` | `kwork_submit_offer` |

### 1.4. Tool annotations

FastMCP 3.0 поддерживает аннотации поведения инструментов — клиент может использовать их для UI-решений (пропуск подтверждения для безопасных операций, предупреждение для деструктивных):

```python
from mcp.types import ToolAnnotations

@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=True,
))
async def list_projects(...) -> str: ...

@mcp.tool(annotations=ToolAnnotations(
    destructiveHint=True,
    openWorldHint=True,
))
async def delete_offer(...) -> str: ...
```

**Классификация текущих 25 инструментов:**

| Тип | Инструменты |
|---|---|
| `readOnlyHint=True` | `get_me`, `get_connects`, `get_user_info`, `list_projects`, `get_project`, `search_projects`, `get_exchange_info`, `list_my_offers`, `get_offer`, `list_worker_orders`, `get_order_details`, `list_dialogs`, `get_dialog`, `list_my_kworks`, `get_kwork_details`, `list_categories`, `get_favorite_categories`, `list_notifications` |
| `destructiveHint=True` | `delete_offer`, `pause_kwork` |
| Write (без destructive) | `submit_offer`, `send_message`, `mark_dialog_read`, `send_order_for_approval`, `start_kwork` |

### 1.5. Flatten arguments

**Проблема:** Некоторые параметры могут быть неочевидны для агента.

**Best practice:** Используйте `Literal` типы и ограничения вместо произвольных строк:

```python
# Вместо:
async def list_projects(category_id: int | None = None, ...) -> str:

# Лучше:
from typing import Literal

async def list_worker_orders(
    status: Literal["all", "active", "checking", "completed", "cancelled"] = "all",
    limit: int = 20,
    ctx: Context,
) -> str:
```

---

## 2. Миграция на FastMCP 3.0

### 2.1. Обоснование

FastMCP 3.0 GA — стабильный релиз с обратной совместимостью. `@mcp.tool()` работает как прежде. Новые возможности:

| Фича | Ценность для kwork-mcp |
|---|---|
| OpenTelemetry | Автоматическая трассировка каждого вызова инструмента — отладка продакшна |
| Granular Auth | Авторизация на уровне инструмента (read-only без пароля) |
| Component Versioning | Версионирование инструментов без breaking changes |
| Session State | `ctx.set_state()`/`ctx.get_state()` для кэширования между вызовами |
| Tool Annotations | Подсказки поведения для клиентов (readOnly, destructive) |
| Structured Outputs | Машиночитаемый JSON рядом с текстом |
| `--reload` | Авто-перезапуск при изменении кода |
| PingMiddleware | Keepalive для долгих сессий |
| In-memory testing | Тестирование без subprocess |

### 2.2. Structured Outputs (outputSchema)

**Текущее:** все инструменты возвращают форматированные строки. Агент не может программно обработать данные.

**Рекомендуемое:** возвращать Pydantic-модели или dataclass — FastMCP автоматически создаст и текстовое, и структурированное содержимое:

```python
from dataclasses import dataclass

@dataclass
class ProjectInfo:
    id: int
    title: str
    budget: int | None
    offers_count: int
    hiring_percent: float | None
    description: str

@mcp.tool()
async def get_project(project_id: int, ctx: Context) -> ProjectInfo:
    """Подробная информация о проекте на бирже Kwork по его ID."""
    ...
    return ProjectInfo(
        id=project_id,
        title=resp.get("name", "—"),
        budget=resp.get("price"),
        offers_count=resp.get("offers", 0),
        hiring_percent=resp.get("user_hired_percent"),
        description=resp.get("description", "—"),
    )
```

Клиент получит и читаемый текст, и JSON для программной обработки.

### 2.3. Session State для кэширования

```python
@mcp.tool()
async def get_me(ctx: Context) -> str:
    # Кэшируем профиль на время сессии
    cached = ctx.get_state("profile")
    if cached:
        return cached

    session = ctx.request_context.lifespan_context
    client = await session.ensure_client()
    await session.rate_limit()
    async with api_guard("get_me"):
        actor = await client.get_me()

    result = f"Имя: {actor.username}\n..."
    ctx.set_state("profile", result)
    return result
```

### 2.4. Миграция — шаги

1. Обновить зависимость: `mcp>=1.20` → `fastmcp>=3.0`
2. Заменить `from mcp.server.fastmcp import FastMCP` → `from fastmcp import FastMCP`
3. Добавить annotations к каждому инструменту
4. Постепенно перевести возвращаемые типы на dataclass/Pydantic
5. Добавить OTEL-конфигурацию
6. Тесты перевести на in-memory Client

---

## 3. Новые инструменты

### 3.1. Покрытие API

Из 259+ endpoints pykwork используется ~20 (8%). Критически недостающие сценарии:

### 3.2. Приоритет 1 — Управление заказами (покупатель)

Без этих инструментов нельзя полноценно работать как покупатель.

| Инструмент | pykwork метод | Описание |
|---|---|---|
| `kwork_list_buyer_orders` | `get_payer_orders()` | Список заказов покупателя |
| `kwork_order_kwork` | `order_kwork()` | Оформить заказ на кворк |
| `kwork_approve_order` | `approve_order()` | Принять выполненную работу |
| `kwork_cancel_order` | `cancel_order_by_payer()` / `cancel_order_by_worker()` | Отменить заказ |
| `kwork_request_revision` | `send_order_for_revision()` | Отправить на доработку |
| `kwork_send_requirements` | `send_order_requirements()` | Отправить ТЗ исполнителю |
| `kwork_get_order_files` | `get_order_files()` | Получить файлы заказа |

### 3.3. Приоритет 2 — Треки (чат заказа)

Основной канал коммуникации по заказам — не личные сообщения, а треки.

| Инструмент | pykwork метод | Описание |
|---|---|---|
| `kwork_list_tracks` | `get_tracks()` | Список треков (обсуждений) заказа |
| `kwork_send_track_message` | `track_message()` | Написать в трек заказа |
| `kwork_read_track` | `track_read()` | Прочитать трек |

### 3.4. Приоритет 3 — Поиск и каталог

| Инструмент | pykwork метод | Описание |
|---|---|---|
| `kwork_search` | `search()` | Общий поиск по Kwork (кворки, пользователи) |
| `kwork_search_catalog` | `search_kworks_catalog_query()` | Поиск по каталогу кворков |
| `kwork_get_user_kworks` | `user_kworks()` | Кворки конкретного пользователя |
| `kwork_get_user_reviews` | `user_reviews()` | Отзывы пользователя |

### 3.5. Приоритет 4 — Портфолио и отзывы

| Инструмент | pykwork метод | Описание |
|---|---|---|
| `kwork_list_portfolios` | `portfolio_list()` | Список портфолио |
| `kwork_create_portfolio` | `create_portfolio()` | Создать работу в портфолио |
| `kwork_get_kwork_reviews` | `get_kwork_reviews()` | Отзывы на конкретный кворк |

### 3.6. Приоритет 5 — Профиль и настройки

| Инструмент | pykwork метод | Описание |
|---|---|---|
| `kwork_set_taking_orders` | `set_taking_orders()` | Включить/выключить приём заказов |
| `kwork_set_weekends` | `set_available_at_weekends()` | Доступность в выходные |
| `kwork_upload_file` | `file_upload()` | Загрузить файл |

### 3.7. Приоритет 6 — Управление проектами покупателя

| Инструмент | pykwork метод | Описание |
|---|---|---|
| `kwork_list_my_wants` | `my_wants()` | Мои проекты (как заказчик) |
| `kwork_create_want` | Через web client | Создать проект на бирже |
| `kwork_stop_want` | `stop_want()` | Остановить проект |

### 3.8. Количество инструментов

Best practice: 5-15 инструментов на сервер. Сейчас 25, с новыми будет 40+.

**Решение:** разделить на 2-3 сервера по ролям:
- `kwork-seller-mcp` — инструменты продавца (текущие + треки)
- `kwork-buyer-mcp` — инструменты покупателя (заказы, ТЗ, приёмка)
- `kwork-mcp` — общие (профиль, поиск, каталог, уведомления)

Или использовать Dynamic Component Control из FastMCP 3.0:
```python
@mcp.tool()
async def set_role(role: Literal["seller", "buyer"], ctx: Context) -> str:
    if role == "seller":
        ctx.enable_components("seller_*")
        ctx.disable_components("buyer_*")
    ...
```

---

## 4. Качество кода

### 4.1. Общий utils-модуль

**Проблема:** Форматирование дат дублируется в 4 файлах (dialogs.py:11-14, projects.py:12-15, orders.py:23-29, notifications.py:38-46). Каждый с немного другой сигнатурой.

**Решение:** Создать `src/kwork_mcp/utils.py`:

```python
from datetime import UTC, datetime
from typing import Any


def format_timestamp(value: Any, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Форматировать unix timestamp или строковую дату."""
    if not value:
        return "—"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).strftime(fmt)
    return str(value)


def format_date(ts: int | None) -> str:
    """Форматировать unix timestamp в дату (без времени)."""
    return format_timestamp(ts, "%d.%m.%Y")


def truncate(text: str | None, limit: int = 200) -> str:
    """Обрезать текст до указанной длины."""
    if not text:
        return "—"
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def format_price(value: Any) -> str:
    """Форматировать цену в рублях."""
    if not value:
        return "не указана"
    return f"{value} руб."


def safe_get(data: dict, *keys: str, default: Any = "—") -> Any:
    """Безопасно достать значение из dict по нескольким возможным ключам."""
    for key in keys:
        val = data.get(key)
        if val is not None:
            return val
    return default
```

### 4.2. Нормализация ответов API

**Проблема:** Каждый инструмент парсит dict с множеством fallback-ключей. Например:
- `offers.py:43`: `offer.get("name") or offer.get("want_name") or offer.get("title") or "—"`
- `orders.py:52`: `order.get("price") or order.get("total_price") or "—"`
- `kworks.py:90`: `response.get(status_key) or response.get(f"{status_key}_kworks") or []`

Этот код хрупок и будет ломаться при изменениях API.

**Решение:** Создать `src/kwork_mcp/normalizers.py` — промежуточный слой между pykwork и инструментами:

```python
from dataclasses import dataclass
from typing import Any


def unwrap_response(data: dict[str, Any]) -> Any:
    """Извлечь данные из стандартной обёртки pykwork."""
    if isinstance(data, dict) and "response" in data:
        return data["response"]
    return data


@dataclass
class NormalizedOffer:
    id: int
    title: str
    status: str
    price: int | None
    description: str
    duration: int | None
    project_id: int | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NormalizedOffer":
        return cls(
            id=d.get("id", 0),
            title=d.get("name") or d.get("kwork_name") or d.get("title") or "—",
            status=str(d.get("status", "—")),
            price=d.get("price") or d.get("kwork_price"),
            description=d.get("description", "—"),
            duration=d.get("kwork_duration") or d.get("duration"),
            project_id=d.get("want_id") or d.get("project_id"),
        )
```

Вся логика fallback-ключей в одном месте, инструменты работают с типизированными объектами.

### 4.3. Устранение type: ignore

**Проблема:** `dialogs.py:20,46` — `ctx: Context = None  # type: ignore[assignment]`

**Решение:** Использовать правильную типизацию FastMCP:
```python
# FastMCP инжектирует ctx автоматически — не нужен default=None
@mcp.tool()
async def list_dialogs(page: int = 1, ctx: Context) -> str:
```

Или если нужна обратная совместимость:
```python
from typing import Optional

@mcp.tool()
async def list_dialogs(page: int = 1, *, ctx: Optional[Context] = None) -> str:
```

### 4.4. Устранение прямого доступа к private-полям pykwork

**Проблема:** `session.py:74,84` — `client._token = token`. Прямой доступ к приватному полю — сломается при обновлении pykwork.

**Решение:**
1. Открыть PR в pykwork с публичным setter/getter для token
2. До принятия PR — обернуть в метод-хелпер с комментарием:

```python
def _set_client_token(client: Kwork, token: str) -> None:
    """Set auth token on pykwork client.

    Uses private field until pykwork exposes public API.
    Tracked: https://github.com/kesha1225/pykwork/issues/XX
    """
    client._token = token  # noqa: SLF001
```

### 4.5. Устранение прямого HTTP-вызова в get_exchange_info

**Проблема:** `projects.py:154-159` — обход pykwork для `/exchangeInfo`. Прямое использование `client.session.post()`, хардкод URL, доступ к `client._token`.

**Решение:**
1. Открыть PR в pykwork с методом `exchange_info()`
2. До принятия — вынести в session.py как отдельный метод:

```python
async def get_exchange_info(self) -> dict:
    """Fetch exchange info (bypasses pykwork, endpoint not wrapped)."""
    client = await self.ensure_client()
    from kwork.api import AUTH_HEADER
    resp = await client.session.post(
        "https://api.kwork.ru/exchangeInfo",
        headers={"Authorization": AUTH_HEADER},
        data={"token": client._token},
    )
    return await resp.json(content_type=None)
```

### 4.6. Магические числа

| Файл | Строка | Значение | Что делать |
|---|---|---|---|
| `rate_limiter.py` | 34 | `max 20 retries` | Вынести в конфиг |
| `dialogs.py` | 37 | `[:80]` обрезка сообщения | Использовать `truncate()` из utils |
| `orders.py` | 97 | `[:10]` последних событий | Параметр `max_events` |
| `offers.py` | 12 | `_MIN_DESCRIPTION_LENGTH = 150` | Уже вынесено — хорошо |
| `kworks.py` | 50 | `[:5]` похожих кворков | Параметр `max_similar` |

---

## 5. Тестирование

### 5.1. Текущее состояние

- **11 тестов** в 3 модулях (config, rate_limiter, session)
- **0 тестов** для tool-модулей (8 файлов, 25 инструментов)
- **0 тестов** для `api_guard()` (errors.py)
- Оценочное покрытие: ~15%

### 5.2. In-memory тестирование инструментов (FastMCP 3.0)

FastMCP 3.0 позволяет тестировать инструменты без subprocess, используя `fastmcp.Client`:

```python
import pytest
from fastmcp.client import Client

@pytest.fixture
async def mcp_client():
    from kwork_mcp.server import mcp
    async with Client(transport=mcp) as client:
        yield client

async def test_list_tools(mcp_client):
    tools = await mcp_client.list_tools()
    assert len(tools) == 25  # все инструменты зарегистрированы

async def test_get_me(mcp_client, mocker):
    # Мок pykwork клиента
    mock_actor = mocker.MagicMock()
    mock_actor.username = "testuser"
    mock_actor.email = "test@example.com"
    ...
    result = await mcp_client.call_tool("get_me", {})
    assert "testuser" in result.data
```

### 5.3. Тесты для api_guard()

```python
import pytest
from kwork.exceptions import KworkHTTPException
from kwork_mcp.errors import api_guard
from mcp.server.fastmcp.exceptions import ToolError

async def test_api_guard_captcha():
    with pytest.raises(ToolError, match="капчу"):
        async with api_guard("test"):
            exc = KworkHTTPException(status=200, response_json={"error_code": 118})
            raise exc

async def test_api_guard_401():
    with pytest.raises(ToolError, match="Сессия истекла"):
        async with api_guard("test"):
            raise KworkHTTPException(status=401, response_json={})

async def test_api_guard_timeout():
    with pytest.raises(ToolError, match="таймаут"):
        async with api_guard("test"):
            raise TimeoutError()
```

### 5.4. Параметризованные тесты для форматирования

```python
@pytest.mark.parametrize("ts,expected", [
    (None, "—"),
    (0, "01.01.1970"),
    (1710500000, "15.03.2024"),
])
def test_format_date(ts, expected):
    assert format_date(ts) == expected

@pytest.mark.parametrize("text,limit,expected", [
    (None, 200, "—"),
    ("", 200, "—"),
    ("short", 200, "short"),
    ("a" * 300, 200, "a" * 200 + "..."),
])
def test_truncate(text, limit, expected):
    assert truncate(text, limit) == expected
```

### 5.5. Тесты на реальных API-ответах

В `docs/api_responses.md` уже задокументированы реальные структуры ответов API. Использовать их как фикстуры:

```python
# conftest.py
import json
from pathlib import Path

@pytest.fixture
def api_responses():
    path = Path(__file__).parent.parent / "docs" / "api_responses.md"
    # Парсинг JSON-блоков из markdown
    ...
```

### 5.6. Целевое покрытие

| Модуль | Текущее | Целевое | Приоритет |
|---|---|---|---|
| errors.py | 0% | 100% | Высокий |
| utils.py (новый) | 0% | 100% | Высокий |
| normalizers.py (новый) | 0% | 100% | Высокий |
| tools/*.py | 0% | 80%+ | Средний |
| session.py | ~60% | 90% | Средний |
| config.py | ~50% | 80% | Низкий |

**Целевое общее покрытие: 60%+**

### 5.7. CI: добавить coverage-отчёт

```yaml
# .github/workflows/ci.yml
- name: Run tests with coverage
  run: uv run python -m pytest tests/ -x -v --cov=kwork_mcp --cov-report=xml
- name: Upload coverage
  uses: codecov/codecov-action@v4
```

---

## 6. Безопасность

### 6.1. Сильные стороны (сохранить)

- Токен хранится с `os.open(..., 0o600)` — атомарные permissions
- Пароль в `SecretStr` — не утекает в логи
- `api_guard()` не раскрывает внутренние детали в ToolError
- `.gitignore` покрывает `.env`, `.kwork_token`

### 6.2. Валидация входных данных

**Проблема:** Нет валидации параметров до вызова API. Отрицательный `project_id` или пустая строка `username` уйдут в API как есть.

**Решение:** Pydantic-валидация или ручные проверки:

```python
@mcp.tool()
async def get_project(project_id: int, ctx: Context) -> str:
    if project_id <= 0:
        raise ToolError("project_id должен быть положительным числом.")
    ...

@mcp.tool()
async def send_message(user_id: int, text: str, ctx: Context) -> str:
    if user_id <= 0:
        raise ToolError("user_id должен быть положительным числом.")
    if not text.strip():
        raise ToolError("Текст сообщения не может быть пустым.")
    ...
```

### 6.3. Sanitization ошибок

**Проблема:** `errors.py:41` — `raise ToolError(f"Ошибка {operation}: {exc}")` может передать внутренние детали pykwork (URL, параметры) агенту.

**Решение:**
```python
# Не передавать полный exc, только его сообщение
raise ToolError(f"Ошибка {operation}: {exc}") from exc
# Лучше:
raise ToolError(f"Ошибка {operation}. Проверьте параметры и попробуйте снова.") from exc
```

### 6.4. Зависимости

**Проблема:** Нет upper bounds на зависимости (`kwork>=0.2.0` — примет и несовместимую 1.0.0).

**Решение:**
```toml
dependencies = [
    "fastmcp>=3.0,<4",
    "kwork>=0.2.0,<0.3",
    "pydantic-settings>=2.7,<3",
    "loguru>=0.7,<1",
]
```

---

## 7. Надёжность и отказоустойчивость

### 7.1. Auto-relogin при 401

**Проблема:** При истечении сессии (401) пользователь получает ToolError и вынужден перезапустить MCP-сервер.

**Решение:** `api_guard()` вызывает `session.relogin()` и повторяет запрос:

```python
@asynccontextmanager
async def api_guard(operation: str, session: KworkSessionManager | None = None):
    try:
        yield
    except KworkHTTPException as exc:
        if exc.status == 401 and session:
            logger.info("Session expired, attempting relogin...")
            await session.relogin()
            # Инструмент должен повторить вызов самостоятельно
            raise RetryableError("Сессия обновлена, повторите вызов.") from exc
        ...
```

### 7.2. Exponential backoff для 429/503

```python
# rate_limiter.py или отдельный retry.py
import asyncio
import random

async def retry_with_backoff(coro_factory, max_retries=3, base_delay=1.0):
    for attempt in range(max_retries):
        try:
            return await coro_factory()
        except KworkHTTPException as exc:
            if exc.status in (429, 503) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning("Rate limited, retrying in {:.1f}s...", delay)
                await asyncio.sleep(delay)
            else:
                raise
```

### 7.3. MCP Elicitation для капчи

**Текущее:** При капче (error code 118) — ToolError с текстом "откройте kwork.ru".

**Лучше:** Используя MCP Elicitation (URL mode), можно перенаправить пользователя прямо на страницу решения капчи:

```python
if code == 118:
    # URL-mode elicitation — открыть страницу капчи в браузере
    result = await ctx.elicit(
        message="Kwork требует решить капчу. Откройте ссылку в браузере.",
        mode="url",
        url="https://kwork.ru/captcha",
    )
    # После решения капчи — повторить запрос
```

### 7.4. MCP Tasks для долгих операций

`submit_offer` — 4-шаговый web flow, может занять несколько секунд. Tasks primitive позволяет вернуть handle сразу:

```python
@mcp.tool(task=True)
async def submit_offer(..., ctx: Context) -> str:
    # FastMCP автоматически создаёт Task и возвращает handle клиенту
    # Работа продолжается в фоне
    ...
```

### 7.5. Таймауты

**Проблема:** Нет явных таймаутов — полагаемся на дефолтный 30s pykwork.

**Решение:**
```python
import asyncio

async with api_guard("operation"):
    result = await asyncio.wait_for(client.some_method(), timeout=15.0)
```

---

## 8. Деплой и дистрибуция

### 8.1. Streamable HTTP транспорт

**Текущее:** только stdio (локальный запуск).

**Рекомендуемое:** добавить HTTP-транспорт для удалённого деплоя:

```python
# server.py
import sys

def run():
    transport = os.environ.get("KWORK_MCP_TRANSPORT", "stdio")
    if transport == "http":
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8080)
    else:
        mcp.run(transport="stdio")
```

### 8.2. Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen --no-dev
COPY src/ src/
EXPOSE 8080
ENV KWORK_MCP_TRANSPORT=http
CMD ["uv", "run", "python", "-m", "kwork_mcp"]
```

### 8.3. MCP Server Card (.well-known)

Из roadmap MCP на 2026 год — `.well-known/mcp.json` для автоматического обнаружения сервера:

```json
{
  "name": "kwork-mcp",
  "description": "MCP server for Kwork freelance marketplace",
  "version": "0.2.0",
  "tools": 25,
  "auth": "token",
  "transport": ["stdio", "streamable-http"]
}
```

---

## 9. DX (Developer Experience)

### 9.1. Логирование

**Текущее:** Минимальное логирование — только авторизация и ошибки.

**Рекомендуемое:** Добавить логирование вызовов инструментов:

```python
@asynccontextmanager
async def api_guard(operation: str):
    logger.debug("API call: {}", operation)
    start = time.monotonic()
    try:
        yield
    except ...:
        ...
    finally:
        elapsed = time.monotonic() - start
        logger.debug("API call {} completed in {:.2f}s", operation, elapsed)
```

### 9.2. Healthcheck endpoint

Для Docker/Kubernetes деплоя:

```python
@mcp.resource("health://status")
async def healthcheck() -> str:
    return json.dumps({
        "status": "ok",
        "authenticated": session._client is not None,
        "rate_limiter": {
            "rps": session._rate_limiter.rps,
            "burst": session._rate_limiter.burst,
        },
    })
```

### 9.3. CONTRIBUTING.md

Добавить руководство по разработке:
- Как добавить новый инструмент (шаблон, регистрация, тесты)
- Как запустить тесты
- Как настроить dev-окружение
- Архитектурные решения и их обоснования

### 9.4. Type checking

Добавить mypy или pyright в CI:

```toml
# pyproject.toml
[tool.mypy]
python_version = "3.12"
strict = true
warn_return_any = true

# .github/workflows/ci.yml
- name: Type check
  run: uv run mypy src/
```

---

## 10. Приоритизация

### Фаза 1: Quick wins (1-2 дня) — DONE (v0.2.0b1)

- [x] Создать `src/kwork_mcp/utils.py` — вынести дупликаты
- [x] Добавить валидацию входных параметров (ID > 0)
- [x] Улучшить описания всех 25 инструментов (когда использовать, что возвращает)
- [x] Добавить тесты для `api_guard()` (errors.py) — 9 тестов
- [x] Устранить `type: ignore` в dialogs.py
- [x] Добавить upper bounds на зависимости в pyproject.toml
- [x] Добавить coverage-отчёт в CI

### Фаза 2: Premium architecture (3-5 дней) — DONE (v0.2.0b1)

- [x] Миграция на FastMCP 3.0 (fastmcp>=3.1)
- [x] Добавить tool annotations (readOnly, destructive, idempotent)
- [x] Нормализация ответов API — через `unwrap_response()`, `safe_get()`, `check_success()`, `extract_error()` в utils.py
- [ ] Добавить тесты для tool-модулей (in-memory Client)
- [x] Вынести прямой HTTP-вызов из projects.py в session.py (`get_exchange_info()`)
- [x] Обернуть доступ к `_token` в helper-метод (`_set_client_token`, `_get_client_token`)
- [x] Санитизация ошибок — `mask_error_details=True`, детали в loguru
- [x] Auto-relogin при 401 через `api_guard(session=session)`
- [x] Smoke test в release CI
- [ ] Добавить логирование вызовов инструментов

### Фаза 3: Новые возможности (1-2 недели)

- [ ] Инструменты управления заказами (покупатель): 7 штук
- [ ] Инструменты треков (чат заказа): 3 штуки
- [ ] Structured outputs (dataclass возвраты)
- [ ] Составные outcome-oriented инструменты
- [ ] Переименование с префиксом `kwork_`

### Фаза 4: Production-grade (2-3 недели)

- [ ] Streamable HTTP транспорт
- [ ] Docker image
- [ ] OpenTelemetry трассировка
- [ ] MCP Elicitation для капчи
- [ ] Session state для кэширования
- [ ] Type checking (mypy) в CI
- [ ] Тестовое покрытие 60%+
- [ ] Поиск, каталог, портфолио — оставшиеся инструменты
- [ ] Dynamic component control (роли seller/buyer)

---

## Источники

- [Anthropic: Writing Tools for Agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [MCP Best Practices — Philipp Schmid](https://www.philschmid.de/mcp-best-practices)
- [54 Patterns for MCP Tools — Arcade](https://www.arcade.dev/blog/mcp-tool-patterns)
- [FastMCP 3.0 GA](https://www.jlowin.dev/blog/fastmcp-3-launch)
- [FastMCP Tools Documentation](https://gofastmcp.com/servers/tools)
- [FastMCP Testing](https://gofastmcp.com/servers/testing)
- [MCP Specification (ноябрь 2025)](https://modelcontextprotocol.io/specification/2025-11-25)
- [MCP Elicitation](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation)
- [MCP Tasks — WorkOS](https://workos.com/blog/mcp-async-tasks-ai-agent-workflows)
- [MCP 2026 Roadmap](http://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/)
- [15 Best Practices for MCP Servers — The New Stack](https://thenewstack.io/15-best-practices-for-building-mcp-servers-in-production/)
- [Docker MCP Best Practices](https://www.docker.com/blog/mcp-server-best-practices/)
- [MCP Security Best Practices](https://modelcontextprotocol.io/specification/draft/basic/security_best_practices)
- [OWASP: Secure MCP Server Development](https://genai.owasp.org/resource/a-practical-guide-for-secure-mcp-server-development/)
- [Context7 — Upstash](https://github.com/upstash/context7)
- [pykwork — GitHub](https://github.com/kesha1225/pykwork)
