"""PTAX stub shared by the tests: answers CotacaoMoedaPeriodo like the BCB (JSON with a "value" list)."""
import httpx
import pytest

USD = [(5.7803, 5.7809, "2026-11-17 10:11:13.349613"), (5.7845, 5.7851, "2026-11-17 13:07:26.01516"),
       (5.7917, 5.7923, "2026-11-17 13:07:25.819873")]  # the latest bulletin is the 2nd (13:07:26)
EUR = [(6.7001, 6.7035, "2026-11-17 13:07:26.01516")]


def ptax_handler(rates, calls=None, fail=lambda code: False):
    """rates: {"USD": [(buy, sell, dataHoraCotacao), ...]}; calls collects the currency of every request."""
    def handler(request):
        assert "CotacaoMoedaPeriodo" in request.url.path
        code = request.url.params["@moeda"].strip("'")
        if calls is not None:
            calls.append(code)
        if fail(code):
            return httpx.Response(500)
        rows = [{"cotacaoCompra": b, "cotacaoVenda": s, "dataHoraCotacao": d} for b, s, d in rates.get(code, [])]
        return httpx.Response(200, json={"value": rows})
    return handler


@pytest.fixture
def ptax():
    return ptax_handler
