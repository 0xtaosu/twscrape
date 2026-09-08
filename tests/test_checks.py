import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from twscrape import checks
from twscrape.account import Account
from twscrape.accounts_pool import AccountsPool
from twscrape.api import OP_UserByScreenName
from twscrape.checks import (
    CheckRegistry,
    check_account,
    parse_concurrency,
    parse_probes,
)
from twscrape.http import ConnectError, NetworkError
from twscrape.xclid import XClIdAccountError

from .mock_http import MockClient


async def _no_sleep(_secs):
    pass


@pytest.fixture
async def acc_mock(pool_mock: AccountsPool, monkeypatch):
    await pool_mock.add_account_cookies("user1", "auth_token=token1; ct0=csrf1")
    await pool_mock.set_active("user1", True)

    clt = MockClient()
    monkeypatch.setattr(Account, "make_client", lambda self, proxy=None: clt)
    # 代理探针走的是无凭据客户端，不经过 Account.make_client
    monkeypatch.setattr(checks, "_clean_client", lambda acc: clt)
    yield pool_mock, clt


def test_parse_probes_normalizes_and_validates():
    assert parse_probes(None) == ["proxy", "x_live"]
    assert parse_probes("") == ["proxy", "x_live"]
    assert parse_probes("x_live,proxy") == ["proxy", "x_live"]
    assert parse_probes(["gql"]) == ["gql"]
    assert parse_probes("gql,gql") == ["gql"]

    with pytest.raises(ValueError):
        parse_probes("nope")
    with pytest.raises(ValueError):
        parse_probes(42)


def test_parse_concurrency_bounds():
    assert parse_concurrency(None) == 5
    assert parse_concurrency("3") == 3

    for bad in (0, 21, "x"):
        with pytest.raises(ValueError):
            parse_concurrency(bad)


async def test_proxy_probe_reports_exit_ip(acc_mock):
    pool, clt = acc_mock
    clt.add_response(json={"ip": "203.0.113.7"})

    result = await check_account(await pool.get("user1"), probes=["proxy"])
    assert result.ok is True
    assert result.reason == "ok"
    assert result.state == "done"

    probe = result.probes[0]
    assert probe.probe == "proxy"
    assert probe.extra["exit_ip"] == "203.0.113.7"
    assert probe.extra["proxy"] == "直连"
    assert probe.latency_ms is not None


async def test_proxy_probe_never_leaks_credentials(acc_mock):
    pool, clt = acc_mock
    account = await pool.get("user1")
    account.proxy = "http://proxyuser:proxy-secret@10.0.0.1:8080"
    await pool.save(account)
    clt.add_response(json={"ip": "203.0.113.7"})

    result = await check_account(await pool.get("user1"), probes=["proxy"])
    assert result.probes[0].extra["proxy"] == "http://10.0.0.1:8080"
    assert "proxy-secret" not in str(result.to_dict())
    assert "proxyuser" not in str(result.to_dict())


async def test_proxy_probe_flags_bad_probe_service(acc_mock):
    pool, clt = acc_mock
    clt.add_response(status_code=502, text="bad gateway")

    result = await check_account(await pool.get("user1"), probes=["proxy"])
    assert result.ok is False
    assert result.reason == "probe_failed"


async def test_unreachable_proxy_skips_later_probes(acc_mock):
    pool, clt = acc_mock
    clt.add_exception(ConnectError("proxy refused"))

    result = await check_account(await pool.get("user1"), probes=["proxy", "x_live"])
    assert result.ok is False
    assert result.reason == "proxy_unreachable"
    assert [x.status for x in result.probes] == ["failed", "skipped"]
    # 跳过的探针不该再消耗一次请求
    assert clt._queue == []


async def test_network_error_is_not_blamed_on_the_account(acc_mock):
    pool, clt = acc_mock
    clt.add_exception(NetworkError("read timeout"))

    result = await check_account(await pool.get("user1"), probes=["proxy"])
    assert result.reason == "network_error"


async def test_x_live_probe_reports_screen_name(acc_mock):
    pool, clt = acc_mock
    clt.add_response(json={"screen_name": "user1"})

    result = await check_account(await pool.get("user1"), probes=["x_live"])
    assert result.ok is True
    assert result.probes[0].extra["screen_name"] == "user1"


async def test_x_live_probe_detects_cookie_from_another_account(acc_mock):
    pool, clt = acc_mock
    clt.add_response(json={"screen_name": "someone_else"})

    result = await check_account(await pool.get("user1"), probes=["x_live"])
    assert result.ok is True
    assert result.probes[0].extra["screen_name_mismatch"] is True
    assert "someone_else" in result.probes[0].detail


