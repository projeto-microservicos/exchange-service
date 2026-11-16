"""Optional best-effort quote telemetry, outside the HTTP critical path."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import logging
from uuid import uuid4

import aio_pika
from pamqp.commands import Basic

from app.metrics import EVENTS, EVENT_QUEUE

logger = logging.getLogger(__name__)


class QuoteEvents:
    def __init__(self, url, *, queue_size=256, attempts=3, retry_delay=0.25, sender=None):
        self.url = url
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.attempts = attempts
        self.retry_delay = retry_delay
        self.sender = sender or self._publish
        self.connection = None
        self.exchange = None
        self.worker = None

    async def start(self):
        self.worker = asyncio.create_task(self._run(), name="exchange-quote-events")

    def offer(self, source, target, quote):
        event = {
            "eventId": str(uuid4()), "type": "QuoteFetched", "source": source, "target": target,
            "sell": str(quote["sell"]), "buy": str(quote["buy"]), "date": quote["date"],
            "occurredAt": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.queue.put_nowait(event)
            EVENTS.labels("queued").inc()
            EVENT_QUEUE.set(self.queue.qsize())
        except asyncio.QueueFull:
            EVENTS.labels("dropped").inc()
            logger.warning("Quote event queue full; telemetry dropped")

    async def _disconnect(self):
        if self.connection is not None:
            with suppress(Exception):
                await self.connection.close()
        self.connection = self.exchange = None

    async def _publish(self, event):
        if self.connection is None or self.connection.is_closed:
            self.connection = await aio_pika.connect(self.url, timeout=1)
            channel = await self.connection.channel(publisher_confirms=True, on_return_raises=True)
            self.exchange = await channel.declare_exchange("store.exchanges", aio_pika.ExchangeType.DIRECT, durable=True)
            queue = await channel.declare_queue("exchanges.quoted", durable=True, arguments={
                "x-max-length": 10000, "x-overflow": "reject-publish", "x-message-ttl": 86400000,
            })
            await queue.bind(self.exchange, routing_key="quoted")
        confirmation = await self.exchange.publish(aio_pika.Message(
            body=json.dumps(event).encode(), content_type="application/json", message_id=event["eventId"],
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ), routing_key="quoted", mandatory=True, timeout=1)
        if not isinstance(confirmation, Basic.Ack):
            raise RuntimeError("Broker did not confirm quote event")

    async def _run(self):
        while True:
            event = await self.queue.get()
            EVENT_QUEUE.set(self.queue.qsize())
            try:
                for attempt in range(self.attempts):
                    try:
                        async with asyncio.timeout(2):
                            await self.sender(event)
                        EVENTS.labels("published").inc()
                        break
                    except Exception as error:
                        await self._disconnect()
                        if attempt + 1 == self.attempts:
                            EVENTS.labels("dropped").inc()
                            logger.warning("Quote telemetry abandoned after retries (%s)", type(error).__name__)
                        else:
                            EVENTS.labels("retried").inc()
                            await asyncio.sleep(self.retry_delay * 2**attempt)
            finally:
                self.queue.task_done()

    async def close(self):
        if self.worker is not None:
            try:
                async with asyncio.timeout(2):
                    await self.queue.join()
            except TimeoutError:
                logger.warning("Shutdown before quote telemetry drained")
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker
        await self._disconnect()
