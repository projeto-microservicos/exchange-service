import asyncio
from datetime import date
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest

from app.bacen import fetch_quote
from app.main import create_app

HEADERS = {"id-account": str(uuid4())}


@pytest.fixture
def closing_ptax():
    def payload(sell="5.20", buy="5.10", timestamp="2026-11-11 12:00:00"):
        return {"value": [{"cotacaoVenda": sell, "cotacaoCompra": buy,
                           "dataHoraCotacao": timestamp, "tipoBoletim": "Fechamento"}]}
    return payload


def closing_quote(transport, source="USD", target="BRL"):
    async def request():
        async with httpx.AsyncClient(transport=transport, base_url="http://ptax/") as client:
            return await fetch_quote(client, source, target, today=date(2026, 11, 16))
    return asyncio.run(request())


def test_cross_rate_uses_brl_quotes_with_correct_bid_ask(closing_ptax):
    seen = []
    def provider(request):
        currency = request.url.params["@moeda"]
        seen.append(currency)
        payload = closing_ptax("5.20", "5.10") if currency == "'USD'" else closing_ptax("6.30", "6.20")
        return httpx.Response(200, json=payload)
    result = closing_quote(httpx.MockTransport(provider), "USD", "EUR")
    assert float(result["sell"]) == pytest.approx(5.20 / 6.20)
    assert float(result["buy"]) == pytest.approx(5.10 / 6.30)
    assert result["sell"] >= result["buy"] > 0
    assert sorted(seen) == ["'EUR'", "'USD'"]


def test_last_business_day_is_used_and_intraday_rows_are_ignored(closing_ptax):
    old = closing_ptax("5.20", "5.10", "2026-11-12 13:00:00")["value"]
    latest = closing_ptax("5.40", "5.30", "2026-11-13 13:05:00")["value"]
    intraday = closing_ptax("6.00", "5.90", "2026-11-16 10:00:00")["value"]
    intraday[0]["tipoBoletim"] = "Abertura"
    # Deliberately out of order: the adapter must select a valid latest closing date.
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"value": latest + intraday + old}))
    result = closing_quote(transport)
    assert float(result["sell"]) == 5.40
    assert result["date"] == "2026-11-13 13:05:00"


def test_cross_rate_uses_last_closing_day_shared_by_both_currencies(closing_ptax):
    def provider(request):
        if request.url.params["@moeda"] == "'USD'":
            rows = closing_ptax("99", "98", "2026-11-13 13:00:00")["value"] + closing_ptax("5.2", "5.1")["value"]
        else:
            rows = closing_ptax("6.3", "6.2")["value"]
        return httpx.Response(200, json={"value": rows})
    result = closing_quote(httpx.MockTransport(provider), "USD", "EUR")
    assert result["date"] == "2026-11-11 12:00:00"
    assert float(result["sell"]) == pytest.approx(5.2 / 6.2)


def test_currencies_without_ptax_coverage_are_rejected_before_external_calls():
    def forbidden(request):
        pytest.fail("Unsupported currencies must not contact BACEN")
    with TestClient(create_app(httpx.MockTransport(forbidden))) as client:
        for pair in ("USD/CNY", "NZD/USD", "CNY/CNY"):
            assert client.get("/exchanges/" + pair, headers=HEADERS).status_code == 422


@pytest.mark.parametrize("timestamp", ["2026-10-01 13:00:00", "2026-11-17 13:00:00"])
def test_quotes_outside_the_bounded_date_window_are_not_returned(timestamp, closing_ptax):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=closing_ptax(timestamp=timestamp)))
    with pytest.raises(HTTPException) as error:
        closing_quote(transport)
    assert error.value.status_code == 503


def test_cross_rate_does_not_mix_quotes_from_different_days(closing_ptax):
    def provider(request):
        timestamp = "2026-11-13 13:00:00" if request.url.params["@moeda"] == "'USD'" else "2026-11-12 13:00:00"
        return httpx.Response(200, json=closing_ptax(timestamp=timestamp))
    with pytest.raises(HTTPException) as error:
        closing_quote(httpx.MockTransport(provider), "USD", "EUR")
    assert error.value.status_code == 503


def test_invalid_bacen_json_is_a_service_unavailable_response():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="not json"))
    with TestClient(create_app(transport)) as client:
        assert client.get("/exchanges/USD/BRL", headers=HEADERS).status_code == 503


def test_old_awesomeapi_cache_namespace_is_never_read():
    class Cache:
        async def hgetall(self, key):
            assert key == "exchange:ptax:v1:USD:BRL"
            return {"sell": "5.2", "buy": "5.1", "date": "2026-11-13 13:00:00"}
        async def aclose(self):
            pass
    with TestClient(create_app()) as client:
        client.app.state.cache = Cache()
        assert client.get("/exchanges/USD/BRL", headers=HEADERS).status_code == 200
