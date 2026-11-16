"""Real Redis TTL and RabbitMQ publisher confirms with disposable dependencies."""
import asyncio
import json
import os
import time
from uuid import uuid4

import aio_pika
from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import Settings
from app.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("EXCHANGE_TEST_REDIS_URL") or not os.getenv("EXCHANGE_TEST_RABBITMQ_URL"),
    reason="Run scripts/test-infrastructure.sh for disposable Redis/RabbitMQ",
)]


def test_real_cache_expiry_identity_isolation_and_confirmed_event():
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"USDBRL": {
            "ask": "5.2", "bid": "5.1", "create_date": "2026-11-16 12:00:00"}})
    settings = Settings(cache_enabled=True, redis_url=os.environ["EXCHANGE_TEST_REDIS_URL"], cache_ttl=1,
                        messaging_enabled=True, rabbitmq_url=os.environ["EXCHANGE_TEST_RABBITMQ_URL"])
    with TestClient(create_app(httpx.MockTransport(provider), settings=settings)) as client:
        alice, bob = str(uuid4()), str(uuid4())
        first = client.get("/exchanges/USD/BRL", headers={"id-account": alice})
        second = client.get("/exchanges/USD/BRL", headers={"id-account": bob})
        assert first.status_code == second.status_code == 200
        assert first.json()["id-account"] == alice
        assert second.json()["id-account"] == bob
        assert len(calls) == 1
        async def inspect():
            assert await client.app.state.cache.ttl("exchange:v1:USD:BRL") >= 0
            assert set(await client.app.state.cache.hgetall("exchange:v1:USD:BRL")) == {"sell", "buy", "date"}
            await asyncio.wait_for(client.app.state.events.queue.join(), timeout=10)
            connection = await aio_pika.connect(settings.rabbitmq_url)
            async with connection:
                channel = await connection.channel()
                queue = await channel.get_queue("exchanges.quoted")
                message = await queue.get(timeout=3)
                async with message.process():
                    event = json.loads(message.body)
                    assert message.message_id == event["eventId"]
                    assert event["type"] == "QuoteFetched" and event["sell"] == "5.2"
                    assert "id-account" not in event
        client.portal.call(inspect)
        time.sleep(1.1)
        assert client.get("/exchanges/USD/BRL", headers={"id-account": alice}).status_code == 200
        assert len(calls) == 2
