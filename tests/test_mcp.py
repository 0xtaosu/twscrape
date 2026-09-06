import json
from typing import Any, cast

import pytest

from twscrape.accounts_pool import NoAccountError
from twscrape.mcp import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    McpError,
    McpService,
    handle_message,
    tool_catalog,
)
from twscrape.x_api import XApiNotFoundError, XApiService, XApiUnavailableError


class FakeXApi:
    """Records what the tool layer forwarded, so tests assert on the mapping."""

    def __init__(self, raise_with: Exception | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self.raise_with = raise_with

    def _record(self, name: str, *args):
        self.calls.append((name, args))
        if self.raise_with:
            raise self.raise_with
        return {"ok": name, "args": list(args)}

    async def search(self, query, limit):
        return self._record("search", query, limit)

    async def user(self, ident, by="username"):
        return self._record("user", ident, by)

    async def user_tweets(self, ident, limit, include_replies, by):
        return self._record("user_tweets", ident, limit, include_replies, by)

    async def followers(self, ident, limit, by, skip_user, cursor=None):
        return self._record("followers", ident, limit, by, skip_user, cursor)

    async def following(self, ident, limit, by, skip_user, cursor=None):
        return self._record("following", ident, limit, by, skip_user, cursor)

    async def tweet(self, tweet_id):
        return self._record("tweet", tweet_id)


def make_service(raise_with: Exception | None = None) -> tuple[McpService, FakeXApi]:
    fake = FakeXApi(raise_with)

    async def health():
        return {"total": 3, "active": 2}

    return McpService(cast(XApiService, cast(Any, fake)), health), fake


async def send(service: McpService, message: dict) -> Any:
    return await handle_message(service, message, "1.2.3")


def rpc(method: str, params: dict | None = None, message_id: Any = 1) -> dict:
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


async def call_tool(service: McpService, name: str, arguments: dict | None = None) -> dict:
    response = await send(service, rpc("tools/call", {"name": name, "arguments": arguments or {}}))
    assert response is not None
    return response["result"]


async def test_initialize_negotiates_and_advertises_tools():
    service, _ = make_service()

    response = await send(service, rpc("initialize", {"protocolVersion": "2024-11-05"}))
    assert response is not None
    result = response["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"] == {
        "name": "twscrape",
        "title": "twscrape X 只读接口",
        "version": "1.2.3",
    }
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert "只读" in result["instructions"]

    # An unknown revision falls back to ours rather than echoing nonsense back.
    unknown = await send(service, rpc("initialize", {"protocolVersion": "1999-01-01"}))
    assert unknown is not None
    assert unknown["result"]["protocolVersion"] == PROTOCOL_VERSION


async def test_notifications_get_no_reply():
    service, _ = make_service()

    assert await send(service, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    # Even a method we do not implement stays silent when sent as a notification.
    assert await send(service, {"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None


async def test_tools_list_schemas_are_closed_and_documented():
    service, _ = make_service()

    response = await send(service, rpc("tools/list"))
    assert response is not None
    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "search_tweets",
        "get_user",
        "get_user_tweets",
        "get_user_followers",
        "get_user_following",
        "get_tweet",
        "get_pool_health",
    ]
    for tool in tools:
        assert tool["description"]
        assert tool["title"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False

    # Tweet ids run past 2^53, so they must not be typed as JSON numbers.
    get_tweet = next(tool for tool in tools if tool["name"] == "get_tweet")
    assert get_tweet["inputSchema"]["properties"]["tweet_id"]["type"] == "string"


async def test_tool_call_forwards_arguments_and_returns_both_shapes():
    service, fake = make_service()

    result = await call_tool(
        service,
        "get_user_followers",
        {
            "user": "12345",
            "by": "id",
            "limit": 50,
            "skip_user": True,
            "cursor": "abc123",
        },
    )

    assert fake.calls == [("followers", ("12345", 50, "id", True, "abc123"))]
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "ok": "followers",
        "args": ["12345", 50, "id", True, "abc123"],
    }
    # The text block must be the same payload, for clients that ignore structured output.
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


async def test_tool_call_applies_documented_defaults():
    service, fake = make_service()

    await call_tool(service, "get_user_tweets", {"user": "alice"})
    await call_tool(service, "get_user", {"user": "  alice  "})

    assert fake.calls == [
        ("user_tweets", ("alice", 20, False, "username")),
        ("user", ("alice", "username")),
    ]


async def test_pool_health_needs_no_x_api_call():
    service, fake = make_service()

    result = await call_tool(service, "get_pool_health")

    assert fake.calls == []
    assert result["structuredContent"] == {"total": 3, "active": 2}


@pytest.mark.parametrize(
    "name,arguments,expected",
    [
        ("get_user", {}, "缺少必填参数 user"),
        ("get_user", {"user": "   "}, "缺少必填参数 user"),
        ("get_user", {"user": 7}, "user 必须是字符串"),
        ("get_user", {"user": "a", "by": "email"}, "by 必须是 username 或 id"),
        ("get_user", {"user": "a", "nope": 1}, "不支持的参数: nope"),
        ("search_tweets", {"query": "x", "limit": 0}, "limit 必须在 1 到 200 之间"),
        ("search_tweets", {"query": "x", "limit": 201}, "limit 必须在 1 到 200 之间"),
        ("search_tweets", {"query": "x", "limit": True}, "limit 必须是整数"),
        ("search_tweets", {"query": "x" * 501}, "query 不能超过 500 个字符"),
        (
            "get_user_tweets",
            {"user": "a", "include_replies": "yes"},
            "include_replies 必须是布尔值",
        ),
        ("get_user_followers", {"user": "a", "cursor": "a b"}, "cursor 格式无效"),
        ("get_user_followers", {"user": "a", "cursor": "c" * 501}, "cursor 不能超过 500 个字符"),
        ("get_tweet", {"tweet_id": "not-a-number"}, "tweet_id 必须是数字"),
        ("get_tweet", {"tweet_id": "0"}, "tweet_id 必须是正数"),
        ("get_pool_health", {"limit": 5}, "不支持的参数: limit"),
    ],
)
async def test_bad_arguments_are_tool_errors_not_protocol_errors(name, arguments, expected):
    """A model can read and fix a tool error; a JSON-RPC error just kills the call."""
    service, fake = make_service()

    result = await call_tool(service, name, arguments)

    assert result["isError"] is True
    assert result["content"][0]["text"] == expected
    assert fake.calls == []


async def test_tweet_id_is_parsed_without_float_rounding():
    service, fake = make_service()

    await call_tool(service, "get_tweet", {"tweet_id": "1234567890123456789"})

    assert fake.calls == [("tweet", (1234567890123456789,))]


@pytest.mark.parametrize(
    "error,expected",
    [
        (XApiNotFoundError("用户不存在"), "用户不存在"),
        (NoAccountError("pool drained"), "账号池暂时没有可用账号，请稍后重试"),
        (ValueError("limit 必须是整数"), "limit 必须是整数"),
    ],
)
async def test_upstream_failures_surface_as_tool_errors(error, expected):
    service, _ = make_service(error)

    result = await call_tool(service, "get_user", {"user": "ghost"})

    assert result["isError"] is True
    assert result["content"][0]["text"] == expected


async def test_unavailable_upstream_reports_reason():
    service, _ = make_service(XApiUnavailableError("上游暂时不可用", reason="no_account"))

    result = await call_tool(service, "get_user", {"user": "ghost"})

    assert result["isError"] is True
    assert result["content"][0]["text"] == "上游暂时不可用（reason: no_account）"


@pytest.mark.parametrize(
    "message,code",
    [
        ([], INVALID_REQUEST),
        ({"id": 1, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": "resources/read"}, METHOD_NOT_FOUND),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, INVALID_PARAMS),
    ],
)
async def test_malformed_messages_raise_protocol_errors(message, code):
    service, _ = make_service()

    with pytest.raises(McpError) as error:
        await send(service, message)

    assert error.value.code == code


@pytest.mark.parametrize(
    "params",
    [
        {"name": "", "arguments": {}},
        {"name": 5, "arguments": {}},
        {"name": "drop_database", "arguments": {}},
        {"name": "get_user", "arguments": "user=alice"},
    ],
)
async def test_bad_tool_call_envelope_is_a_protocol_error(params):
    """Naming a tool that does not exist is a broken client, not a failed tool."""
    service, _ = make_service()

    with pytest.raises(McpError) as error:
        await send(service, rpc("tools/call", params))

    assert error.value.code == INVALID_PARAMS


async def test_ping_answers_empty_result():
    service, _ = make_service()

    response = await send(service, rpc("ping", message_id="ping-1"))

    assert response == {"jsonrpc": "2.0", "id": "ping-1", "result": {}}


def test_catalog_is_rebuilt_per_call():
    """Callers mutate what tools/list hands them; a shared list would leak that."""
    first = tool_catalog()
    first[0]["name"] = "mutated"

    assert tool_catalog()[0]["name"] == "search_tweets"
