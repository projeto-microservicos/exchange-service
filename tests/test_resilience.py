import asyncio
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.messaging import QuoteEvents

QUOTE = {"sell": Decimal("5.2"), "buy": Decimal("5.1"), "date": "2026-11-16 12:00:00"}
HEADERS = {"id-account": "00000000-0000-0000-0000-000000000001"}
DATE = "2026-11-17 13:07:26.01516"


@pytest.mark.parametrize("buy,sell,date", [
    (0, 0, DATE), (-2, -1, DATE), (2, 1, DATE), ("Infinity", 1, DATE), ("NaN", 1, DATE),
    (True, True, DATE), (1, 2, None), (1, 2, "invalid-date"), ("abc", 2, DATE),
])
def test_invalid_ptax_data_returns_service_unavailable(buy, sell, date):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"value": [
        {"cotacaoCompra": buy, "cotacaoVenda": sell, "dataHoraCotacao": date}]}))
    with TestClient(create_app(transport, settings=Settings())) as client:
        assert client.get("/exchanges/USD/BRL", headers=HEADERS).status_code == 503


def test_provider_timeout_bounds_a_slow_ptax():
    async def provider(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"value": []})
    with TestClient(create_app(httpx.MockTransport(provider), settings=Settings(provider_timeout=0.02))) as client:
        assert client.get("/exchanges/EUR/USD", headers=HEADERS).status_code == 503


def test_disabled_flags_never_initialize_optional_dependencies(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled infrastructure must not be initialized")
    monkeypatch.setattr("app.main.Redis.from_url", forbidden)
    monkeypatch.setattr("app.main.QuoteEvents", forbidden)
    with TestClient(create_app(settings=Settings())) as client:
        assert client.get("/ready").status_code == 200
        assert client.get("/exchanges/usd/usd", headers=HEADERS).json()["sell"] == 1
        assert client.get("/exchanges/USD/BRL", headers={"id-account": "invalid"}).status_code == 422


def test_corrupted_cache_is_replaced_without_reusing_identity():
    class Cache:
        data = {"sell": "NaN", "buy": "1", "date": QUOTE["date"], "id-account": str(uuid4())}
        async def hgetall(self, key): return self.data
        def pipeline(self, transaction): return self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def hset(self, key, mapping): self.data = mapping
        def expire(self, key, seconds): assert seconds == 60
        async def execute(self): pass
        async def aclose(self): pass
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"value": [
        {"cotacaoCompra": 5.1, "cotacaoVenda": 5.2, "dataHoraCotacao": DATE}]}))
    with TestClient(create_app(transport, settings=Settings())) as client:
        client.app.state.cache = cache = Cache()
        result = client.get("/exchanges/USD/BRL", headers=HEADERS)
        assert result.status_code == 200
        assert result.json()["id-account"] == HEADERS["id-account"]
        assert set(cache.data) == {"sell", "buy", "date"}


def test_quote_events_retry_the_same_id_and_bound_memory():
    async def scenario():
        calls = []
        async def sender(event):
            calls.append(dict(event))
            if len(calls) == 1:
                raise ConnectionError("broker unavailable")
        events = QuoteEvents("unused", queue_size=1, sender=sender, retry_delay=0)
        events.offer("USD", "BRL", QUOTE)
        events.offer("EUR", "BRL", QUOTE)
        assert events.queue.qsize() == 1
        await events.start()
        await asyncio.wait_for(events.queue.join(), timeout=1)
        await events.close()
        assert len(calls) == 2
        assert calls[0]["eventId"] == calls[1]["eventId"]
        assert "id-account" not in calls[0]
        assert calls[0]["sell"] == "5.2"
    asyncio.run(scenario())


def test_failed_telemetry_does_not_block_later_events():
    async def scenario():
        calls = []
        async def sender(event):
            calls.append(event["source"])
            if event["source"] == "USD":
                raise ConnectionError("unavailable")
        events = QuoteEvents("unused", attempts=2, sender=sender, retry_delay=0)
        events.offer("USD", "BRL", QUOTE)
        events.offer("EUR", "BRL", QUOTE)
        await events.start()
        await asyncio.wait_for(events.queue.join(), timeout=1)
        await events.close()
        assert calls == ["USD", "USD", "EUR"]
    asyncio.run(scenario())


@pytest.mark.parametrize("environment,value", [("CACHE_ENABLED", "maybe"), ("EXCHANGE_CACHE_TTL", "0"), ("EXCHANGE_PROVIDER_TIMEOUT", "NaN"),
                                               ("EXCHANGE_RATE_TTL", "0"), ("EXCHANGE_RATE_RETRY", "-1")])
def test_invalid_settings_fail_early(monkeypatch, environment, value):
    monkeypatch.setenv(environment, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_metrics_are_bounded_and_openapi_documents_identity():
    with TestClient(create_app(settings=Settings())) as client:
        assert client.get("/exchanges/USD/USD", headers=HEADERS).status_code == 200
        metrics = client.get("/metrics").text
        assert 'exchange_requests_total{endpoint="exchanges",status="200"}' in metrics
        assert HEADERS["id-account"] not in metrics
        schema = client.get("/openapi.json").json()
        quote = schema["components"]["schemas"]["ExchangeQuote"]
        assert "id-account" in quote["required"]
