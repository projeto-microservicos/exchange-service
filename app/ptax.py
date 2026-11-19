"""PTAX (Banco Central do Brasil) rates, kept in memory, with cross rates through BRL.

PTAX quotes each currency against the real (BRL) and blocks no cloud provider, unlike AwesomeAPI,
which answers 429 from AWS. The approach follows the course's "Exchange API: BACEN service" page:

* one quote per currency in BRL: the latest bulletin of the last 7 days (weekends and holidays have none);
* kept in memory for ``ttl`` seconds; only one request per currency refreshes it, the others wait for it;
* when PTAX fails and an expired quote is in memory, that quote is served ("stale") and PTAX is asked
  again after ``retry`` seconds;
* a pair is a cross rate: with buy ``b`` and sell ``s`` in BRL, FROM -> TO is
  ``buy = b(FROM) / s(TO)`` and ``sell = s(FROM) / b(TO)``. BRL counts as 1.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import logging
import time

import httpx

from app.metrics import PROVIDER_CALLS

logger = logging.getLogger(__name__)

PTAX_URL = "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata"
# Currencies with a PTAX bulletin (GET /Moedas, checked on 17/11/2026), plus the real itself.
PTAX_CURRENCIES = frozenset("AUD CAD CHF DKK EUR GBP JPY NOK SEK USD".split())
SUPPORTED = PTAX_CURRENCIES | {"BRL"}
BRASILIA = timezone(timedelta(hours=-3))  # no daylight saving time since 2019
PLACES = Decimal("0.000001")


class ProviderError(Exception):
    """PTAX did not answer a usable quote."""


@dataclass(frozen=True)
class BrlQuote:
    buy: Decimal   # cotacaoCompra: BRL the market pays for one unit
    sell: Decimal  # cotacaoVenda: BRL the market charges for one unit
    date: str      # bulletin time, "YYYY-MM-DD HH:MM:SS" (Brasília)


BRL = BrlQuote(Decimal(1), Decimal(1), "")


@dataclass
class _Entry:
    quote: BrlQuote
    expires: float


class PtaxRates:
    def __init__(self, client: httpx.AsyncClient, *, ttl: float, retry: float, timeout: float,
                 clock=time.monotonic, today=lambda: datetime.now(BRASILIA).date()):
        self.client, self.ttl, self.retry, self.timeout = client, ttl, retry, timeout
        self.clock, self.today = clock, today
        self.entries: dict[str, _Entry] = {}
        self.locks: dict[str, asyncio.Lock] = {}  # bounded by SUPPORTED

    async def pair(self, source: str, target: str) -> tuple[dict, str]:
        """Rate FROM -> TO and where it came from: "hit", "miss" (PTAX called) or "stale"."""
        (src, a), (dst, b) = await asyncio.gather(self.currency(source), self.currency(target))
        status = "stale" if "stale" in (a, b) else "miss" if "miss" in (a, b) else "hit"
        buy = (src.buy / dst.sell).quantize(PLACES, ROUND_HALF_EVEN)
        sell = (src.sell / dst.buy).quantize(PLACES, ROUND_HALF_EVEN)
        dates = [d for d in (src.date, dst.date) if d]
        # A cross rate is as old as its oldest bulletin.
        return {"sell": sell, "buy": buy, "date": min(dates)}, status

    async def currency(self, code: str) -> tuple[BrlQuote, str]:
        if code == "BRL":
            return BRL, "hit"
        entry = self.entries.get(code)
        if entry and self.clock() < entry.expires:
            return entry.quote, "hit"
        async with self.locks.setdefault(code, asyncio.Lock()):
            # Another request may have refreshed it while this one waited (no stampede on PTAX).
            entry = self.entries.get(code)
            if entry and self.clock() < entry.expires:
                return entry.quote, "hit"
            try:
                quote = await self._fetch(code)
            except ProviderError:
                if entry is None:
                    raise
                logger.warning("PTAX failed for %s; serving the last quote for %ss", code, self.retry)
                entry.expires = self.clock() + self.retry
                return entry.quote, "stale"
            self.entries[code] = _Entry(quote, self.clock() + self.ttl)
            return quote, "miss"

    async def _fetch(self, code: str) -> BrlQuote:
        end = self.today()
        start = end - timedelta(days=7)
        params = {
            "@moeda": f"'{code}'",
            "@dataInicial": f"'{start:%m-%d-%Y}'",
            "@dataFinalCotacao": f"'{end:%m-%d-%Y}'",
            "$format": "json",
            "$select": "cotacaoCompra,cotacaoVenda,dataHoraCotacao",
        }
        path = "/CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.client.get(path, params=params)
            PROVIDER_CALLS.labels(str(response.status_code)).inc()
            response.raise_for_status()
            bulletins = response.json()["value"]
            latest = max(bulletins, key=lambda row: row["dataHoraCotacao"])
            return _quote(latest)
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            # ValueError also covers max() of an empty list: no bulletin in the last 7 days.
            PROVIDER_CALLS.labels("failure").inc()
            logger.warning("PTAX request for %s failed (%s)", code, type(error).__name__)
            raise ProviderError(code) from error


def _quote(row: dict) -> BrlQuote:
    try:
        if isinstance(row["cotacaoCompra"], bool) or isinstance(row["cotacaoVenda"], bool):
            raise ValueError("rates cannot be booleans")
        buy, sell = Decimal(str(row["cotacaoCompra"])), Decimal(str(row["cotacaoVenda"]))
        if not (buy.is_finite() and sell.is_finite() and buy > 0 and sell >= buy):
            raise ValueError("invalid bid/ask")
        date = datetime.fromisoformat(row["dataHoraCotacao"]).strftime("%Y-%m-%d %H:%M:%S")
    except InvalidOperation as error:
        raise ValueError("invalid rate") from error
    return BrlQuote(buy, sell, date)
