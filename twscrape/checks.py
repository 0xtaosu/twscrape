"""主动探测账号可用性：代理连通性、X 会话存活、GQL 端到端抓取。

这些探针刻意绕开 ``AccountsPool``/``QueueClient``：

* 池子会自己挑账号（``get_for_queue_or_wait``），指定测 A 很可能跑到 B 上；
* 检测是只读旁路，不占 lock、不写 stats、不会因为一次失败就把账号踢下线。

打 X 的探针用 ``Account.make_client()`` 构造，和真实抓取共享同一条代理解析、
UA、cookie/csrf 路径 —— 否则测出"代理通"而抓取照样挂，等于白测。代理探针是唯一的
例外：它打的是第三方回显服务，只能带代理和 UA，绝不能带凭据（见 ``_clean_client``）。
想让检测结果落到账号状态上，得显式 ``apply=True``。
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Sequence
from urllib.parse import urlsplit

from .account import Account, has_required_cookies
from .accounts_pool import AccountsPool
from .api import GQL_FEATURES, GQL_URL, OP_UserByScreenName
from .http import ConnectError, HttpClient, HttpError, NetworkError, Response, format_error
from .http import make_client as make_http_client
from .logger import logger
from .queue_client import Ctx
from .utils import encode_params, safe_proxy_display, utc
from .xclid import XClIdAccountError

DEFAULT_PROBE_URL = "https://api.ipify.org?format=json"
X_LIVE_URL = "https://api.x.com/1.1/account/settings.json"
DEFAULT_GQL_TARGET = "x"

PROBE_KINDS = ("proxy", "x_live", "gql")
DEFAULT_PROBES = ("proxy", "x_live")

DEFAULT_TIMEOUT = 15.0
DEFAULT_CONCURRENCY = 5
MAX_CONCURRENCY = 20
MAX_RUNS = 20

PROBE_LABELS = {"proxy": "代理", "x_live": "会话", "gql": "抓取"}

REASON_LABELS = {
    "ok": "正常",
    "skipped": "已跳过",
    "not_found": "账号不存在",
    "session_missing": "缺少会话",
    "session_expired": "会话失效",
    "banned": "账号被封",
    "rate_limited": "限流中",
    "proxy_unreachable": "代理不通",
    "network_error": "网络异常",
    "timeout": "超时",
    "probe_failed": "探测服务异常",
    "gql_outdated": "GQL 特性过期",
    "no_data": "没有返回数据",
    "error": "未知错误",
}

# 代理层挂了，后面的探针必然是同一个死因，没必要再等一遍超时
_PROXY_FATAL = {"proxy_unreachable", "network_error", "timeout"}
# 只有这两种是账号自己的问题，才允许 apply 写回账号状态
_APPLY_REASONS = {"session_expired", "banned"}


def probe_url() -> str:
    return os.getenv("TWS_PROBE_URL", "").strip() or DEFAULT_PROBE_URL


def gql_target() -> str:
    return os.getenv("TWS_PROBE_SCREEN_NAME", "").strip() or DEFAULT_GQL_TARGET


def reason_label(reason: str) -> str:
    return REASON_LABELS.get(reason, reason)


def parse_probes(value: Any) -> list[str]:
    """接受 ``"proxy,x_live"`` 或 ``["proxy", "x_live"]``，保持 PROBE_KINDS 的顺序。"""
    if value is None:
        return list(DEFAULT_PROBES)
    if isinstance(value, str):
        items = [x.strip() for x in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
    else:
        raise ValueError("probes 必须是字符串或数组")

    items = [x for x in items if x]
    if not items:
        return list(DEFAULT_PROBES)
    if unknown := [x for x in items if x not in PROBE_KINDS]:
        raise ValueError(f"未知探针: {', '.join(sorted(set(unknown)))}")
    return [x for x in PROBE_KINDS if x in items]


def parse_concurrency(value: Any) -> int:
    if value is None:
        return DEFAULT_CONCURRENCY
    try:
        num = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("concurrency 必须是整数") from error
    if not 1 <= num <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency 必须在 1 和 {MAX_CONCURRENCY} 之间")
    return num


@dataclass
class ProbeResult:
    probe: str
    status: str  # ok | failed | skipped
    reason: str
    detail: str
    latency_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "probe_label": PROBE_LABELS.get(self.probe, self.probe),
            "status": self.status,
            "reason": self.reason,
            "reason_label": reason_label(self.reason),
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "extra": dict(self.extra),
        }


@dataclass
class AccountCheck:
    username: str
    state: str = "pending"  # pending | running | done
    ok: bool = False
    reason: str = "pending"
    detail: str = ""
    probes: list[ProbeResult] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "state": self.state,
            "ok": self.ok,
            "reason": self.reason,
            "reason_label": reason_label(self.reason),
            "detail": self.detail,
            "probes": [x.to_dict() for x in self.probes],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "applied": self.applied,
        }


def _ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)


def _clean_client(acc: Account) -> HttpClient:
    """代理探针专用：只带代理和 UA，绝不带凭据。

    ``Account.make_client()`` 的 cookie 是无域的，``authorization`` / ``x-csrf-token``
    又是客户端级默认头 —— 拿它去打第三方回显服务，等于把整个会话原样送出去。
    """
    seed = int(hashlib.sha256(acc.username.encode()).hexdigest()[:8], 16)
    return make_http_client(
        proxy=acc.resolve_proxy(),
        headers={"user-agent": acc.user_agent},
        cookies={},
        seed=seed,
    )


def _secrets(acc: Account) -> list[str]:
    """这个账号身上不该出现在检测结果里的字符串。

    异常文案是会带原始值的（httpx 的 LocalProtocolError 会把非法请求头连值一起
    拼进消息），而检测结果要进 dashboard 响应和日志。
    """
    values = [acc.cookies.get(x, "") for x in ("auth_token", "ct0")]
    values.append(acc.password)
    values.append(acc.email_password)
    if proxy := acc.resolve_proxy():
        parts = urlsplit(proxy)
        values += [parts.password or "", parts.username or ""]
    # 太短的值（比如 cookie-only 账号的占位密码 "_"）满篇都是，替换了反而毁文案
    return [x for x in values if isinstance(x, str) and len(x) >= 6]


def _scrub(text: str, secrets: list[str]) -> str:
    for value in secrets:
        text = text.replace(value, "***")
    return text


def _ok(
    probe: str, detail: str, latency_ms: int, extra: dict[str, Any] | None = None
) -> ProbeResult:
    return ProbeResult(probe, "ok", "ok", detail, latency_ms, extra or {})


def _fail(probe: str, reason: str, detail: str, latency_ms: int | None = None) -> ProbeResult:
    return ProbeResult(probe, "failed", reason, detail, latency_ms)


def _skip(probe: str, detail: str) -> ProbeResult:
    return ProbeResult(probe, "skipped", "skipped", detail)


def _errors(res: Any) -> tuple[set[int], str]:
    """从 X 的响应体里抽出错误码和拼好的错误文案。"""
    if not isinstance(res, dict):
        return set(), ""
    raw = res.get("errors")
    if not isinstance(raw, list):
        return set(), ""

    codes: set[int] = set()
    messages: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            codes.add(int(item.get("code", -1)))
        except (TypeError, ValueError):
            pass
        message = item.get("message")
        if isinstance(message, str) and message:
            messages.append(message)
    return codes, "; ".join(dict.fromkeys(messages))


def _json(rep: Response) -> Any:
    try:
        return rep.json()
    except Exception:
        return None


def _classify_x(probe: str, rep: Response, latency_ms: int) -> ProbeResult | None:
    """把 X 的响应映射成失败原因；返回 None 表示这一层没问题。"""
    codes, message = _errors(_json(rep))
    status = rep.status_code

    if 326 in codes:
        return _fail(probe, "banned", message or "账号被 X 限制访问", latency_ms)
    if 336 in codes:
        return _fail(probe, "gql_outdated", "GQL_FEATURES 已过期，需要更新 api.py", latency_ms)
    # 限流判断必须排在 401/403 前面：X 经常用 403 包着 code 88 返回限流，
    # 反过来就会把一个健康账号判成会话失效 —— apply=True 时直接被停用。
    if 88 in codes or status == 429:
        return _fail(probe, "rate_limited", message or "触发速率限制", latency_ms)
    if 32 in codes or status in (401, 403):
        return _fail(probe, "session_expired", message or f"HTTP {status}", latency_ms)
    if status != 200:
        return _fail(probe, "error", message or f"HTTP {status}", latency_ms)
    if message:
        return _fail(probe, "error", message, latency_ms)
    return None


async def _probe_proxy(acc: Account, clt: HttpClient) -> ProbeResult:
    started = time.monotonic()
    rep = await clt.get(probe_url())  # clt 是 _clean_client 造的，见 _CLEAN_PROBES
    latency = _ms(started)

    if rep.status_code != 200:
        return _fail("proxy", "probe_failed", f"探测服务返回 HTTP {rep.status_code}", latency)

    data = _json(rep)
    exit_ip = ""
    if isinstance(data, dict):
        for key in ("ip", "origin", "query"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                exit_ip = value.strip()
                break
    if not exit_ip:
        exit_ip = (rep.text or "").strip()[:64]

    # 只回显脱敏后的代理，凭据不能进 dashboard 响应
    proxy = safe_proxy_display(acc.resolve_proxy()) or "直连"
    detail = f"出口 IP {exit_ip}" if exit_ip else "连通，但没解析出出口 IP"
    return _ok("proxy", detail, latency, {"exit_ip": exit_ip, "proxy": proxy})


async def _probe_x_live(acc: Account, clt: HttpClient) -> ProbeResult:
    if not has_required_cookies(acc.cookies):
        return _fail("x_live", "session_missing", "缺少 auth_token / ct0")

    started = time.monotonic()
    rep = await clt.get(X_LIVE_URL)
    latency = _ms(started)

    if failure := _classify_x("x_live", rep, latency):
        return failure

    data = _json(rep)
    screen_name = data.get("screen_name") if isinstance(data, dict) else None
    if not isinstance(screen_name, str) or not screen_name:
        # 200 但没有账号信息 —— 拦截页/空响应都长这样，不能当会话有效。
        # 用 no_data 而不是 session_expired：证据不足以支撑 apply 停用账号。
        return _fail("x_live", "no_data", "HTTP 200 但没返回账号信息", latency)

    extra: dict[str, Any] = {"screen_name": screen_name}
    detail = f"会话有效 · @{screen_name}"
    # cookie 是从别的账号复制过来的，这种错配单看抓取日志根本发现不了
    if screen_name.casefold() != acc.username.casefold():
        extra["screen_name_mismatch"] = True
        detail = f"会话有效，但 cookie 属于 @{screen_name}"
    return _ok("x_live", detail, latency, extra)


async def _probe_gql(acc: Account, clt: HttpClient) -> ProbeResult:
    if not has_required_cookies(acc.cookies):
        return _fail("gql", "session_missing", "缺少 auth_token / ct0")

    target = gql_target()
    # Ctx 只是"账号 + 客户端"，和池子无关，借它拿到 x-client-transaction-id 的生成与重试
    ctx = Ctx(acc, clt, proxy=acc.resolve_proxy())
    params = encode_params(
        {
            "variables": {"screen_name": target, "withSafetyModeUserFields": True},
            "features": GQL_FEATURES,
        }
    )

    started = time.monotonic()
    rep = await ctx.req("GET", f"{GQL_URL}/{OP_UserByScreenName}", params=params)
    latency = _ms(started)

    if failure := _classify_x("gql", rep, latency):
        return failure

    # X 会用 200 + {"data": {"user": {"result": null}}} 表示查不到，
    # 只看 user 这一层是真的（{"result": None} 也真），得挖到 result。
    payload = _json(rep)
    container = payload.get("data") if isinstance(payload, dict) else None
    user = container.get("user") if isinstance(container, dict) else None
    if not (user.get("result") if isinstance(user, dict) else None):
        return _fail("gql", "no_data", f"@{target} 查询返回空数据", latency)
    return _ok("gql", f"成功抓取 @{target}", latency, {"target": target})


_PROBE_FNS: dict[str, Callable[[Account, HttpClient], Coroutine[Any, Any, ProbeResult]]] = {
    "proxy": _probe_proxy,
    "x_live": _probe_x_live,
    "gql": _probe_gql,
}

# 打第三方回显服务的探针，只能拿无凭据客户端
_CLEAN_PROBES = frozenset({"proxy"})


async def _run_probe(probe: str, acc: Account, clt: HttpClient, timeout: float) -> ProbeResult:
    started = time.monotonic()
    try:
        return await asyncio.wait_for(_PROBE_FNS[probe](acc, clt), timeout)
    except asyncio.TimeoutError:
        return _fail(probe, "timeout", f"{timeout:g}s 内没有响应", _ms(started))
    except XClIdAccountError as error:
        # 生成 transaction-id 时拿到的是未登录版 web app —— 会话没了
        return _fail(probe, "session_expired", format_error(error), _ms(started))
    except ConnectError as error:
        return _fail(probe, "proxy_unreachable", format_error(error), _ms(started))
    except NetworkError as error:
        return _fail(probe, "network_error", format_error(error), _ms(started))
    except HttpError as error:
        return _fail(probe, "error", format_error(error), _ms(started))
    except Exception as error:
        logger.debug(f"check probe {probe} failed for {acc.username}: {error!r}")
        return _fail(probe, "error", format_error(error), _ms(started))


def _summarize(probes: list[ProbeResult]) -> tuple[bool, str, str]:
    for item in probes:
        if item.status == "failed":
            return False, item.reason, f"{PROBE_LABELS.get(item.probe, item.probe)}: {item.detail}"
    if not probes or all(x.status == "skipped" for x in probes):
        return False, "skipped", "没有可执行的探针"
    return True, "ok", " · ".join(x.detail for x in probes if x.status == "ok")


async def check_account(
    acc: Account,
    *,
    probes: Sequence[str] = DEFAULT_PROBES,
    timeout: float = DEFAULT_TIMEOUT,
) -> AccountCheck:
    """跑一遍探针。永不抛异常 —— 失败都会变成一条 ``AccountCheck``。"""
    selected = parse_probes(list(probes))
    result = AccountCheck(username=acc.username, state="running", started_at=utc.now().isoformat())

    # 按凭据需求分两个客户端，且按需建：只测代理时不该造出带凭据的那个
    clients: dict[str, HttpClient] = {}
    try:
        for probe in selected:
            if any(x.reason in _PROXY_FATAL for x in result.probes):
                result.probes.append(_skip(probe, "上游网络不通，已跳过"))
                continue

            kind = "clean" if probe in _CLEAN_PROBES else "auth"
            if kind not in clients:
                try:
                    clients[kind] = _clean_client(acc) if kind == "clean" else acc.make_client()
                except Exception as error:
                    # 连客户端都建不起来（代理地址畸形之类），这就是这一层的结论
                    logger.debug(f"check client setup failed for {acc.username}: {error!r}")
                    result.probes.append(_fail(probe, "error", format_error(error)))
                    continue
            result.probes.append(await _run_probe(probe, acc, clients[kind], timeout))
    finally:
        for clt in clients.values():
            try:
                await clt.aclose()
            except Exception:  # 关闭失败不该影响检测结论
                logger.debug(f"check client close failed for {acc.username}")

    # 异常文案会带上原始请求头/代理串，统一在出口抹掉
    secrets = _secrets(acc)
    for item in result.probes:
        item.detail = _scrub(item.detail, secrets)

    result.ok, result.reason, result.detail = _summarize(result.probes)
    result.state = "done"
    result.finished_at = utc.now().isoformat()
    return result


@dataclass
class CheckRun:
    id: str
    usernames: list[str]
    probes: list[str]
    apply: bool
    concurrency: int
    state: str = "queued"  # queued | running | done | cancelled
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    cancelled: bool = False
    results: dict[str, AccountCheck] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        states = [x.state for x in self.results.values()]
        done = [x for x in self.results.values() if x.state == "done"]
        return {
            "total": len(self.usernames),
            "pending": states.count("pending"),
            "running": states.count("running"),
            "done": len(done),
            "ok": sum(1 for x in done if x.ok),
            "failed": sum(1 for x in done if not x.ok),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "probes": list(self.probes),
            "apply": self.apply,
            "concurrency": self.concurrency,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary(),
            "accounts": [self.results[name].to_dict() for name in self.usernames],
        }


def _crashed(username: str, error: BaseException, acc: Account | None = None) -> AccountCheck:
    """把一个逃出来的异常变成这个账号的检测结论。"""
    detail = format_error(error) if isinstance(error, Exception) else repr(error)
    return AccountCheck(
        username=username,
        state="done",
        reason="error",
        detail=_scrub(detail, _secrets(acc)) if acc is not None else detail,
        finished_at=utc.now().isoformat(),
    )


class CheckRegistry:
    """内存里的检测任务登记表。

    刻意不落库：检测结果的价值就是"此刻能不能用"，重启后重测一次也才几秒钟，
    省掉一个 sqlite migration 就少一个和上游冲突的点。

    看板是多线程 HTTP + 单个常驻事件循环，任务在循环线程里跑、快照在 HTTP
    线程里读，所以所有读写都过同一把锁，并且只交出 dict 拷贝。
    """

    def __init__(self, pool: AccountsPool, *, max_runs: int = MAX_RUNS):
        self._pool = pool
        self._max_runs = max_runs
        self._lock = threading.Lock()
        self._runs: dict[str, CheckRun] = {}
        self._order: list[str] = []
        self._last: dict[str, AccountCheck] = {}

    def create(
        self,
        usernames: Sequence[str],
        *,
        probes: Sequence[str] = DEFAULT_PROBES,
        apply: bool = False,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> CheckRun:
        names = list(dict.fromkeys(x.strip() for x in usernames if x and x.strip()))
        if not names:
            raise ValueError("没有需要检测的账号")

        run = CheckRun(
            id=uuid.uuid4().hex[:12],
            usernames=names,
            probes=parse_probes(list(probes)),
            apply=bool(apply),
            concurrency=parse_concurrency(concurrency),
            created_at=utc.now().isoformat(),
        )
        run.results = {name: AccountCheck(username=name) for name in names}

        with self._lock:
            self._runs[run.id] = run
            self._order.append(run.id)
            self._evict_locked()
            if len(self._order) > self._max_runs:
                # 淘汰完还超限 = 上限内全是没跑完的。既然不能丢它们，就别收新的，
                # 否则这张表会被没完没了的新任务撑爆。
                self._order.remove(run.id)
                self._runs.pop(run.id, None)
                raise ValueError("正在进行的检测任务过多，请稍后再试")
        return run

    def _evict_locked(self) -> None:
        """超上限时淘汰最旧的**已结束**任务。

        淘汰一个还在跑的任务，等于它的进度查不到、取消也调不动，而它的协程
        还在往这张表里写 —— 宁可暂时超出上限。
        """
        while len(self._order) > self._max_runs:
            victim = next(
                (
                    x
                    for x in self._order
                    if (run := self._runs.get(x)) is None or run.state in ("done", "cancelled")
                ),
                None,
            )
            if victim is None:
                return
            self._order.remove(victim)
            self._runs.pop(victim, None)

    async def execute(self, run: CheckRun) -> None:
        with self._lock:
            if run.cancelled:
                run.state = "cancelled"
                run.finished_at = utc.now().isoformat()
                return
            run.state = "running"
            run.started_at = utc.now().isoformat()

        accounts: dict[str, Account] = {}
        try:
            # 一次取全，而不是每个任务各查一次：并发去争 db.py 那个模块级
            # asyncio.Lock 没有任何好处，账号快照对一次检测来说也足够新。
            accounts = {x.username: x for x in await self._pool.get_all()}
            semaphore = asyncio.Semaphore(run.concurrency)

            async def one(username: str) -> None:
                async with semaphore:
                    with self._lock:
                        # 取消判定和置 running 必须在同一把锁里，否则会在取消
                        # 生效之后又放一个探针出去
                        if run.cancelled:
                            return
                        run.results[username].state = "running"
                        run.results[username].started_at = utc.now().isoformat()
                    result = await self._check_one(accounts.get(username), username, run)
                    with self._lock:
                        run.results[username] = result
                        self._last[username] = result

            # return_exceptions：一个账号炸了不能把整批带走，更不能让 finally
            # 先跑完、剩下的协程继续往一个"已完成"的任务里写
            outcomes = await asyncio.gather(
                *(one(x) for x in run.usernames), return_exceptions=True
            )
            for name, outcome in zip(run.usernames, outcomes):
                if isinstance(outcome, BaseException):
                    logger.warning(f"check task crashed for {name}: {outcome!r}")
                    with self._lock:
                        run.results[name] = _crashed(name, outcome, accounts.get(name))
        except Exception as error:
            # 取号本身失败（数据库挂了之类）：整批给个结论，别留一屏 pending
            logger.warning(f"check run {run.id} failed: {error!r}")
            with self._lock:
                for name in run.usernames:
                    if run.results[name].state != "done":
                        run.results[name] = _crashed(name, error, accounts.get(name))
        finally:
            with self._lock:
                run.state = "cancelled" if run.cancelled else "done"
                run.finished_at = utc.now().isoformat()

    async def _check_one(self, acc: Account | None, username: str, run: CheckRun) -> AccountCheck:
        if acc is None:
            return AccountCheck(
                username=username,
                state="done",
                reason="not_found",
                detail="账号已不存在",
                finished_at=utc.now().isoformat(),
            )

        result = await check_account(acc, probes=run.probes)
        if run.apply and result.reason in _APPLY_REASONS:
            try:
                await self._pool.mark_inactive(username, result.detail[:120])
                result.applied = True
            except Exception as error:
                logger.warning(f"check apply failed for {username}: {error!r}")
        return result

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run.to_dict() if run else None

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            for run_id in reversed(self._order):
                run = self._runs.get(run_id)
                if run is not None:
                    return run.to_dict()
        return None

    def cancel(self, run_id: str) -> bool:
        """标记取消。已经发出的探针会自己跑完 —— 强杀请求换不来更干净的状态。"""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.state in ("done", "cancelled"):
                return False
            run.cancelled = True
            if run.state == "queued":
                run.state = "cancelled"
                run.finished_at = utc.now().isoformat()
            return True

    def is_busy(self) -> bool:
        with self._lock:
            return any(x.state in ("queued", "running") for x in self._runs.values())

    def account_states(self) -> dict[str, dict[str, Any]]:
        """给账号列表用：``username -> {"checking": bool, "last_check": {...} | None}``。"""
        with self._lock:
            checking = {
                name
                for run in self._runs.values()
                if run.state in ("queued", "running")
                for name, item in run.results.items()
                if item.state in ("pending", "running")
            }
            states: dict[str, dict[str, Any]] = {
                name: {"checking": False, "last_check": item.to_dict()}
                for name, item in self._last.items()
            }

        for name in checking:
            entry = states.setdefault(name, {"checking": False, "last_check": None})
            entry["checking"] = True
        return states


# ---------------------------------------------------------------------------
# python -m twscrape.checks
#
# 故意不接到 cli.py 上：那个文件和上游一字不差，动它等于给每次同步上游多加一个
# 手工合并点。逻辑全在这里，命令行只是薄薄一层。
# ---------------------------------------------------------------------------


def _format_line(item: AccountCheck) -> str:
    mark = "✓" if item.ok else "✗"
    head = f"{mark} {item.username:<20}"
    if item.ok:
        parts = [f"{PROBE_LABELS.get(x.probe, x.probe)} {x.detail}" for x in item.probes if x.ok]
        tail = " · ".join(parts)
    else:
        tail = f"{reason_label(item.reason)} — {item.detail}"
    latency = [x.latency_ms for x in item.probes if x.latency_ms is not None]
    suffix = f"  [{sum(latency)}ms]" if latency else ""
    return f"{head} {tail}{suffix}"


async def _amain(args: argparse.Namespace) -> int:
    pool = AccountsPool(args.db)
    if args.all or not args.username:
        usernames = [x.username for x in await pool.get_all()]
    else:
        usernames = list(args.username)

    if not usernames:
        print("没有账号可检测", file=sys.stderr)
        return 1

    registry = CheckRegistry(pool)
    run = registry.create(
        usernames,
        probes=parse_probes(args.probes),
        apply=args.apply,
        concurrency=parse_concurrency(args.concurrency),
    )
    await registry.execute(run)

    if args.json:
        print(json.dumps(run.to_dict(), ensure_ascii=False, indent=2))
    else:
        for name in run.usernames:
            print(_format_line(run.results[name]))
        summary = run.summary()
        print(f"\n{summary['ok']} 正常 · {summary['failed']} 异常 · 共 {summary['total']} 个账号")

    return 0 if run.summary()["failed"] == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m twscrape.checks",
        description="检测账号的代理连通性与 X 会话状态",
    )
    parser.add_argument("username", nargs="*", help="要检测的账号，留空等同 --all")
    parser.add_argument("--db", default="accounts.db", help="Accounts database file")
    parser.add_argument("--all", action="store_true", help="检测全部账号")
    parser.add_argument(
        "--probes",
        default=",".join(DEFAULT_PROBES),
        help=f"逗号分隔，可选 {', '.join(PROBE_KINDS)}（gql 会消耗真实抓取额度）",
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="并发数")
    parser.add_argument("--apply", action="store_true", help="把失效/被封的账号写回为停用")
    parser.add_argument("--json", action="store_true", help="输出 JSON")

    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_amain(args)))
    except ValueError as error:
        print(f"错误: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
