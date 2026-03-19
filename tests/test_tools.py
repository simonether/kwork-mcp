from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kwork.schema import DialogMessage, InboxMessage, User

from kwork_mcp.session import KworkSessionManager
from kwork_mcp.tools import dialogs as dialogs_mod
from kwork_mcp.tools import profile as profile_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(mock_session: KworkSessionManager) -> MagicMock:
    ctx = MagicMock()
    ctx.lifespan_context = {"session": mock_session}
    return ctx


def _register_tools(module, mock_session: KworkSessionManager) -> dict:
    """Register tools and return {name: callable} mapping."""
    from fastmcp import FastMCP

    mcp = FastMCP("test")
    module.register(mcp)
    # FastMCP stores tools; we call the underlying functions directly
    # by grabbing them from the closure.
    # Instead, we'll use the module's register to capture closures.
    return mcp


# ---------------------------------------------------------------------------
# Profile: get_user_info
# ---------------------------------------------------------------------------


class TestGetUserInfo:
    @pytest.mark.asyncio
    async def test_by_user_id(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        user = User()
        user.id = 42
        user.username = "testuser"
        user.fullname = "Test User"
        user.rating = 4.9
        user.good_reviews = 10
        user.bad_reviews = 0
        user.level_description = "Pro"
        user.specialization = "Тексты"
        user.description = "Описание"
        user.online = True
        mock_client.get_user.return_value = user

        mcp = FastMCP("test")
        profile_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["get_user_info"].fn

        result = await fn(ctx=ctx, user_id=42)
        assert "ID пользователя: 42" in result
        assert "@testuser" not in result or "testuser" in result
        mock_client.get_user.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_by_username(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        mock_client.user_by_username.return_value = {
            "response": {
                "id": 99,
                "username": "found_user",
                "fullname": "Found",
                "rating": 4.5,
                "good_reviews": 5,
                "bad_reviews": 1,
                "level_description": "Новичок",
                "specialization": "Дизайн",
                "description": "Привет",
                "online": False,
            }
        }

        mcp = FastMCP("test")
        profile_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["get_user_info"].fn

        result = await fn(ctx=ctx, username="found_user")
        assert "ID пользователя: 99" in result
        assert "found_user" in result
        mock_client.user_by_username.assert_awaited_once_with(username="found_user")

    @pytest.mark.asyncio
    async def test_neither_raises(self, mock_session: KworkSessionManager) -> None:
        ctx = _make_ctx(mock_session)
        mcp = FastMCP("test")
        profile_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["get_user_info"].fn

        with pytest.raises(ToolError, match="Укажите хотя бы один"):
            await fn(ctx=ctx)

    @pytest.mark.asyncio
    async def test_username_strips_at(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        mock_client.user_by_username.return_value = {"response": {"id": 1, "username": "test"}}

        mcp = FastMCP("test")
        profile_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["get_user_info"].fn

        await fn(ctx=ctx, username="@test")
        mock_client.user_by_username.assert_awaited_once_with(username="test")

    @pytest.mark.asyncio
    async def test_username_not_found(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        mock_client.user_by_username.return_value = {"response": {}}

        mcp = FastMCP("test")
        profile_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["get_user_info"].fn

        with pytest.raises(ToolError, match="не найден"):
            await fn(ctx=ctx, username="ghost")


# ---------------------------------------------------------------------------
# Profile: search_users
# ---------------------------------------------------------------------------


class TestSearchUsers:
    @pytest.mark.asyncio
    async def test_found(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        mock_client.user_search.return_value = {
            "response": [
                {"id": 1, "username": "alice", "fullname": "Alice", "rating": 5.0, "specialization": "Тексты"},
                {"id": 2, "username": "bob", "fullname": "", "rating": 4.0},
            ]
        }

        mcp = FastMCP("test")
        profile_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["search_users"].fn

        result = await fn(query="test", ctx=ctx)
        assert "@alice" in result
        assert "ID: 1" in result
        assert "@bob" in result

    @pytest.mark.asyncio
    async def test_empty_query(self, mock_session: KworkSessionManager) -> None:
        ctx = _make_ctx(mock_session)
        mcp = FastMCP("test")
        profile_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["search_users"].fn

        with pytest.raises(ToolError, match="пустым"):
            await fn(query="", ctx=ctx)


# ---------------------------------------------------------------------------
# Dialogs: list_dialogs includes user_id
# ---------------------------------------------------------------------------


class TestListDialogs:
    @pytest.mark.asyncio
    async def test_includes_user_id(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        dialog = DialogMessage()
        dialog.username = "seller"
        dialog.user_id = 777
        dialog.last_message = "Привет"
        dialog.unread_count = 0
        dialog.time = 1705320000
        mock_client.get_dialogs_page.return_value = [dialog]

        mcp = FastMCP("test")
        dialogs_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["list_dialogs"].fn

        result = await fn(ctx=ctx)
        assert "ID: 777" in result
        assert "@seller" in result


# ---------------------------------------------------------------------------
# Dialogs: get_dialog includes message_id
# ---------------------------------------------------------------------------


class TestGetDialog:
    @pytest.mark.asyncio
    async def test_includes_message_id(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        msg = InboxMessage()
        msg.message_id = 555
        msg.from_username = "sender"
        msg.time = 1705320000
        msg.message = "Тестовое сообщение"
        paging = {"pages": 1}
        mock_client.get_dialog_with_user_page.return_value = ([msg], paging)

        mcp = FastMCP("test")
        dialogs_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["get_dialog"].fn

        result = await fn(username="sender", ctx=ctx)
        assert "#555" in result
        assert "@sender" in result


# ---------------------------------------------------------------------------
# Dialogs: send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_by_user_id(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        mock_client.send_message.return_value = {"success": True}

        mcp = FastMCP("test")
        dialogs_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["send_message"].fn

        result = await fn(text="Привет", ctx=ctx, user_id=42)
        assert "ID 42" in result
        mock_client.send_message.assert_awaited_once_with(user_id=42, text="Привет")

    @pytest.mark.asyncio
    async def test_by_username_resolves(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        mock_client.user_by_username.return_value = {"response": {"id": 99, "username": "target"}}
        mock_client.send_message.return_value = {"success": True}

        mcp = FastMCP("test")
        dialogs_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["send_message"].fn

        result = await fn(text="Привет", ctx=ctx, username="target")
        assert "@target" in result
        assert "ID 99" in result
        mock_client.send_message.assert_awaited_once_with(user_id=99, text="Привет")

    @pytest.mark.asyncio
    async def test_neither_raises(self, mock_session: KworkSessionManager) -> None:
        ctx = _make_ctx(mock_session)
        mcp = FastMCP("test")
        dialogs_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["send_message"].fn

        with pytest.raises(ToolError, match="Укажите хотя бы один"):
            await fn(text="Hello", ctx=ctx)


# ---------------------------------------------------------------------------
# Dialogs: edit_message
# ---------------------------------------------------------------------------


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_success(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        mock_client.inbox_edit.return_value = {"success": True}

        mcp = FastMCP("test")
        dialogs_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["edit_message"].fn

        result = await fn(message_id=123, text="Исправлено", ctx=ctx)
        assert "#123" in result
        assert "отредактировано" in result
        mock_client.inbox_edit.assert_awaited_once_with(id=123, body={"text": "Исправлено"})

    @pytest.mark.asyncio
    async def test_failure(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        mock_client.inbox_edit.return_value = {"success": False, "error": "нельзя"}

        mcp = FastMCP("test")
        dialogs_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["edit_message"].fn

        result = await fn(message_id=123, text="Новый текст", ctx=ctx)
        assert "нельзя" in result

    @pytest.mark.asyncio
    async def test_invalid_id(self, mock_session: KworkSessionManager) -> None:
        ctx = _make_ctx(mock_session)
        mcp = FastMCP("test")
        dialogs_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["edit_message"].fn

        with pytest.raises(ToolError, match="положительным"):
            await fn(message_id=0, text="text", ctx=ctx)


# ---------------------------------------------------------------------------
# Dialogs: delete_message
# ---------------------------------------------------------------------------


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_success(self, mock_session: KworkSessionManager, mock_client: AsyncMock) -> None:
        ctx = _make_ctx(mock_session)
        mock_client.delete_message.return_value = {"success": True}

        mcp = FastMCP("test")
        dialogs_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["delete_message"].fn

        result = await fn(message_id=456, ctx=ctx)
        assert "#456" in result
        assert "удалено" in result
        mock_client.delete_message.assert_awaited_once_with(456)

    @pytest.mark.asyncio
    async def test_invalid_id(self, mock_session: KworkSessionManager) -> None:
        ctx = _make_ctx(mock_session)
        mcp = FastMCP("test")
        dialogs_mod.register(mcp)
        tools = {t.name: t for t in await mcp.list_tools()}
        fn = tools["delete_message"].fn

        with pytest.raises(ToolError, match="положительным"):
            await fn(message_id=-1, ctx=ctx)