@pytest.mark.parametrize(
    "status_code,json_body,expected",
    [
        (401, None, "session_expired"),
        (403, None, "session_expired"),
        (
            200,
            {"errors": [{"code": 32, "message": "Could not authenticate you"}]},
            "session_expired",
        ),
        (200, {"errors": [{"code": 326, "message": "Denied by access control"}]}, "banned"),
        (429, None, "rate_limited"),
        (200, {"errors": [{"code": 88, "message": "Rate limit exceeded"}]}, "rate_limited"),
        (
            200,
            {"errors": [{"code": 34, "message": "Sorry, that page does not exist"}]},
            "transaction_id",
        ),
        (500, None, "error"),
    ],
)
async def test_x_live_probe_maps_x_responses(acc_mock, status_code, json_body, expected):
    pool, clt = acc_mock
    clt.add_response(status_code=status_code, json=json_body)

    result = await check_account(await pool.get("user1"), probes=["x_live"])
    assert result.ok is False
    assert result.reason == expected


async def test_x_live_probe_sends_transaction_id(acc_mock):
    """X 对 settings.json 校验 x-client-transaction-id，裸 clt.get() 会被回 404 + code 34。"""
    pool, clt = acc_mock
    clt.add_response(json={"screen_name": "user1"})

    result = await check_account(await pool.get("user1"), probes=["x_live"])
    assert result.ok is True

    method, url, kwargs = clt.calls[0]
    assert (method, url) == ("GET", checks.X_LIVE_URL)
    assert kwargs["headers"]["x-client-transaction-id"] == "mocked-clid"


async def test_x_live_404_is_not_blamed_on_the_session(acc_mock, monkeypatch):
    pool, clt = acc_mock
    # Ctx.req 会换生成器重试三次，全 404 之后抛 AbortReqError
    for _ in range(3):
        clt.add_response(status_code=404, json={"errors": [{"code": 34, "message": "nope"}]})
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _no_sleep)

    result = await check_account(await pool.get("user1"), probes=["x_live"])
    assert result.ok is False
    assert result.reason == "transaction_id"
    # 证据只说明 transaction-id 这一层坏了，不足以判账号死亡 —— apply 不能停用它
    assert result.reason not in checks._APPLY_REASONS


async def test_apply_leaves_transaction_id_failures_alone(acc_mock, monkeypatch):
    pool, clt = acc_mock
    for _ in range(3):
        clt.add_response(status_code=404, json={})
    monkeypatch.setattr("twscrape.queue_client.asyncio.sleep", _no_sleep)

    registry = CheckRegistry(pool)
    run = registry.create(["user1"], probes=["x_live"], apply=True)
    await registry.execute(run)

    assert run.results["user1"].reason == "transaction_id"
    assert run.results["user1"].applied is False
    assert (await pool.get("user1")).active is True


async def test_probes_need_a_session(pool_mock: AccountsPool, monkeypatch):
    await pool_mock.add_account("nosession", "pass", "email", "email_pass")
    monkeypatch.setattr(Account, "make_client", lambda self, proxy=None: MockClient())

    result = await check_account(await pool_mock.get("nosession"), probes=["x_live", "gql"])
    assert result.reason == "session_missing"
    assert [x.reason for x in result.probes] == ["session_missing", "session_missing"]


async def test_gql_probe_hits_the_real_scraping_surface(acc_mock):
    pool, clt = acc_mock
    clt.add_response(json={"data": {"user": {"result": {"__typename": "User"}}}})

    result = await check_account(await pool.get("user1"), probes=["gql"])
    assert result.ok is True
    assert result.probes[0].extra["target"] == "x"


async def test_gql_probe_reports_empty_data(acc_mock):
    pool, clt = acc_mock
    clt.add_response(json={"data": {}})

    result = await check_account(await pool.get("user1"), probes=["gql"])
    assert result.reason == "no_data"


async def test_gql_probe_surfaces_outdated_features(acc_mock):
    pool, clt = acc_mock
    clt.add_response(
        json={"errors": [{"code": 336, "message": "The following features cannot be null"}]}
    )

    result = await check_account(await pool.get("user1"), probes=["gql"])
    assert result.reason == "gql_outdated"


