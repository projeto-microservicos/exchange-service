import asyncio
from decimal import Decimal
import re
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.main import create_app
from app.ptax import PtaxRates, ProviderError
from conftest import EUR, USD, ptax_handler


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("CACHE_ENABLED", "false")
    monkeypatch.setenv("MESSAGING_ENABLED", "false")


def client(handler):
    return TestClient(create_app(httpx.MockTransport(handler)))


def headers():
    return {"id-account": str(uuid4())}


def q(value):
    return float(Decimal(value).quantize(Decimal("0.000001")))


def test_quote_is_the_latest_ptax_bulletin_with_the_callers_identity(ptax):
    account = str(uuid4())
    with client(ptax({"USD": USD})) as c:
        r = c.get("/exchanges/USD/BRL", headers={"id-account": account})
        assert r.status_code == 200
        assert r.json() == {"sell": 5.7851, "buy": 5.7845, "date": "2026-11-17 13:07:26", "id-account": account}
        assert r.headers["X-Cache"] == "miss"


def test_cross_rate_goes_through_brl(ptax):
    with client(ptax({"USD": USD, "EUR": EUR})) as c:
        body = c.get("/exchange/USD/EUR", headers=headers()).json()
        # buy = b(USD) / s(EUR); sell = s(USD) / b(EUR)
        assert body["buy"] == q(Decimal("5.7845") / Decimal("6.7035"))
        assert body["sell"] == q(Decimal("5.7851") / Decimal("6.7001"))
        assert body["sell"] >= body["buy"]
        inverse = c.get("/exchange/BRL/USD", headers=headers()).json()
        assert inverse["buy"] == q(1 / Decimal("5.7851")) and inverse["sell"] == q(1 / Decimal("5.7845"))


def cache_count(result):
    return REGISTRY.get_sample_value("exchange_cache_requests_total", {"result": result}) or 0.0


def test_memory_cache_answers_the_second_call_without_ptax(ptax):
    calls = []
    hits, misses = cache_count("hit"), cache_count("miss")  # counters are global to the process
    with client(ptax({"USD": USD, "EUR": EUR}, calls)) as c:
        first = c.get("/exchanges/USD/EUR", headers=headers())
        second = c.get("/exchange/EUR/USD", headers=headers())  # same currencies, other direction
        assert (first.headers["X-Cache"], second.headers["X-Cache"]) == ("miss", "hit")
        assert sorted(calls) == ["EUR", "USD"]
        assert (cache_count("hit") - hits, cache_count("miss") - misses) == (1, 1)


def test_concurrent_requests_for_a_currency_make_one_ptax_call():
    calls = []

    async def scenario():
        async def slow(request):
            calls.append(request.url.params["@moeda"])
            await asyncio.sleep(0.05)
            return ptax_handler({"USD": USD})(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow), base_url="http://ptax") as http:
            rates = PtaxRates(http, ttl=300, retry=30, timeout=1)
            results = await asyncio.gather(*(rates.pair("USD", "BRL") for _ in range(10)))
        return [status for _, status in results]

    statuses = asyncio.run(scenario())
    assert len(calls) == 1
    assert statuses.count("miss") == 1 and statuses.count("hit") == 9


def test_expired_rate_is_served_stale_when_ptax_fails_then_retried(ptax):
    calls, down = [], {"now": False}
    with client(ptax({"USD": USD}, calls, fail=lambda _: down["now"])) as c:
        clock = {"t": 0.0}
        c.app.state.rates.clock = lambda: clock["t"]
        assert c.get("/exchanges/USD/BRL", headers=headers()).headers["X-Cache"] == "miss"

        clock["t"], down["now"] = 301.0, True  # TTL (300 s) expired and PTAX is down
        stale = c.get("/exchanges/USD/BRL", headers=headers())
        assert stale.status_code == 200 and stale.headers["X-Cache"] == "stale"
        assert stale.json()["sell"] == 5.7851

        clock["t"] = 320.0  # inside the 30 s retry window: no new call to PTAX
        assert c.get("/exchanges/USD/BRL", headers=headers()).headers["X-Cache"] == "hit"
        assert len(calls) == 2

        clock["t"], down["now"] = 332.0, False  # retry window over and PTAX back
        assert c.get("/exchanges/USD/BRL", headers=headers()).headers["X-Cache"] == "miss"
        assert len(calls) == 3


