from uuid import uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import create_app


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("CACHE_ENABLED", "false")
    monkeypatch.setenv("MESSAGING_ENABLED", "false")


def client(handler):
    return TestClient(create_app(httpx.MockTransport(handler)))


def test_quote_has_authenticated_identity_and_bid_ask():
    def provider(request):
        assert request.url.path == "/json/last/USD-BRL"
        return httpx.Response(200, json={"USDBRL": {"ask": "5.20", "bid": "5.10", "create_date": "2026-11-11 12:00:00"}})
    account = str(uuid4())
    with client(provider) as c:
        r = c.get("/exchanges/USD/BRL", headers={"id-account": account})
        assert r.status_code == 200
        assert r.json() == {"sell": 5.2, "buy": 5.1, "date": "2026-11-11 12:00:00", "id-account": account}


def test_reverse_pair_and_same_currency():
    def provider(request):
        if request.url.path.endswith("BRL-USD"):
            return httpx.Response(404)
        return httpx.Response(200, json={"USDBRL": {"ask": "5", "bid": "4", "create_date": "2026-11-11 12:00:00"}})
    with client(provider) as c:
        headers = {"id-account": str(uuid4())}
        assert c.get("/exchanges/BRL/USD", headers=headers).json()["sell"] == 0.25
        assert c.get("/exchanges/USD/USD", headers=headers).json()["buy"] == 1
        assert c.get("/exchanges/XXX/XXX", headers=headers).status_code == 422
        assert c.get("/exchanges/USD/BRL").status_code == 422


@pytest.mark.parametrize("status,payload,expected", [(404, {}, 422), (429, {}, 503), (500, {}, 503),
    (200, {"USDBRL": {"ask": "NaN", "bid": "1", "create_date": "x"}}, 503), (200, {}, 503)])
def test_provider_failure_is_not_a_fake_quote(status, payload, expected):
    with client(lambda r: httpx.Response(status, json=payload)) as c:
        assert c.get("/exchanges/USD/BRL", headers={"id-account": str(uuid4())}).status_code == expected


def test_timeout():
    def provider(request):
        raise httpx.ReadTimeout("timed out", request=request)
    with client(provider) as c:
        assert c.get("/exchanges/USD/BRL", headers={"id-account": str(uuid4())}).status_code == 503


def test_cached_quote_never_reuses_another_accounts_identity():
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"USDBRL": {"ask": "5.20", "bid": "5.10", "create_date": "2026-11-11 12:00:00"}})
    class Cache:
        data = {}
        async def hgetall(self, key): return self.data.get(key, {})
        def pipeline(self, transaction): return self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def hset(self, key, mapping): self.pending = (key, mapping)
        def expire(self, key, seconds): assert seconds > 0
        async def execute(self): self.data[self.pending[0]] = self.pending[1]
        async def aclose(self): pass
    with client(provider) as c:
        c.app.state.cache = Cache()
        alice, bob = str(uuid4()), str(uuid4())
        first = c.get("/exchanges/USD/BRL", headers={"id-account": alice})
        second = c.get("/exchanges/USD/BRL", headers={"id-account": bob})
        assert first.json()["id-account"] == alice
        assert second.json()["id-account"] == bob
        assert first.json()["sell"] == second.json()["sell"] == 5.2
        assert len(calls) == 1


def test_redis_failure_falls_back_to_provider():
    from redis.exceptions import RedisError
    class BrokenCache:
        async def hgetall(self, key): raise RedisError("unavailable")
        def pipeline(self, transaction): raise RedisError("unavailable")
        async def aclose(self): pass
    with client(lambda r: httpx.Response(200, json={"USDBRL": {"ask": "5", "bid": "4", "create_date": "2026-11-11 12:00:00"}})) as c:
        c.app.state.cache = BrokenCache()
        assert c.get("/exchanges/USD/BRL", headers={"id-account": str(uuid4())}).status_code == 200
