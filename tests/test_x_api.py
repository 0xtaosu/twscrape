import json
from pathlib import Path
from typing import Any, cast

import pytest

from twscrape.accounts_pool import NoAccountError
from twscrape.models import Tweet, parse_tweets, parse_user
from twscrape.x_api import (
    XApiNotFoundError,
    XApiService,
    XApiUnavailableError,
    parse_bool,
    parse_by,
    parse_cursor,
    parse_limit,
    parse_uid,
    tweet_to_dict,
    user_to_dict,
)


class MockResponse:
    def __init__(self, source: str | dict[str, Any]):
        self.source = source

    def json(self):
        if isinstance(self.source, dict):
            return self.source
        path = Path(__file__).parent / "mocked-data" / self.source
        return json.loads(path.read_text())


def sample_user():
    user = parse_user(cast(Any, MockResponse("raw_user_by_login.json")))
    assert user is not None
    return user


def sample_tweet() -> Tweet:
    return next(parse_tweets(cast(Any, MockResponse("raw_search.json"))))


@pytest.mark.parametrize(
    ("value", "expected"), [(None, 20), ("1", 1), ("200", 200), ("true", None)]
)
def test_parse_limit(value: str | None, expected: int | None):
    if expected is None:
        with pytest.raises(ValueError):
            parse_limit(value)
    else:
        assert parse_limit(value) == expected


@pytest.mark.parametrize("value", ["0", "201", "-1"])
def test_parse_limit_range(value: str):
    with pytest.raises(ValueError):
        parse_limit(value)


@pytest.mark.parametrize(
    ("value", "expected"), [(None, False), ("true", True), ("1", True), ("false", False)]
)
def test_parse_bool(value: str | None, expected: bool):
    assert parse_bool(value) is expected


def test_serializers_return_json_safe_public_shapes():
    user = user_to_dict(sample_user())
    tweet = tweet_to_dict(sample_tweet())

    assert user["username"] == "XDevelopers"
    assert isinstance(user["id"], str)
    assert tweet["id"]
    assert tweet["user"]["username"]
    assert "cookies" not in json.dumps({"user": user, "tweet": tweet})
    json.dumps({"user": user, "tweet": tweet})


# raw_following.json is a real captured page; its size is what X actually returns.
PAGE_SIZE = 60


class GraphPage:
    """One upstream social-graph response plus the cursor that follows it."""

    def __init__(self, source: str | dict[str, Any], cursor: str | None):
        self.source = source
        self.cursor = cursor

    def json(self):
        obj = MockResponse(self.source).json()
        obj["__cursor"] = self.cursor
        return obj


class FakeAPI:
    def __init__(
        self,
        tweets: list[Tweet],
        user=None,
        unavailable: dict[str, str] | None = None,
        pages: list["GraphPage"] | None = None,
    ):
        self.tweets = tweets
        self.user = user
        self.unavailable = unavailable
        self.closed = False
        self.graph_method = None
        self.graph_uid = None
        self.graph_kv: dict[str, Any] = {}
        self.pages: list[GraphPage] = pages if pages is not None else []
        self.lookups: list[tuple[str, str]] = []

    async def user_by_id_raw(self, uid: int):
        self.lookups.append(("id", str(uid)))
        return await self._user_response()

    async def _user_response(self):
        if self.unavailable is not None:
            return MockResponse(
                {
                    "data": {
                        "user": {"result": {"__typename": "UserUnavailable", **self.unavailable}}
                    }
                }
            )
        if self.user is None:
            return MockResponse({"data": {"user": {"result": {}}}})
        return MockResponse("raw_user_by_login.json")

    async def user_by_login_raw(self, username: str):
        self.lookups.append(("username", username))
        return await self._user_response()

    async def search(self, query: str, limit: int):
        try:
            for tweet in self.tweets:
                yield tweet
        finally:
            self.closed = True

    def _get_cursor(self, obj: dict[str, Any], cursor_type: str = "Bottom") -> str | None:
        value = obj.get("__cursor")
        return value if isinstance(value, str) else None

    async def followers_raw(self, user_id: int, limit: int, kv=None):
        self.graph_method, self.graph_uid, self.graph_kv = "followers", user_id, dict(kv or {})
        try:
            for page in self.pages:
                yield page
        finally:
            self.closed = True

    async def following_raw(self, user_id: int, limit: int, kv=None):
        self.graph_method, self.graph_uid, self.graph_kv = "following", user_id, dict(kv or {})
        try:
            for page in self.pages:
                yield page
        finally:
            self.closed = True


