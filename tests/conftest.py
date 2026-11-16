from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def fixed_ptax_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
    monkeypatch.setattr("app.bacen.datetime", Clock)


@pytest.fixture
def ptax():
    def payload(sell="5.20", buy="5.10", timestamp="2026-11-11 12:00:00"):
        return {"value": [{"cotacaoVenda": sell, "cotacaoCompra": buy,
                           "dataHoraCotacao": timestamp, "tipoBoletim": "Fechamento"}]}
    return payload
