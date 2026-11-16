from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter("exchange_requests_total", "HTTP requests excluding health and metrics", ["status", "endpoint"])
LATENCY = Histogram("exchange_request_duration_seconds", "HTTP request duration", ["endpoint"])
CACHE_REQUESTS = Counter("exchange_cache_requests_total", "Cache lookups", ["result"])
PROVIDER_CALLS = Counter("exchange_provider_requests_total", "Provider HTTP calls", ["result"])
PROVIDER_LATENCY = Histogram("exchange_provider_duration_seconds", "Total quote resolution duration")
EVENTS = Counter("exchange_events_total", "Quote event delivery", ["result"])
EVENT_QUEUE = Gauge("exchange_event_queue_size", "Quote events waiting for delivery")