async def test_search_enforces_exact_limit_and_closes_generator(pool_mock):
    fake_api = FakeAPI([sample_tweet(), sample_tweet()])
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    result = await service.search("python", limit=1)

    assert result["query"] == "python"
    assert result["count"] == 1
    assert len(result["tweets"]) == 1
    assert fake_api.closed is True


async def test_user_not_found(pool_mock):
    service = XApiService(pool_mock)
    service.api = cast(Any, FakeAPI([]))

    with pytest.raises(XApiNotFoundError):
        await service.user("missing")


async def test_suspended_user_is_reported_as_unavailable(pool_mock):
    service = XApiService(pool_mock)
    service.api = cast(
        Any,
        FakeAPI([], unavailable={"message": "User is suspended", "reason": "Suspended"}),
    )

    with pytest.raises(XApiUnavailableError) as caught:
        await service.user("suspended-user")

    assert str(caught.value) == 'User "suspended-user" is suspended'
    assert caught.value.reason == "Suspended"


@pytest.mark.parametrize("kind", ["followers", "following"])
async def test_social_graph_returns_page_and_cursor(pool_mock, kind: str):
    fake_api = FakeAPI([], user=sample_user(), pages=[GraphPage("raw_following.json", "cur-1")])
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    result = await getattr(service, kind)("xdevelopers", limit=PAGE_SIZE)

    assert result["kind"] == kind
    assert result["count"] == PAGE_SIZE
    assert len(result["users"]) == PAGE_SIZE
    assert result["next_cursor"] == "cur-1"
    assert fake_api.graph_method == kind
    assert fake_api.closed is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "username"), ("username", "username"), ("login", "username"), ("id", "id"), ("uid", "id")],
)
def test_parse_by(value: str | None, expected: str):
    assert parse_by(value) == expected


@pytest.mark.parametrize("value", ["", "handle", "ID2"])
def test_parse_by_rejects_unknown(value: str):
    with pytest.raises(ValueError):
        parse_by(value)


@pytest.mark.parametrize("value", ["abc", "0", "-1", ""])
def test_parse_uid_rejects_invalid(value: str):
    with pytest.raises(ValueError):
        parse_uid(value)


async def test_user_lookup_by_id(pool_mock):
    fake_api = FakeAPI([], user=sample_user())
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    result = await service.user("1472481088304193541", by="id")

    assert result["username"] == "XDevelopers"
    assert fake_api.lookups == [("id", "1472481088304193541")]


async def test_social_graph_by_id_skips_user_lookup(pool_mock):
    fake_api = FakeAPI([], user=sample_user(), pages=[GraphPage("raw_following.json", None)])
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    result = await service.following("1472481088304193541", limit=2, by="id", skip_user=True)

    assert result["user"] is None
    assert result["count"] == PAGE_SIZE
    assert fake_api.graph_uid == 1472481088304193541
    assert fake_api.lookups == []


async def test_social_graph_by_username_still_resolves_user(pool_mock):
    fake_api = FakeAPI([], user=sample_user(), pages=[GraphPage("raw_following.json", None)])
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    result = await service.following("xdevelopers", limit=2, skip_user=True)

    assert result["user"] is not None
    assert fake_api.lookups == [("username", "xdevelopers")]


