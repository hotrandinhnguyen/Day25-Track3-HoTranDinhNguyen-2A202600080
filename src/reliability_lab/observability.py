from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# Matches metric names from the lab slides
agent_requests_total = Counter(
    "agent_requests_total",
    "Total number of requests processed by the gateway",
    ["route", "provider"],
)

agent_latency_seconds = Histogram(
    "agent_latency_seconds",
    "Request latency in seconds",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

cache_hits_total = Counter(
    "cache_hits_total",
    "Total number of cache hits",
)

circuit_state = Gauge(
    "circuit_state",
    "Current circuit breaker state (0=closed, 1=half_open, 2=open)",
    ["circuit_name"],
)


def record_gateway_response(route: str, provider: str | None, latency_ms: float, cache_hit: bool) -> None:
    """Update Prometheus metrics from a GatewayResponse."""
    label_provider = provider or "none"
    agent_requests_total.labels(route=route, provider=label_provider).inc()
    agent_latency_seconds.observe(latency_ms / 1000.0)
    if cache_hit:
        cache_hits_total.inc()


def update_circuit_state(name: str, state_value: str) -> None:
    """Push current circuit state to Prometheus gauge."""
    state_map = {"closed": 0, "half_open": 1, "open": 2}
    circuit_state.labels(circuit_name=name).set(state_map.get(state_value, -1))
