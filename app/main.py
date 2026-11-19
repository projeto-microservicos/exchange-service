"""Exchange API behind the gateway: PTAX (BCB) rates kept in memory, never invented."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
import logging
import time
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, Path, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.bacen import validate_quote
from app.messaging import QuoteEvents
from app.models import ExchangeQuote
from app.metrics import CACHE_REQUESTS, LATENCY, PROVIDER_LATENCY, REQUESTS
from app.ptax import SUPPORTED, PtaxRates, ProviderError

logger = logging.getLogger(__name__)
CURRENCIES = SUPPORTED  # the ten currencies PTAX quotes, plus BRL
Currency = Annotated[str, Path(pattern=r"^[A-Za-z]{3}$")]
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"  # exchange.md example: "2021-09-01 14:23:42"
OPERATIONS = frozenset({"/health", "/ready", "/metrics", "/exchange/health-check"})


def create_app(transport: httpx.AsyncBaseTransport | None = None, *, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            base_url=settings.provider_url.rstrip("/") + "/", timeout=httpx.Timeout(settings.provider_timeout, connect=1, pool=0.25),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport, headers={"User-Agent": "InsperStore-Exchange/1.0"},
        ) as client:
            app.state.client = client
            app.state.rates = PtaxRates(client, ttl=settings.rate_ttl, retry=settings.rate_retry,
                                        timeout=settings.provider_timeout)
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
        if request.url.path in OPERATIONS:
            return await call_next(request)
        # Singular and plural paths share one label, so dashboards keep a single series.
        endpoint = "exchanges" if request.url.path.startswith(("/exchanges/", "/exchange/")) else "other"
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

    @app.get("/exchange/health-check", tags=["operations"], status_code=200, response_class=Response)
    async def health_check():
        """Overview.md: 200 with nothing internal while the service is up; the probes call it."""
        return Response(status_code=200)

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
            # A Redis miss is not counted: the memory layer below records hit, miss or stale.
        except (RedisError, HTTPException):
            CACHE_REQUESTS.labels("error").inc()
            logger.warning("Exchange cache unavailable or invalid; using provider")
        return None

    async def fetch(source, target):
        """Rate from the in-memory PTAX cache (always on): status is "hit", "miss" or "stale"."""
        started = time.monotonic()
        try:
            raw, status = await app.state.rates.pair(source, target)
            result = validate_quote(raw)
        except ProviderError as error:
            raise HTTPException(503, "Exchange provider unavailable") from error
        finally:
            PROVIDER_LATENCY.observe(time.monotonic() - started)
        CACHE_REQUESTS.labels(status).inc()
        if status == "miss" and app.state.events is not None:
            app.state.events.offer(source, target, result)
        return result, status

    async def quote(source, target):
        """Optional Redis layer (shared between replicas) on top of the per-process memory cache."""
        cache = app.state.cache
        if cache is None:
            return await fetch(source, target)
        key = f"exchange:ptax:v1:{source}:{target}"
        # The allowlist bounds the lock map. Recheck inside the lock to prevent cache stampedes.
        async with locks.setdefault((source, target), asyncio.Lock()):
            hit = await cached(key)
            if hit is not None:
                return hit, "hit"
            result, status = await fetch(source, target)
            if status == "stale":
                return result, status  # never spread an expired rate to the other replicas
            try:
                async with cache.pipeline(transaction=True) as pipe:
                    pipe.hset(key, mapping={name: str(value) for name, value in result.items()})
                    pipe.expire(key, settings.cache_ttl)
                    await pipe.execute()
            except RedisError:
                logger.warning("Could not cache exchange quote")
            return result, status

    # exchange.md declares /exchanges/{from}/{to}; its example (and the BACEN page) use /exchange/USD/EUR.
    @app.get("/exchanges/{source}/{target}", tags=["exchange"], response_model=ExchangeQuote)
    @app.get("/exchange/{source}/{target}", tags=["exchange"], response_model=ExchangeQuote)
    async def exchange(source: Currency, target: Currency, account: Annotated[UUID, Header(alias="id-account")],
                       response: Response):
        source, target = source.upper(), target.upper()
        if source not in CURRENCIES or target not in CURRENCIES:
            raise HTTPException(422, "Currency not supported")
        if source == target:
            same = {"sell": Decimal(1), "buy": Decimal(1), "date": datetime.now(timezone.utc).strftime(DATE_FORMAT)}
            result, status = same, "hit"
        else:
            result, status = await quote(source, target)
        # Like the course's BACEN service: hit (memory), miss (PTAX called) or stale (PTAX failed, old rate).
        response.headers["X-Cache"] = status
        return {"sell": float(result["sell"]), "buy": float(result["buy"]), "date": result["date"], "id-account": str(account)}

    return app


app = create_app()