async def test_social_graph_by_id_rejects_non_numeric(pool_mock):
    service = XApiService(pool_mock)
    service.api = cast(Any, FakeAPI([], user=sample_user()))

    with pytest.raises(ValueError):
        await service.following("not-a-number", limit=2, by="id", skip_user=True)


async def test_following_batch_preserves_order_and_isolates_user_errors(pool_mock, monkeypatch):
    service = XApiService(pool_mock)
    seen: list[str] = []

    async def social_graph(ident, limit, kind, by, skip_user, cursor=None):
        seen.append(ident)
        assert (limit, kind, by, skip_user) == (50, "following", "id", True)
        if ident == "2":
            raise XApiUnavailableError('User "2" is suspended', "Suspended")
        return {"users": [{"id": ident}], "count": 1, "next_cursor": f"c-{ident}"}

    monkeypatch.setattr(service, "_social_graph", social_graph)
    result = await service.following_batch([1, 2, 3], 50)

    assert seen == ["1", "2", "3"]
    assert result["results"] == [
        {"id": "1", "ok": True, "users": [{"id": "1"}], "count": 1, "next_cursor": "c-1"},
        {
            "id": "2",
            "ok": False,
            "error": 'User "2" is suspended',
            "status": 403,
            "reason": "suspended",
        },
        {"id": "3", "ok": True, "users": [{"id": "3"}], "count": 1, "next_cursor": "c-3"},
    ]


async def test_following_batch_stops_on_pool_exhaustion(pool_mock, monkeypatch):
    service = XApiService(pool_mock)
    seen: list[str] = []

    async def social_graph(ident, limit, kind, by, skip_user, cursor=None):
        seen.append(ident)
        if ident == "2":
            raise NoAccountError("pool exhausted")
        return {"users": [], "count": 0, "next_cursor": None}

    monkeypatch.setattr(service, "_social_graph", social_graph)

    with pytest.raises(NoAccountError):
        await service.following_batch([1, 2, 3], 50)
    assert seen == ["1", "2"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("  ", None), (" c1 ", "c1")],
)
def test_parse_cursor(value: str | None, expected: str | None):
    assert parse_cursor(value) == expected


@pytest.mark.parametrize("value", ["x" * 501, "has space", "has\tstop", "nul\x00byte"])
def test_parse_cursor_rejects_malformed(value: str):
    with pytest.raises(ValueError):
        parse_cursor(value)


async def test_social_graph_forwards_cursor_and_page_size_upstream(pool_mock):
    fake_api = FakeAPI([], user=sample_user(), pages=[GraphPage("raw_following.json", None)])
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    await service.following("1", limit=50, by="id", skip_user=True, cursor="cur-abc")

    assert fake_api.graph_kv["cursor"] == "cur-abc"
    # limit doubles as the page size asked of X - without it every request would
    # be built from the 20-per-page default and cost extra round trips.
    assert fake_api.graph_kv["count"] == 50


async def test_social_graph_omits_cursor_on_first_page(pool_mock):
    fake_api = FakeAPI([], user=sample_user(), pages=[GraphPage("raw_following.json", None)])
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    await service.following("1", limit=20, by="id", skip_user=True)

    assert "cursor" not in fake_api.graph_kv


async def test_social_graph_stops_at_page_boundary_without_dropping_users(pool_mock):
    # limit is smaller than one upstream page. Cutting the page at limit and then
    # returning that page's cursor would strand the rest of the page forever, so
    # the whole page comes back and count overshoots limit.
    pages = [GraphPage("raw_following.json", "cur-1"), GraphPage("raw_following.json", "cur-2")]
    fake_api = FakeAPI([], user=sample_user(), pages=pages)
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    result = await service.following("1", limit=5, by="id", skip_user=True)

    assert result["count"] == PAGE_SIZE
    assert result["next_cursor"] == "cur-1", "cursor must follow the last page actually returned"


