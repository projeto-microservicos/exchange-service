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
    provider_url: str = "https://economia.awesomeapi.com.br"
    provider_timeout: float = 3.0
    cache_enabled: bool = False
    redis_url: str = "redis://localhost:6379/1"
    cache_ttl: int = 60
    messaging_enabled: bool = False
    rabbitmq_url: str = "amqp://guest:guest@localhost/"
    event_queue_size: int = 256

    def __post_init__(self):
        parsed = urlsplit(self.provider_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("EXCHANGE_PROVIDER_URL must be an HTTP(S) URL without credentials")
        if not math.isfinite(self.provider_timeout) or self.provider_timeout <= 0 or self.cache_ttl <= 0 or self.event_queue_size <= 0:
            raise ValueError("Timeout, cache TTL and event queue size must be positive")

    @classmethod
    def from_env(cls):
        return cls(
            provider_url=os.getenv("EXCHANGE_PROVIDER_URL", cls.provider_url),
            provider_timeout=float(os.getenv("EXCHANGE_PROVIDER_TIMEOUT", "3")),
            cache_enabled=boolean("CACHE_ENABLED"),
            redis_url=os.getenv("REDIS_URL", cls.redis_url),
            cache_ttl=int(os.getenv("EXCHANGE_CACHE_TTL", "60")),
            messaging_enabled=boolean("MESSAGING_ENABLED"),
            rabbitmq_url=os.getenv("RABBITMQ_URL", cls.rabbitmq_url),
            event_queue_size=int(os.getenv("EXCHANGE_EVENT_QUEUE_SIZE", "256")),
        )
