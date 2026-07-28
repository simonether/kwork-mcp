"""Read-only MCP tools with stable structured outcomes."""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field, StringConstraints

from kwork_mcp.models import (
    AccountData,
    CategoryRecord,
    ConnectsData,
    DialogRecord,
    ItemCollection,
    KworkRecord,
    MessageRecord,
    OfferRecord,
    OrderRecord,
    ProjectDiscoveryData,
    ProjectRecord,
    RawObjectData,
    ResultEnvelope,
    UserRecord,
)
from kwork_mcp.tools.common import (
    ANNO_READ,
    correlation_id,
    gateway_from_context,
    success,
    unexpected_failure,
)

AccountOutcome = ResultEnvelope[AccountData]
ConnectsOutcome = ResultEnvelope[ConnectsData]
UserOutcome = ResultEnvelope[UserRecord]
UsersOutcome = ResultEnvelope[ItemCollection[UserRecord]]
ProjectsOutcome = ResultEnvelope[ProjectDiscoveryData]
ProjectOutcome = ResultEnvelope[ProjectRecord]
OffersOutcome = ResultEnvelope[ItemCollection[OfferRecord]]
OfferOutcome = ResultEnvelope[OfferRecord]
OrdersOutcome = ResultEnvelope[ItemCollection[OrderRecord]]
DialogsOutcome = ResultEnvelope[ItemCollection[DialogRecord]]
MessagesOutcome = ResultEnvelope[ItemCollection[MessageRecord]]
KworksOutcome = ResultEnvelope[ItemCollection[KworkRecord]]
CategoriesOutcome = ResultEnvelope[ItemCollection[CategoryRecord]]
RawOutcome = ResultEnvelope[RawObjectData]


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        title="Статус привязки аккаунта Kwork",
        annotations=ANNO_READ,
        output_schema=AccountOutcome.model_json_schema(),
    )
    async def account_status(ctx: Context) -> ToolResult:
        """Проверить фактический аккаунт, ожидаемую привязку и готовность writes.

        Используйте первым после запуска и перед безопасным write-flow. Возвращает
        стабильные user_id/username и полную внешнюю запись профиля. Данные Kwork
        помечены как недоверенный внешний ввод.
        """
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).account_status()
            return success(
                AccountOutcome,
                data=data,
                summary=f"Kwork-аккаунт подтверждён: user_id={data.user_id}.",
                empty=False,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(AccountOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Баланс коннектов Kwork",
        annotations=ANNO_READ,
        output_schema=ConnectsOutcome.model_json_schema(),
    )
    async def get_connects(ctx: Context) -> ToolResult:
        """Получить активные и общие коннекты без изменения аккаунта."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).get_connects()
            return success(
                ConnectsOutcome,
                data=data,
                summary=f"Активных коннектов: {data.active}.",
                empty=False,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(ConnectsOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Профиль пользователя Kwork",
        annotations=ANNO_READ,
        output_schema=UserOutcome.model_json_schema(),
    )
    async def get_user_info(
        ctx: Context,
        user_id: Annotated[int, Field(gt=0)] | None = None,
        username: Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
        ]
        | None = None,
    ) -> ToolResult:
        """Получить полный публичный профиль ровно по одному user_id или username."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).get_user(
                user_id=user_id,
                username=username,
            )
            return success(
                UserOutcome,
                data=data,
                summary=(
                    f"Пользователь найден: user_id={data.user_id}." if data is not None else "Пользователь не найден."
                ),
                empty=data is None,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(UserOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Поиск пользователей Kwork",
        annotations=ANNO_READ,
        output_schema=UsersOutcome.model_json_schema(),
    )
    async def search_users(
        query: Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
        ],
        ctx: Context,
        page: Annotated[int, Field(ge=1, le=10_000)] = 1,
    ) -> ToolResult:
        """Искать пользователей Kwork с сохранением полных полей и paging metadata."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).search_users(query, page)
            return success(
                UsersOutcome,
                data=data,
                summary=f"Найдено пользователей на странице: {len(data.items)}.",
                empty=not data.items,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(UsersOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Поиск проектов Kwork",
        annotations=ANNO_READ,
        output_schema=ProjectsOutcome.model_json_schema(),
    )
    async def discover_projects(
        mode: Literal["favorites", "all", "category_ids"],
        ctx: Context,
        category_ids: Annotated[
            list[Annotated[int, Field(gt=0)]],
            Field(max_length=100),
        ]
        | None = None,
        price_from: Annotated[int, Field(ge=0)] | None = None,
        price_to: Annotated[int, Field(ge=0)] | None = None,
        hiring_from: Annotated[int, Field(ge=0, le=100)] | None = None,
        offers_from: Annotated[int, Field(ge=0)] | None = None,
        offers_to: Annotated[int, Field(ge=0)] | None = None,
        query: Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
        ]
        | None = None,
        cursor: Annotated[str, StringConstraints(min_length=10, max_length=4096)] | None = None,
    ) -> ToolResult:
        """Получить страницу проектов с явным discovery-режимом и opaque cursor.

        ``favorites`` передаёт пустую категорию, ``all`` — специальное значение
        ``all``, ``category_ids`` требует непустой список. Результат сохраняет полные
        описания и unknown upstream fields, paging, query fingerprint и watermark,
        пригодный для будущего delta polling.
        """
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).discover_projects(
                mode=mode,
                category_ids=category_ids,
                price_from=price_from,
                price_to=price_to,
                hiring_from=hiring_from,
                offers_from=offers_from,
                offers_to=offers_to,
                query=query,
                cursor=cursor,
            )
            return success(
                ProjectsOutcome,
                data=data,
                summary=f"Проектов на странице: {len(data.projects.items)}; mode={mode}.",
                empty=not data.projects.items,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(ProjectsOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Детали проекта Kwork",
        annotations=ANNO_READ,
        output_schema=ProjectOutcome.model_json_schema(),
    )
    async def get_project(
        project_id: Annotated[int, Field(gt=0)],
        ctx: Context,
    ) -> ToolResult:
        """Получить проект по стабильному project_id с полным описанием и raw fields."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).get_project(project_id)
            return success(
                ProjectOutcome,
                data=data,
                summary=(
                    f"Проект найден: project_id={project_id}."
                    if data is not None
                    else f"Проект {project_id} не найден."
                ),
                empty=data is None,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(ProjectOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Информация биржи Kwork",
        annotations=ANNO_READ,
        output_schema=RawOutcome.model_json_schema(),
    )
    async def get_exchange_info(ctx: Context) -> ToolResult:
        """Получить полный стандартный exchangeInfo response через pinned API client."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).get_exchange_info()
            empty = data.raw == [] or data.raw == {}
            return success(
                RawOutcome,
                data=data,
                summary="Статистика биржи получена." if not empty else "Статистика биржи пуста.",
                empty=empty,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(RawOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Список моих офферов Kwork",
        annotations=ANNO_READ,
        output_schema=OffersOutcome.model_json_schema(),
    )
    async def list_my_offers(
        ctx: Context,
        page: Annotated[int, Field(ge=1, le=10_000)] = 1,
    ) -> ToolResult:
        """Список собственных офферов; каждый элемент гарантирует offer_id и project_id."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).list_my_offers(page)
            return success(
                OffersOutcome,
                data=data,
                summary=f"Офферов на странице: {len(data.items)}.",
                empty=not data.items,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(OffersOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Детали оффера Kwork",
        annotations=ANNO_READ,
        output_schema=OfferOutcome.model_json_schema(),
    )
    async def get_offer(
        offer_id: Annotated[int, Field(gt=0)],
        ctx: Context,
    ) -> ToolResult:
        """Получить собственный оффер по offer_id, включая обязательный project_id."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).get_offer(offer_id)
            return success(
                OfferOutcome,
                data=data,
                summary=(f"Оффер найден: offer_id={offer_id}." if data is not None else f"Оффер {offer_id} не найден."),
                empty=data is None,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(OfferOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Список заказов продавца Kwork",
        annotations=ANNO_READ,
        output_schema=OrdersOutcome.model_json_schema(),
    )
    async def list_worker_orders(
        ctx: Context,
        page: Annotated[int, Field(ge=1, le=10_000)] = 1,
    ) -> ToolResult:
        """Список заказов продавца через реальный generic workerOrders page contract."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).list_worker_orders(page)
            return success(
                OrdersOutcome,
                data=data,
                summary=f"Заказов на странице: {len(data.items)}.",
                empty=not data.items,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(OrdersOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Детали заказа Kwork",
        annotations=ANNO_READ,
        output_schema=RawOutcome.model_json_schema(),
    )
    async def get_order_details(
        order_id: Annotated[int, Field(gt=0)],
        ctx: Context,
    ) -> ToolResult:
        """Получить полную структуру details/stages/tracks заказа без обрезки."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).get_order_details(order_id)
            return success(
                RawOutcome,
                data=data,
                summary=(f"Детали заказа {order_id} получены." if data is not None else f"Заказ {order_id} не найден."),
                empty=data is None,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(RawOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Список диалогов Kwork",
        annotations=ANNO_READ,
        output_schema=DialogsOutcome.model_json_schema(),
    )
    async def list_dialogs(
        ctx: Context,
        page: Annotated[int, Field(ge=1, le=10_000)] = 1,
    ) -> ToolResult:
        """Получить страницу диалогов с полными последними сообщениями."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).list_dialogs(page)
            return success(
                DialogsOutcome,
                data=data,
                summary=f"Диалогов на странице: {len(data.items)}.",
                empty=not data.items,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(DialogsOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Сообщения диалога Kwork",
        annotations=ANNO_READ,
        output_schema=MessagesOutcome.model_json_schema(),
    )
    async def get_dialog(
        username: Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
        ],
        ctx: Context,
        page: Annotated[int, Field(ge=1, le=10_000)] = 1,
    ) -> ToolResult:
        """Получить полные сообщения диалога по username и странице."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).get_dialog(username.lstrip("@"), page)
            return success(
                MessagesOutcome,
                data=data,
                summary=f"Сообщений на странице: {len(data.items)}.",
                empty=not data.items,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(MessagesOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Список моих кворков",
        annotations=ANNO_READ,
        output_schema=KworksOutcome.model_json_schema(),
    )
    async def list_my_kworks(ctx: Context) -> ToolResult:
        """Получить все собственные кворки с status group и полными raw fields."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).list_my_kworks()
            return success(
                KworksOutcome,
                data=data,
                summary=f"Кворков: {len(data.items)}.",
                empty=not data.items,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(KworksOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Детали кворка",
        annotations=ANNO_READ,
        output_schema=RawOutcome.model_json_schema(),
    )
    async def get_kwork_details(
        kwork_id: Annotated[int, Field(gt=0)],
        ctx: Context,
    ) -> ToolResult:
        """Получить полный getKworkDetailsExtra response без ложных полей."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).get_kwork_details(kwork_id)
            return success(
                RawOutcome,
                data=data,
                summary=(f"Данные кворка {kwork_id} получены." if data is not None else f"Кворк {kwork_id} не найден."),
                empty=data is None,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(RawOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Дерево категорий Kwork",
        annotations=ANNO_READ,
        output_schema=CategoriesOutcome.model_json_schema(),
    )
    async def list_categories(ctx: Context) -> ToolResult:
        """Получить полное трёхуровневое дерево категорий и их ID."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).list_categories()
            return success(
                CategoriesOutcome,
                data=data,
                summary=f"Корневых категорий: {len(data.items)}.",
                empty=not data.items,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(CategoriesOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Избранные категории Kwork",
        annotations=ANNO_READ,
        output_schema=RawOutcome.model_json_schema(),
    )
    async def list_favorite_categories(ctx: Context) -> ToolResult:
        """Получить полный ответ избранных категорий текущего аккаунта."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).list_favorite_categories()
            empty = data.raw == [] or data.raw == {}
            return success(
                RawOutcome,
                data=data,
                summary="Избранные категории получены." if not empty else "Избранных категорий нет.",
                empty=empty,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(RawOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Уведомления Kwork",
        annotations=ANNO_READ,
        output_schema=RawOutcome.model_json_schema(),
    )
    async def list_notifications(ctx: Context) -> ToolResult:
        """Получить полные notification groups и вложенные notifications."""
        correlation = correlation_id()
        try:
            data = await gateway_from_context(ctx).list_notifications()
            empty = data.raw == [] or data.raw == {}
            return success(
                RawOutcome,
                data=data,
                summary="Уведомления получены." if not empty else "Уведомлений нет.",
                empty=empty,
                correlation=correlation,
            )
        except Exception as exc:
            return unexpected_failure(RawOutcome, exception=exc, correlation=correlation)
