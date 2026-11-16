"""Direct BACEN/PTAX closing quotes, expressed in BRL per unit of currency."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
from urllib.parse import quote, urlencode

import httpx
from fastapi import HTTPException

from app.metrics import PROVIDER_CALLS

# Published by PTAX's Moedas resource; BRL is the mathematical reference currency.
CURRENCIES = frozenset("AUD BRL CAD CHF DKK EUR GBP JPY NOK SEK USD".split())
LOOKBACK_DAYS = 10
ENDPOINT = "CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"


def validate_quote(raw: dict) -> dict:
    try:
        if isinstance(raw["sell"], bool) or isinstance(raw["buy"], bool):
            raise ValueError("Rates cannot be booleans")
        sell, buy = Decimal(raw["sell"]), Decimal(raw["buy"])
        if not all(rate.is_finite() and rate > 0 and math.isfinite(float(rate)) and float(rate) > 0
                   for rate in (sell, buy)) or sell < buy:
            raise ValueError("Invalid bid/ask rates")
        timestamp = raw["date"]
        if not isinstance(timestamp, str):
            raise ValueError("Invalid date")
        datetime.fromisoformat(timestamp)
        return {"sell": sell, "buy": buy, "date": timestamp}
    except (InvalidOperation, ValueError, KeyError, TypeError, OverflowError) as error:
        raise HTTPException(503, "Invalid exchange provider response") from error


async def fetch_quote(client: httpx.AsyncClient, source: str, target: str, *, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    start = today - timedelta(days=LOOKBACK_DAYS)

    async def closing_rates(currency):
        params = {
            "@moeda": f"'{currency}'",
            "@dataInicial": f"'{start:%m-%d-%Y}'",
            "@dataFinalCotacao": f"'{today:%m-%d-%Y}'",
            "$format": "json",
            "$filter": "tipoBoletim eq 'Fechamento'",
            "$orderby": "dataHoraCotacao desc",
            "$top": "10",
        }
        # Olinda treats '+' literally in OData expressions: encode spaces as %20.
        response = await client.get(ENDPOINT + "?" + urlencode(params, quote_via=quote))
        PROVIDER_CALLS.labels(str(response.status_code)).inc()
        response.raise_for_status()
        rows = response.json(parse_float=Decimal)["value"]
        if not isinstance(rows, list):
            raise ValueError("Invalid PTAX value list")
        rates = {}
        for row in rows:
            if row["tipoBoletim"] != "Fechamento":
                continue
            rate = validate_quote({"sell": row["cotacaoVenda"], "buy": row["cotacaoCompra"],
                                   "date": row["dataHoraCotacao"]})
            timestamp = datetime.fromisoformat(rate["date"])
            if not start <= timestamp.date() <= today:
                continue
            previous = rates.get(timestamp.date())
            if previous is None or timestamp > datetime.fromisoformat(previous["date"]):
                rates[timestamp.date()] = rate
        if not rates:
            raise HTTPException(503, "No recent PTAX closing quote available")
        return rates

    currencies = [currency for currency in (source, target) if currency != "BRL"]
    # The caller's total timeout bounds both requests. Await both to avoid orphan requests on failure.
    results = await asyncio.gather(*(closing_rates(currency) for currency in currencies), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            raise result
    rates = dict(zip(currencies, results))
    common_days = set.intersection(*(set(values) for values in rates.values()))
    if not common_days:
        raise HTTPException(503, "No PTAX closing date shared by both currencies")
    day = max(common_days)
    quotes = {currency: values[day] for currency, values in rates.items()}
    timestamp = min(quotes.values(), key=lambda item: datetime.fromisoformat(item["date"]))["date"]
    reference = {"sell": Decimal(1), "buy": Decimal(1), "date": timestamp}
    source_quote, target_quote = quotes.get(source, reference), quotes.get(target, reference)
    return validate_quote({"sell": source_quote["sell"] / target_quote["buy"],
                           "buy": source_quote["buy"] / target_quote["sell"], "date": timestamp})
