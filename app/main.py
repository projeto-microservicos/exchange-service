"""Exchange API behind the gateway: AwesomeAPI quotes without invented rates."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import math
import time
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, Path, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.messaging import QuoteEvents
from app.models import ExchangeQuote
from app.metrics import CACHE_REQUESTS, LATENCY, PROVIDER_CALLS, PROVIDER_LATENCY, REQUESTS

logger = logging.getLogger(__name__)
CURRENCIES = frozenset("USD BRL EUR GBP JPY CAD AUD CHF CNY INR SEK NOK DKK NZD MXN ARS ZAR CLP COP PEN UYU TRY HKD SGD ILS PLN RUB".split())
Currency = Annotated[str, Path(pattern=r"^[A-Za-z]{3}$")]


def validate_quote(raw: dict) -> dict:
    try:
        if isinstance(raw["sell"], bool) or isinstance(raw["buy"], bool):
            raise ValueError("Rates cannot be booleans")
        sell, buy = Decimal(raw["sell"]), Decimal(raw["buy"])
        if not all(rate.is_finite() and rate > 0 and math.isfinite(float(rate)) and float(rate) > 0 for rate in (sell, buy)) or sell < buy:
            raise ValueError("Invalid bid/ask rates")
        date = raw["date"]
        if not isinstance(date, str):
            raise ValueError("Invalid date")
        datetime.fromisoformat(date)
        return {"sell": sell, "buy": buy, "date": date}
    except (InvalidOperation, ValueError, KeyError, TypeError, OverflowError) as error:
        raise HTTPException(503, "Invalid exchange provider response") from error


def create_app(transport: httpx.AsyncBaseTransport | None = None, *, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            base_url=settings.provider_url, timeout=httpx.Timeout(settings.provider_timeout, connect=1, pool=0.25),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport, headers={"User-Agent": "InsperStore-Exchange/1.0"},
        ) as client:
            app.state.client = client
            app.state.cache = Redis.from_url(settings.redis_url, decode_responses=True,
                                             socket_timeout=0.2, socket_connect_timeout=0.2) if settings.cache_enabled else None
            app.state.events = QuoteEvents(settings.rabbitmq_url, queue_size=settings.event_queue_size) if settings.messaging_enabled else None
            if app.state.events is not None:
                await app.state.events.start()
            try:
                yield
            finally:
                if app.state.events is not None:
                    await app.state.events.close()
                if app.state.cache is not None:
                    await app.state.cache.aclose()

    app = FastAPI(title="Exchange API", version="1.0.0", lifespan=lifespan)
    locks: dict[tuple[str, str], asyncio.Lock] = {}

    @app.middleware("http")
    async def metrics(request, call_next):
        if request.url.path in {"/health", "/ready", "/metrics"}:
            return await call_next(request)
        endpoint = "exchanges" if request.url.path.startswith("/exchanges/") else "other"
        started, status = time.monotonic(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            REQUESTS.labels(str(status), endpoint).inc()
            LATENCY.labels(endpoint).observe(time.monotonic() - started)

    @app.get("/health", tags=["operations"])
    @app.get("/ready", tags=["operations"])
    async def health():
        return {"status": "UP"}

    @app.get("/metrics", include_in_schema=False)
    async def prometheus():
        return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def cached(key):
        try:
            raw = await app.state.cache.hgetall(key)
            if raw:
                result = validate_quote(raw)
                CACHE_REQUESTS.labels("hit").inc()
                return result
            CACHE_REQUESTS.labels("miss").inc()
        except (RedisError, HTTPException):
            CACHE_REQUESTS.labels("error").inc()
            logger.warning("Exchange cache unavailable or invalid; using provider")
        return None

    async def fetch(source, target):
        started = time.monotonic()
        try:
            async with asyncio.timeout(settings.provider_timeout):
                response = await app.state.client.get(f"/json/last/{source}-{target}")
                PROVIDER_CALLS.labels(str(response.status_code)).inc()
                inverse = response.status_code in (400, 404)
                if inverse:
                    response = await app.state.client.get(f"/json/last/{target}-{source}")
                    PROVIDER_CALLS.labels(str(response.status_code)).inc()
                    if response.status_code in (400, 404):
                        raise HTTPException(422, "Currency pair not supported")
                response.raise_for_status()
                raw = response.json()[(target + source) if inverse else (source + target)]
                result = validate_quote({"sell": raw["ask"], "buy": raw["bid"], "date": raw["create_date"]})
                if inverse:
                    result = validate_quote({"sell": Decimal(1) / result["buy"],
                                             "buy": Decimal(1) / result["sell"], "date": result["date"]})
        except HTTPException:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            PROVIDER_CALLS.labels("failure").inc()
            logger.warning("Exchange provider failed (%s)", type(error).__name__)
            raise HTTPException(503, "Exchange provider unavailable") from error
        finally:
            PROVIDER_LATENCY.observe(time.monotonic() - started)
        if app.state.events is not None:
            app.state.events.offer(source, target, result)
        return result

    async def quote(source, target):
        cache = app.state.cache
        if cache is None:
            CACHE_REQUESTS.labels("disabled").inc()
            return await fetch(source, target)
        key = f"exchange:v1:{source}:{target}"
        # The allowlist bounds the lock map. Recheck inside the lock to prevent cache stampedes.
        async with locks.setdefault((source, target), asyncio.Lock()):
            hit = await cached(key)
            if hit is not None:
                return hit
            result = await fetch(source, target)
            try:
                async with cache.pipeline(transaction=True) as pipe:
                    pipe.hset(key, mapping={name: str(value) for name, value in result.items()})
                    pipe.expire(key, settings.cache_ttl)
                    await pipe.execute()
            except RedisError:
                logger.warning("Could not cache exchange quote")
            return result

    @app.get("/exchanges/{source}/{target}", tags=["exchange"], response_model=ExchangeQuote)
    async def exchange(source: Currency, target: Currency, account: Annotated[UUID, Header(alias="id-account")]):
        source, target = source.upper(), target.upper()
        if source not in CURRENCIES or target not in CURRENCIES:
            raise HTTPException(422, "Currency not supported")
        result = {"sell": Decimal(1), "buy": Decimal(1), "date": datetime.now(timezone.utc).isoformat()} if source == target else await quote(source, target)
        return {"sell": float(result["sell"]), "buy": float(result["buy"]), "date": result["date"], "id-account": str(account)}

    return app


app = create_app()