async def test_registry_run_tracks_progress_and_summary(acc_mock):
    pool, clt = acc_mock
    clt.add_response(json={"ip": "203.0.113.7"})

    registry = CheckRegistry(pool)
    run = registry.create(["user1"], probes=["proxy"])

    queued = registry.get(run.id)
    assert queued is not None
    assert queued["state"] == "queued"
    assert queued["summary"] == {
        "total": 1,
        "pending": 1,
        "running": 0,
        "done": 0,
        "ok": 0,
        "failed": 0,
    }
    assert queued["accounts"][0]["state"] == "pending"

    await registry.execute(run)

    done = registry.get(run.id)
    assert done is not None
    assert done["state"] == "done"
    assert done["summary"]["ok"] == 1
    assert done["accounts"][0]["ok"] is True
    assert registry.latest() == done
    assert registry.is_busy() is False


async def test_registry_reports_missing_accounts(acc_mock):
    pool, _ = acc_mock
    registry = CheckRegistry(pool)
    run = registry.create(["ghost"], probes=["proxy"])
    await registry.execute(run)

    assert run.results["ghost"].reason == "not_found"
    stored = registry.get(run.id)
    assert stored is not None
    assert stored["summary"]["failed"] == 1


async def test_registry_dedupes_and_rejects_empty_targets(acc_mock):
    pool, _ = acc_mock
    registry = CheckRegistry(pool)
    assert registry.create(["user1", "user1", " "], probes=["proxy"]).usernames == ["user1"]

    with pytest.raises(ValueError):
        registry.create([" ", ""])


async def test_registry_cancel_stops_pending_accounts(acc_mock):
    pool, clt = acc_mock
    registry = CheckRegistry(pool)
    run = registry.create(["user1"], probes=["proxy"])

    assert registry.cancel(run.id) is True
    await registry.execute(run)

    stored = registry.get(run.id)
    assert stored is not None
    assert stored["state"] == "cancelled"
    assert run.results["user1"].state == "pending"
    assert registry.cancel(run.id) is False
    assert registry.cancel("nope") is False


async def test_registry_keeps_only_recent_runs(acc_mock):
    pool, clt = acc_mock
    registry = CheckRegistry(pool, max_runs=2)

    finished = []
    for _ in range(3):
        clt.add_response(json={"ip": "203.0.113.7"})
        run = registry.create(["user1"], probes=["proxy"])
        await registry.execute(run)
        finished.append(run)

    assert registry.get(finished[0].id) is None
    assert registry.get(finished[1].id) is not None
    assert registry.get(finished[2].id) is not None


async def test_registry_never_evicts_a_running_job(acc_mock):
    """淘汰掉在跑的任务 = 进度查不到、取消调不动，而它还在往表里写。"""
    pool, _ = acc_mock
    registry = CheckRegistry(pool, max_runs=1)

    live = registry.create(["user1"])
    live.state = "running"

    with pytest.raises(ValueError, match="过多"):
        registry.create(["user1"])

    assert registry.get(live.id) is not None

    live.state = "done"
    newer = registry.create(["user1"])
    assert registry.get(live.id) is None
    assert registry.get(newer.id) is not None


async def test_apply_deactivates_dead_sessions_only_when_asked(acc_mock):
    pool, clt = acc_mock
    clt.add_response(status_code=401)

    registry = CheckRegistry(pool)
    run = registry.create(["user1"], probes=["x_live"])
    await registry.execute(run)

    assert run.results["user1"].applied is False
    assert (await pool.get("user1")).active is True

    clt.add_response(status_code=401)
    run = registry.create(["user1"], probes=["x_live"], apply=True)
    await registry.execute(run)

    account = await pool.get("user1")
    assert run.results["user1"].applied is True
    assert account.active is False
    assert account.error_msg is not None


async def test_apply_leaves_rate_limited_accounts_alone(acc_mock):
    pool, clt = acc_mock
    clt.add_response(status_code=429)

    registry = CheckRegistry(pool)
    run = registry.create(["user1"], probes=["x_live"], apply=True)
    await registry.execute(run)

    assert run.results["user1"].reason == "rate_limited"
    assert run.results["user1"].applied is False
    assert (await pool.get("user1")).active is True


async def test_account_states_expose_progress_to_the_dashboard(acc_mock):
    pool, clt = acc_mock
    clt.add_response(json={"ip": "203.0.113.7"})

    registry = CheckRegistry(pool)
    run = registry.create(["user1"], probes=["proxy"])

    assert registry.account_states()["user1"]["checking"] is True
    assert registry.account_states()["user1"]["last_check"] is None
    assert registry.is_busy() is True

    await registry.execute(run)

    state = registry.account_states()["user1"]
    assert state["checking"] is False
    assert state["last_check"]["reason"] == "ok"
    assert state["last_check"]["reason_label"] == "正常"