async def test_social_graph_accumulates_pages_until_limit(pool_mock):
    pages = [GraphPage("raw_following.json", "cur-1"), GraphPage("raw_following.json", "cur-2")]
    fake_api = FakeAPI([], user=sample_user(), pages=pages)
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    result = await service.following("1", limit=PAGE_SIZE + 1, by="id", skip_user=True)

    # the fixture is one captured page repeated, so ids dedupe - what matters is
    # that a second page was consumed and its cursor is the one handed back
    assert result["next_cursor"] == "cur-2"


async def test_social_graph_reports_end_of_list_as_null_cursor(pool_mock):
    fake_api = FakeAPI([], user=sample_user(), pages=[GraphPage("raw_following.json", None)])
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    result = await service.following("1", limit=200, by="id", skip_user=True)

    # limit not reached, but X has no further cursor - that is the end of the list
    assert result["next_cursor"] is None
    assert result["count"] == PAGE_SIZE


async def test_social_graph_empty_result_has_null_cursor(pool_mock):
    fake_api = FakeAPI([], user=sample_user(), pages=[])
    service = XApiService(pool_mock)
    service.api = cast(Any, fake_api)

    result = await service.following("1", limit=20, by="id", skip_user=True)

    assert result["users"] == []
    assert result["count"] == 0
    assert result["next_cursor"] is None


def _renumbered_page(offset: int, size: int) -> dict[str, Any]:
    """A copy of the captured page holding `size` users with ids offset by `offset`.

    Lets a test build several *distinct* pages out of the one real fixture, so
    paging through them proves no user is skipped at a page boundary.
    """
    import copy

    obj = copy.deepcopy(MockResponse("raw_following.json").json())
    block = obj["data"]["user"]["result"]["timeline"]["timeline"]["instructions"]
    block = next(x for x in block if isinstance(x, dict) and isinstance(x.get("entries"), list))
    users = [e for e in block["entries"] if e["entryId"].startswith("user-")]
    others = [e for e in block["entries"] if not e["entryId"].startswith("user-")]

    kept = []
    for i, entry in enumerate(users[:size]):
        entry = copy.deepcopy(entry)
        new_id = str(1_000_000 + offset + i)
        entry["entryId"] = f"user-{new_id}"
        result = entry["content"]["itemContent"]["user_results"]["result"]
        result["rest_id"] = new_id
        result["id"] = new_id
        kept.append(entry)
    block["entries"] = kept + others
    return obj


async def test_walking_every_page_loses_no_users(pool_mock):
    # The whole point of the cursor: a following list larger than one page must be
    # retrievable in full, in order, without gaps or repeats.
    layout = [(0, 10, "cur-1"), (100, 10, "cur-2"), (200, 7, None)]
    pages = [GraphPage(_renumbered_page(offset, size), cursor) for offset, size, cursor in layout]
    sent_cursors: list[str | None] = []

    class PagingAPI(FakeAPI):
        async def following_raw(self, user_id: int, limit: int, kv=None):
            cursor = (kv or {}).get("cursor")
            sent_cursors.append(cursor)
            start = [None, "cur-1", "cur-2"].index(cursor)
            for page in pages[start:]:
                yield page

    service = XApiService(pool_mock)
    service.api = cast(Any, PagingAPI([], user=sample_user()))

    collected: list[str] = []
    cursor: str | None = None
    for _ in range(len(layout) + 1):
        result = await service.following("1", limit=10, by="id", skip_user=True, cursor=cursor)
        collected.extend(user["id"] for user in result["users"])
        cursor = result["next_cursor"]
        if cursor is None:
            break

    expected = [str(1_000_000 + o + i) for o, size, _ in layout for i in range(size)]
    assert collected == expected
    assert len(collected) == len(set(collected)), "a user was returned twice"
    assert sent_cursors == [None, "cur-1", "cur-2"]
    assert cursor is None, "walk must terminate on a null cursor"
