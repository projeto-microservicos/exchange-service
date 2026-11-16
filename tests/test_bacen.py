from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import create_app

HEADERS = {"id-account": str(uuid4())}


def test_cross_rate_uses_brl_quotes_with_correct_bid_ask(ptax):
    seen = []
    def provider(request):
        currency = request.url.params["@moeda"]
        seen.append(currency)
        payload = ptax("5.20", "5.10") if currency == "'USD'" else ptax("6.30", "6.20")
        return httpx.Response(200, json=payload)
    with TestClient(create_app(httpx.MockTransport(provider))) as client:
        response = client.get("/exchanges/usd/eur", headers=HEADERS)
        assert response.status_code == 200
        result = response.json()
        assert result["sell"] == pytest.approx(5.20 / 6.20)
        assert result["buy"] == pytest.approx(5.10 / 6.30)
        assert result["sell"] >= result["buy"] > 0
        assert sorted(seen) == ["'EUR'", "'USD'"]


def test_last_business_day_is_used_and_intraday_rows_are_ignored(ptax):
    old = ptax("5.20", "5.10", "2026-11-12 13:00:00")["value"]
    latest = ptax("5.40", "5.30", "2026-11-13 13:05:00")["value"]
    intraday = ptax("6.00", "5.90", "2026-11-16 10:00:00")["value"]
    intraday[0]["tipoBoletim"] = "Abertura"
    # Deliberately out of order: the adapter must select a valid latest closing date.
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"value": latest + intraday + old}))
    with TestClient(create_app(transport)) as client:
        result = client.get("/exchanges/USD/BRL", headers=HEADERS).json()
        assert result["sell"] == 5.40
        assert result["date"] == "2026-11-13 13:05:00"


def test_cross_rate_uses_last_closing_day_shared_by_both_currencies(ptax):
    def provider(request):
        if request.url.params["@moeda"] == "'USD'":
            rows = ptax("99", "98", "2026-11-13 13:00:00")["value"] + ptax("5.2", "5.1")["value"]
        else:
            rows = ptax("6.3", "6.2")["value"]
        return httpx.Response(200, json={"value": rows})
    with TestClient(create_app(httpx.MockTransport(provider))) as client:
        result = client.get("/exchanges/USD/EUR", headers=HEADERS).json()
        assert result["date"] == "2026-11-11 12:00:00"
        assert result["sell"] == pytest.approx(5.2 / 6.2)


def test_currencies_without_ptax_coverage_are_rejected_before_external_calls():
    def forbidden(request):
        pytest.fail("Unsupported currencies must not contact BACEN")
    with TestClient(create_app(httpx.MockTransport(forbidden))) as client:
        for pair in ("USD/CNY", "NZD/USD", "CNY/CNY"):
            assert client.get("/exchanges/" + pair, headers=HEADERS).status_code == 422


@pytest.mark.parametrize("timestamp", ["2026-10-01 13:00:00", "2026-11-17 13:00:00"])
def test_quotes_outside_the_bounded_date_window_are_not_returned(timestamp, ptax):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=ptax(timestamp=timestamp)))
    with TestClient(create_app(transport)) as client:
        assert client.get("/exchanges/USD/BRL", headers=HEADERS).status_code == 503


def test_cross_rate_does_not_mix_quotes_from_different_days(ptax):
    def provider(request):
        timestamp = "2026-11-13 13:00:00" if request.url.params["@moeda"] == "'USD'" else "2026-11-12 13:00:00"
        return httpx.Response(200, json=ptax(timestamp=timestamp))
    with TestClient(create_app(httpx.MockTransport(provider))) as client:
        assert client.get("/exchanges/USD/EUR", headers=HEADERS).status_code == 503


def test_invalid_bacen_json_is_a_service_unavailable_response():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="not json"))
    with TestClient(create_app(transport)) as client:
        assert client.get("/exchanges/USD/BRL", headers=HEADERS).status_code == 503


def test_old_awesomeapi_cache_namespace_is_never_read(ptax):
    class Cache:
        async def hgetall(self, key):
            assert key == "exchange:ptax:v1:USD:BRL"
            return {"sell": "5.2", "buy": "5.1", "date": "2026-11-13 13:00:00"}
        async def aclose(self):
            pass
    with TestClient(create_app()) as client:
        client.app.state.cache = Cache()
        assert client.get("/exchanges/USD/BRL", headers=HEADERS).status_code == 200