async def test_run_respects_concurrency_limit(pool_mock: AccountsPool, monkeypatch):
    peak, active = 0, 0

    class SlowClient(MockClient):
        async def request(self, method, url, **kwargs):
            nonlocal peak, active
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.02)
                return await super().request(method, url, **kwargs)
            finally:
                active -= 1

    clients: dict[str, SlowClient] = {}

    def make_client(self, proxy=None):
        clt = clients.setdefault(self.username, SlowClient())
        clt.add_response(json={"ip": "203.0.113.7"})
        return clt

    monkeypatch.setattr(Account, "make_client", make_client)
    for i in range(6):
        await pool_mock.add_account_cookies(f"user{i}", "auth_token=t; ct0=c")

    registry = CheckRegistry(pool_mock)
    run = registry.create([f"user{i}" for i in range(6)], probes=["proxy"], concurrency=2)
    await registry.execute(run)

    assert peak <= 2
    stored = registry.get(run.id)
    assert stored is not None
    assert stored["summary"]["ok"] == 6


async def test_proxy_probe_sends_no_credentials_over_the_wire(pool_mock: AccountsPool, monkeypatch):
    """代理探针打的是第三方回显服务 —— 一个凭据字节都不能出去。

    这条必须走真实的客户端构造：mock 客户端拦在构造之后，看不见真正发出去的头。
    最早的实现直接复用 ``Account.make_client()``，于是每测一次代理，就把
    auth_token / ct0 / Bearer 完整送给 ipify 一次。
    """
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.update({k.lower(): v for k, v in self.headers.items()})
            body = b'{"ip": "203.0.113.7"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("TWS_PROBE_URL", f"http://127.0.0.1:{server.server_port}/")

        await pool_mock.add_account_cookies("user1", "auth_token=SECRET-AUTH; ct0=SECRET-CT0")
        acc = await pool_mock.get("user1")
        assert acc is not None

        result = await check_account(acc, probes=["proxy"])
    finally:
        server.shutdown()
        server.server_close()

    assert result.ok is True, result.detail
    assert result.probes[0].extra["exit_ip"] == "203.0.113.7"

    assert "cookie" not in seen
    assert "authorization" not in seen
    assert "x-csrf-token" not in seen
    assert "SECRET-AUTH" not in " ".join(seen.values())
    assert "SECRET-CT0" not in " ".join(seen.values())
    # 代理和 UA 还是得跟真实抓取一致，否则测的不是同一条出口。
    # acc.user_agent 存的是 "@chrome" 这种别名，真串由客户端按 seed 展开。
    auth_client = acc.make_client()
    try:
        assert seen.get("user-agent") == auth_client.headers["user-agent"]
    finally:
        await auth_client.aclose()


async def test_rate_limit_wrapped_in_403_is_not_a_dead_session(acc_mock):
    """X 会用 403 包着 code 88 返回限流。判成 session_expired 就会停用健康账号。"""
    pool, clt = acc_mock
    clt.add_response(status_code=403, json={"errors": [{"code": 88, "message": "Rate limit"}]})

    result = await check_account(await pool.get("user1"), probes=["x_live"])
    assert result.reason == "rate_limited"
    assert result.reason not in ("session_expired", "banned")  # 即 apply 不会动它


async def test_x_live_200_without_account_info_is_not_a_pass(acc_mock):
    """拦截页/空响应也是 200，不能凭状态码就宣布会话有效。"""
    pool, clt = acc_mock
    clt.add_response(json={})

    result = await check_account(await pool.get("user1"), probes=["x_live"])
    assert result.ok is False
    assert result.reason == "no_data"


async def test_gql_probe_rejects_a_null_user_result(acc_mock):
    """``{"user": {"result": null}}`` 这一层是真值，挖到 result 才知道是空。"""
    pool, clt = acc_mock
    clt.add_response(json={"data": {"user": {"result": None}}})

    result = await check_account(await pool.get("user1"), probes=["gql"])
    assert result.ok is False
    assert result.reason == "no_data"


async def test_gql_probe_queries_the_user_lookup_endpoint(acc_mock):
    pool, clt = acc_mock
    clt.add_response(json={"data": {"user": {"result": {"__typename": "User"}}}})

    await check_account(await pool.get("user1"), probes=["gql"])
    _, url, kwargs = clt.calls[-1]
    assert url.endswith(f"/{OP_UserByScreenName}")
    assert json.loads(kwargs["params"]["variables"])["screen_name"] == "x"
    assert "features" in kwargs["params"]


async def test_logged_out_web_app_is_reported_as_a_dead_session(acc_mock):
    pool, clt = acc_mock
    clt.add_exception(XClIdAccountError("Logged-out X web app"))

    result = await check_account(await pool.get("user1"), probes=["gql"])
    assert result.reason == "session_expired"


