"""Environment settings; optional infrastructure stays disabled by default."""
from dataclasses import dataclass
import os
import math
from urllib.parse import urlsplit


def boolean(name: str, default: str = "false") -> bool:
    value = os.getenv(name, default).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class Settings:
    provider_url: str = "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata"
    provider_timeout: float = 3.0
    cache_enabled: bool = False
    redis_url: str = "redis://localhost:6379/1"
    cache_ttl: int = 60
    rate_ttl: float = 300.0
    rate_retry: float = 30.0
    messaging_enabled: bool = False
    rabbitmq_url: str = "amqp://guest:guest@localhost/"
    event_queue_size: int = 256

    def __post_init__(self):
        parsed = urlsplit(self.provider_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("EXCHANGE_PROVIDER_URL must be an HTTP(S) URL without credentials")
        numbers = (self.provider_timeout, self.rate_ttl, self.rate_retry)
        if not all(math.isfinite(n) and n > 0 for n in numbers) or self.cache_ttl <= 0 or self.event_queue_size <= 0:
            raise ValueError("Timeout, cache TTLs, retry and event queue size must be positive")

    @classmethod
    def from_env(cls):
        return cls(
            provider_url=os.getenv("EXCHANGE_PROVIDER_URL", cls.provider_url),
            provider_timeout=float(os.getenv("EXCHANGE_PROVIDER_TIMEOUT", "3")),
            cache_enabled=boolean("CACHE_ENABLED"),
            redis_url=os.getenv("REDIS_URL", cls.redis_url),
            cache_ttl=int(os.getenv("EXCHANGE_CACHE_TTL", "60")),
            rate_ttl=float(os.getenv("EXCHANGE_RATE_TTL", "300")),
            rate_retry=float(os.getenv("EXCHANGE_RATE_RETRY", "30")),
            messaging_enabled=boolean("MESSAGING_ENABLED"),
            rabbitmq_url=os.getenv("RABBITMQ_URL", cls.rabbitmq_url),
            event_queue_size=int(os.getenv("EXCHANGE_EVENT_QUEUE_SIZE", "256")),
        )
