"""MCP (Model Context Protocol) server over the dashboard's read-only X API.

Transport is Streamable HTTP: a single `POST /mcp` carrying one JSON-RPC 2.0
message, answered with `application/json`. The spec allows a JSON response in
place of an SSE stream whenever the server has nothing to stream, which is
always true here - every tool is a single request/response call. Skipping SSE
keeps this inside the stdlib `http.server` the dashboard already runs on, with
no session state to expire and no extra dependency.

Authentication is the dashboard API key (`Authorization: Bearer tws_...`), the
same key the console page issues for curl - see `dashboard.DashboardHandler`.
"""

import json
from typing import Any, Awaitable, Callable

from .accounts_pool import NoAccountError
from .x_api import XApiNotFoundError, XApiService, XApiUnavailableError

# The spec revision this server implements. Older clients are answered in their
# own revision when we can speak it, because the only differences that reach
# this server are in fields it does not use.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_NAME = "twscrape"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

MAX_LIMIT = 200
MAX_QUERY_LEN = 500
MAX_CURSOR_LEN = 500

_USER_ARG = {
    "type": "string",
    "description": "用户名（不含 @）或数字 ID，取决于 by",
}
_BY_ARG = {
    "type": "string",
    "enum": ["username", "id"],
    "default": "username",
    "description": "user 参数的解析方式。用户名会变，长期跟踪用 id 更稳",
}
_LIMIT_ARG = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_LIMIT,
    "default": 20,
    "description": f"最多返回多少条（1-{MAX_LIMIT}）",
}
_CURSOR_ARG = {
    "type": "string",
    "description": "上一次响应里的 next_cursor，用于翻页",
}
_SKIP_USER_ARG = {
    "type": "boolean",
    "default": False,
    "description": "跳过目标用户资料查询。配合 by=id 使用可省掉一次上游请求",
}


