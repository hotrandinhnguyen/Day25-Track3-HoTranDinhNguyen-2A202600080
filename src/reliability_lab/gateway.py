from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.observability import record_gateway_response, update_circuit_state
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


_COST_BUDGET_HARD = 0.10   # absolute cost cap — return cache-only/static above this
_COST_BUDGET_WARN = 0.08   # 80% of hard cap — skip expensive primary, route to cheapest


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        cost_budget: float = _COST_BUDGET_HARD,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cost_budget = cost_budget
        self._cumulative_cost: float = 0.0

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback."""
        start = time.monotonic()

        if self._cumulative_cost >= self.cost_budget:
            return self._budget_exceeded_response(prompt, start)

        cache_resp = self._try_cache(prompt, start)
        if cache_resp is not None:
            return cache_resp

        return self._try_providers(prompt, start)

    def _try_cache(self, prompt: str, start: float) -> GatewayResponse | None:
        """Return a cached response if available, else None."""
        if self.cache is None:
            return None
        cached, score = self.cache.get(prompt)
        if cached is None:
            return None
        elapsed = (time.monotonic() - start) * 1000
        resp = GatewayResponse(cached, f"cache_hit:{score:.2f}", None, True, elapsed, 0.0)
        record_gateway_response(resp.route, resp.provider, resp.latency_ms, resp.cache_hit)
        return resp

    def _candidate_providers(self) -> list[FakeLLMProvider]:
        """Return eligible providers, skipping expensive ones when near budget."""
        warn_threshold = self.cost_budget * (_COST_BUDGET_WARN / _COST_BUDGET_HARD)
        if self._cumulative_cost < warn_threshold:
            return self.providers
        cheapest = min(p.cost_per_1k_tokens for p in self.providers)
        return [p for p in self.providers if p.cost_per_1k_tokens <= cheapest]

    def _try_providers(self, prompt: str, start: float) -> GatewayResponse:
        """Try each provider in order; return static fallback if all fail."""
        last_error: str | None = None
        for i, provider in enumerate(self._candidate_providers()):
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                route = f"primary:{provider.name}" if i == 0 else f"fallback:{provider.name}"
                elapsed = (time.monotonic() - start) * 1000
                resp = GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=max(response.latency_ms, elapsed),
                    estimated_cost=response.estimated_cost,
                )
                self._cumulative_cost += response.estimated_cost
                self._emit_metrics(resp)
                return resp
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)

        elapsed = (time.monotonic() - start) * 1000
        resp = GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=elapsed,
            estimated_cost=0.0,
            error=last_error,
        )
        self._emit_metrics(resp)
        return resp

    def _budget_exceeded_response(self, prompt: str, start: float) -> GatewayResponse:
        """Serve from cache if possible; otherwise return cost-limit static fallback."""
        cache_resp = self._try_cache(prompt, start)
        if cache_resp is not None:
            return cache_resp
        elapsed = (time.monotonic() - start) * 1000
        resp = GatewayResponse(
            text="Service temporarily unavailable due to cost limits.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=elapsed,
            estimated_cost=0.0,
            error="cost_budget_exceeded",
        )
        record_gateway_response(resp.route, resp.provider, resp.latency_ms, resp.cache_hit)
        return resp

    def _emit_metrics(self, resp: GatewayResponse) -> None:
        record_gateway_response(resp.route, resp.provider, resp.latency_ms, resp.cache_hit)
        for name, breaker in self.breakers.items():
            update_circuit_state(name, breaker.state.value)