async def test_client_setup_failure_becomes_a_result(pool_mock: AccountsPool, monkeypatch):
    """连客户端都建不起来（代理地址畸形之类）也得有结论，不能把异常抛给调用方。"""
    await pool_mock.add_account_cookies("user1", "auth_token=token1; ct0=csrf1")

    def boom(self, proxy=None):
        raise RuntimeError("bad proxy url")

    monkeypatch.setattr(Account, "make_client", boom)

    result = await check_account(await pool_mock.get("user1"), probes=["x_live"])
    assert result.ok is False
    assert result.reason == "error"
    assert "bad proxy url" in result.detail


async def test_probe_detail_never_echoes_account_secrets(pool_mock: AccountsPool, monkeypatch):
    """异常文案会带上原始请求头 —— httpx 的 LocalProtocolError 就是这么干的。"""
    await pool_mock.add_account_cookies("user1", "auth_token=token-abcdef; ct0=csrf-abcdef")
    acc = await pool_mock.get("user1")
    assert acc is not None
    acc.proxy = "http://puser:proxy-secret@10.0.0.1:8080"
    await pool_mock.save(acc)

    clt = MockClient()
    clt.add_exception(ValueError("Illegal header value b'csrf-abcdef' for proxy-secret"))
    monkeypatch.setattr(Account, "make_client", lambda self, proxy=None: clt)

    result = await check_account(await pool_mock.get("user1"), probes=["x_live"])
    blob = str(result.to_dict())
    assert "csrf-abcdef" not in blob
    assert "proxy-secret" not in blob
    assert "***" in blob


async def test_run_survives_a_crashing_account(acc_mock, monkeypatch):
    """一个账号的协程炸了，不能把整批带走，也不能让任务在别人还在写时就收尾。"""
    pool, _ = acc_mock
    await pool.add_account_cookies("user2", "auth_token=token2; ct0=csrf2")

    async def explode(acc, **kwargs):
        if acc.username == "user1":
            raise RuntimeError("kaboom")
        return checks.AccountCheck(
            username=acc.username, state="done", ok=True, reason="ok", detail="fine"
        )

    monkeypatch.setattr(checks, "check_account", explode)

    registry = CheckRegistry(pool)
    run = registry.create(["user1", "user2"], concurrency=1)
    await registry.execute(run)

    stored = registry.get(run.id)
    assert stored is not None
    assert stored["state"] == "done"
    assert stored["summary"] == {
        "total": 2,
        "pending": 0,
        "running": 0,
        "done": 2,
        "ok": 1,
        "failed": 1,
    }
    by_name = {x["username"]: x for x in stored["accounts"]}
    assert "kaboom" in by_name["user1"]["detail"]
    assert by_name["user2"]["ok"] is True


async def test_pool_failure_gives_every_account_a_verdict(acc_mock, monkeypatch):
    """取号本身失败时不能留一屏 pending —— 那个任务永远不会再有人碰。"""
    pool, _ = acc_mock

    async def no_db():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(pool, "get_all", no_db)

    registry = CheckRegistry(pool)
    run = registry.create(["user1"])
    await registry.execute(run)

    stored = registry.get(run.id)
    assert stored is not None
    assert stored["state"] == "done"
    assert stored["summary"]["pending"] == 0
    assert stored["accounts"][0]["reason"] == "error"
    assert "database is gone" in stored["accounts"][0]["detail"]


async def test_cancel_midway_stops_the_accounts_not_started_yet(acc_mock, monkeypatch):
    pool, _ = acc_mock
    for name in ("user2", "user3"):
        await pool.add_account_cookies(name, f"auth_token=token-{name}; ct0=csrf-{name}")

    registry = CheckRegistry(pool)
    run = registry.create(["user1", "user2", "user3"], concurrency=1)

    async def one_then_cancel(acc, **kwargs):
        registry.cancel(run.id)  # 第一个账号跑完就取消，剩下的不该再发探针
        return checks.AccountCheck(
            username=acc.username, state="done", ok=True, reason="ok", detail="fine"
        )

    monkeypatch.setattr(checks, "check_account", one_then_cancel)
    await registry.execute(run)

    stored = registry.get(run.id)
    assert stored is not None
    assert stored["state"] == "cancelled"
    assert stored["summary"]["done"] == 1
    assert stored["summary"]["pending"] == 2
    by_name = {x["username"]: x for x in stored["accounts"]}
    assert by_name["user2"]["state"] == "pending"
    assert by_name["user3"]["state"] == "pending"