def tool_catalog() -> list[dict[str, Any]]:
    """Tool definitions exactly as `tools/list` returns them."""
    return [
        {
            "name": "search_tweets",
            "title": "搜索推文",
            "description": "使用 X 搜索语法查询推文，例如 'python lang:en'。只读。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "maxLength": MAX_QUERY_LEN,
                        "description": "X 搜索语法查询串",
                    },
                    "limit": _LIMIT_ARG,
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_user",
            "title": "用户资料",
            "description": "按用户名或数字 ID 获取公开资料。只读。",
            "inputSchema": {
                "type": "object",
                "properties": {"user": _USER_ARG, "by": _BY_ARG},
                "required": ["user"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_user_tweets",
            "title": "用户推文",
            "description": "获取用户时间线，可选择是否包含回复。只读。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user": _USER_ARG,
                    "by": _BY_ARG,
                    "limit": _LIMIT_ARG,
                    "include_replies": {
                        "type": "boolean",
                        "default": False,
                        "description": "是否把回复也算进时间线",
                    },
                },
                "required": ["user"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_user_followers",
            "title": "关注者列表",
            "description": (
                "获取关注该用户的账号。X 按页返回，count 可能略多于 limit；"
                "把 next_cursor 回传给 cursor 取下一页。只读。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user": _USER_ARG,
                    "by": _BY_ARG,
                    "limit": _LIMIT_ARG,
                    "skip_user": _SKIP_USER_ARG,
                    "cursor": _CURSOR_ARG,
                },
                "required": ["user"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_user_following",
            "title": "关注列表",
            "description": (
                "获取该用户正在关注的账号。X 按页返回，count 可能略多于 limit；"
                "把 next_cursor 回传给 cursor 取下一页。只读。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user": _USER_ARG,
                    "by": _BY_ARG,
                    "limit": _LIMIT_ARG,
                    "skip_user": _SKIP_USER_ARG,
                    "cursor": _CURSOR_ARG,
                },
                "required": ["user"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_tweet",
            "title": "单条推文",
            "description": "按推文 ID 获取详情。只读。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        # Tweet ids run past 2^53, where a JSON number silently
                        # loses its last digits. Take them as strings.
                        "description": "推文数字 ID，用字符串传避免大整数精度丢失",
                    }
                },
                "required": ["tweet_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_pool_health",
            "title": "账号池健康",
            "description": "查看账号池总数、活跃数、锁定数，以及下一个可用时间。只读。",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


class McpError(Exception):
    """A JSON-RPC level failure - malformed request, unknown method."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class McpToolError(Exception):
    """A tool that ran and failed. Reported in the result, not as a JSON-RPC error.

    MCP keeps these apart on purpose: a protocol error means the client is
    broken, while a tool error is information the model should see and can act
    on ("that user is suspended") rather than a transport failure.
    """


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise McpError(INVALID_PARAMS, f"{name} 必须是对象")
    return value


def _arg_str(args: dict[str, Any], name: str, required: bool = False) -> str | None:
    value = args.get(name)
    if value is None:
        if required:
            raise McpToolError(f"缺少必填参数 {name}")
        return None
    if not isinstance(value, str):
        raise McpToolError(f"{name} 必须是字符串")
    text: str = value.strip()
    if not text and required:
        raise McpToolError(f"缺少必填参数 {name}")
    return text or None


def _arg_bool(args: dict[str, Any], name: str, default: bool = False) -> bool:
    value = args.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise McpToolError(f"{name} 必须是布尔值")
    return value


def _arg_limit(args: dict[str, Any], default: int = 20) -> int:
    value = args.get("limit")
    if value is None:
        return default
    # bool is an int subclass; True would otherwise pass as limit=1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise McpToolError("limit 必须是整数")
    if not 1 <= value <= MAX_LIMIT:
        raise McpToolError(f"limit 必须在 1 到 {MAX_LIMIT} 之间")
    return value


def _arg_by(args: dict[str, Any]) -> str:
    value = args.get("by")
    if value is None:
        return "username"
    if not isinstance(value, str) or value not in {"username", "id"}:
        raise McpToolError("by 必须是 username 或 id")
    return value


def _arg_cursor(args: dict[str, Any]) -> str | None:
    cursor = _arg_str(args, "cursor")
    if cursor is None:
        return None
    if len(cursor) > MAX_CURSOR_LEN:
        raise McpToolError(f"cursor 不能超过 {MAX_CURSOR_LEN} 个字符")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in cursor):
        raise McpToolError("cursor 格式无效")
    return cursor


def _arg_tweet_id(args: dict[str, Any]) -> int:
    raw = _arg_str(args, "tweet_id", required=True) or ""
    try:
        tweet_id = int(raw)
    except ValueError as error:
        raise McpToolError("tweet_id 必须是数字") from error
    if tweet_id <= 0:
        raise McpToolError("tweet_id 必须是正数")
    return tweet_id


def _reject_unknown(args: dict[str, Any], allowed: set[str]) -> None:
    if unknown := set(args) - allowed:
        raise McpToolError(f"不支持的参数: {', '.join(sorted(unknown))}")


class McpService:
    """Maps MCP tool calls onto the same read-only facade the JSON API uses."""

    def __init__(self, x_api: XApiService, health: Callable[[], Awaitable[dict[str, Any]]]):
        self.x_api = x_api
        self.health = health

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "search_tweets":
            _reject_unknown(args, {"query", "limit"})
            query = _arg_str(args, "query", required=True) or ""
            if len(query) > MAX_QUERY_LEN:
                raise McpToolError(f"query 不能超过 {MAX_QUERY_LEN} 个字符")
            return await self.x_api.search(query, _arg_limit(args))

        if name == "get_user":
            _reject_unknown(args, {"user", "by"})
            user = _arg_str(args, "user", required=True) or ""
            return await self.x_api.user(user, _arg_by(args))

        if name == "get_user_tweets":
            _reject_unknown(args, {"user", "by", "limit", "include_replies"})
            user = _arg_str(args, "user", required=True) or ""
            return await self.x_api.user_tweets(
                user, _arg_limit(args), _arg_bool(args, "include_replies"), _arg_by(args)
            )

        if name in {"get_user_followers", "get_user_following"}:
            _reject_unknown(args, {"user", "by", "limit", "skip_user", "cursor"})
            user = _arg_str(args, "user", required=True) or ""
            method = self.x_api.followers if name == "get_user_followers" else self.x_api.following
            return await method(
                user,
                _arg_limit(args),
                _arg_by(args),
                _arg_bool(args, "skip_user"),
                _arg_cursor(args),
            )

        if name == "get_tweet":
            _reject_unknown(args, {"tweet_id"})
            return await self.x_api.tweet(_arg_tweet_id(args))

        if name == "get_pool_health":
            _reject_unknown(args, set())
            return await self.health()

        raise McpError(INVALID_PARAMS, f"未知工具: {name}")


def _result(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_failure(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def negotiate_protocol(requested: Any) -> str:
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return PROTOCOL_VERSION


async def handle_message(
    service: McpService, message: Any, server_version: str
) -> dict[str, Any] | None:
    """Answer one JSON-RPC message. Returns None for notifications."""
    if not isinstance(message, dict):
        raise McpError(INVALID_REQUEST, "JSON-RPC 消息必须是对象")
    if message.get("jsonrpc") != "2.0":
        raise McpError(INVALID_REQUEST, 'jsonrpc 字段必须为 "2.0"')

    method = message.get("method")
    if not isinstance(method, str):
        raise McpError(INVALID_REQUEST, "method 必须是字符串")

    message_id = message.get("id")
    is_notification = "id" not in message
    if not is_notification and not isinstance(message_id, (str, int)):
        raise McpError(INVALID_REQUEST, "id 必须是字符串或数字")

    params = _require_object(message.get("params"), "params")

    if is_notification:
        # Notifications get no reply at all, including for methods we ignore.
        return None

    def reply(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    if method == "initialize":
        return reply(
            {
                "protocolVersion": negotiate_protocol(params.get("protocolVersion")),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": "twscrape X 只读接口",
                    "version": server_version,
                },
                "instructions": (
                    "twscrape 通过本地账号池只读访问 X 的公开数据："
                    "搜索推文、读取用户资料与时间线、遍历关注关系、查看账号池健康。"
                    "所有工具都不会发帖、点赞或修改任何数据。"
                    "关注者/关注列表按页返回，用 next_cursor 翻页。"
                ),
            }
        )

    if method == "ping":
        return reply({})

    if method == "tools/list":
        return reply({"tools": tool_catalog()})

    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise McpError(INVALID_PARAMS, "name 必须是非空字符串")
        if name not in {tool["name"] for tool in tool_catalog()}:
            raise McpError(INVALID_PARAMS, f"未知工具: {name}")
        arguments = _require_object(params.get("arguments"), "arguments")
        try:
            return reply(_result(await service.call(name, arguments)))
        except McpToolError as error:
            return reply(_tool_failure(str(error)))
        except XApiNotFoundError as error:
            return reply(_tool_failure(str(error)))
        except XApiUnavailableError as error:
            return reply(_tool_failure(f"{error}（reason: {error.reason}）"))
        except NoAccountError:
            # A drained pool is a temporary condition the model can retry, not a
            # broken request - keep it a tool error so the client sees why.
            return reply(_tool_failure("账号池暂时没有可用账号，请稍后重试"))
        except ValueError as error:
            return reply(_tool_failure(str(error)))

    raise McpError(METHOD_NOT_FOUND, f"未知方法: {method}")


def error_response(message_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}