@pytest.mark.parametrize("response", [
    httpx.Response(500), httpx.Response(429), httpx.Response(200, json={"value": []}),
    httpx.Response(200, json={}), httpx.Response(200, text="<html>maintenance</html>"),
])
def test_ptax_failure_without_cached_rate_is_503_never_a_fake_quote(response):
    with client(lambda _: response) as c:
        assert c.get("/exchanges/USD/BRL", headers=headers()).status_code == 503


def test_timeout_is_503():
    def provider(request):
        raise httpx.ReadTimeout("timed out", request=request)
    with client(provider) as c:
        assert c.get("/exchanges/USD/BRL", headers=headers()).status_code == 503


def test_only_ptax_currencies_and_the_identity_header_are_accepted(ptax):
    with client(ptax({"USD": USD})) as c:
        assert c.get("/exchanges/ARS/BRL", headers=headers()).status_code == 422  # PTAX has no ARS
        assert c.get("/exchanges/XXX/XXX", headers=headers()).status_code == 422
        assert c.get("/exchanges/USD/BRL").status_code == 422


def test_health_check_needs_no_identity_and_returns_nothing_internal():
    def provider(request):
        raise AssertionError("health-check must not call the provider")
    with client(provider) as c:
        r = c.get("/exchange/health-check")
        assert r.status_code == 200
        assert r.content == b""


def test_singular_path_answers_like_the_plural_one(ptax):
    account = str(uuid4())
    with client(ptax({"USD": USD, "EUR": EUR})) as c:
        singular = c.get("/exchange/USD/EUR", headers={"id-account": account}).json()
        plural = c.get("/exchanges/USD/EUR", headers={"id-account": account}).json()
        assert singular == plural
        assert c.get("/exchange/USD/EUR").status_code == 422


def test_same_currency_is_one_with_the_statement_date_format():
    with client(lambda r: httpx.Response(500)) as c:
        body = c.get("/exchange/brl/BRL", headers=headers()).json()
        assert body["sell"] == body["buy"] == 1
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", body["date"])


def test_redis_quote_never_reuses_another_accounts_identity(ptax):
    calls = []
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
    with client(ptax({"USD": USD}, calls)) as c:
        c.app.state.cache = Cache()
        alice, bob = str(uuid4()), str(uuid4())
        first = c.get("/exchanges/USD/BRL", headers={"id-account": alice})
        second = c.get("/exchanges/USD/BRL", headers={"id-account": bob})
        assert first.json()["id-account"] == alice and second.json()["id-account"] == bob
        assert first.json()["sell"] == second.json()["sell"] == 5.7851
        assert "id-account" not in Cache.data["exchange:ptax:v1:USD:BRL"]
        assert len(calls) == 1


def test_redis_failure_falls_back_to_memory_and_ptax(ptax):
    from redis.exceptions import RedisError
    class BrokenCache:
        async def hgetall(self, key): raise RedisError("unavailable")
        def pipeline(self, transaction): raise RedisError("unavailable")
        async def aclose(self): pass
    with client(ptax({"USD": USD})) as c:
        c.app.state.cache = BrokenCache()
        assert c.get("/exchanges/USD/BRL", headers=headers()).status_code == 200


def test_quote_event_only_when_ptax_was_called(ptax):
    with client(ptax({"USD": USD})) as c:
        offered = []
        class Events:
            def offer(self, source, target, quote): offered.append((source, target))
            async def close(self): pass
        c.app.state.events = Events()
        c.get("/exchanges/USD/BRL", headers=headers())
        c.get("/exchanges/USD/BRL", headers=headers())
        assert offered == [("USD", "BRL")]


def test_provider_error_is_raised_when_nothing_is_cached():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)),
                                     base_url="http://ptax") as http:
            await PtaxRates(http, ttl=300, retry=30, timeout=1).currency("USD")
    with pytest.raises(ProviderError):
        asyncio.run(scenario())
