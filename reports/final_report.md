# Day 10 Reliability Report

**Họ và tên:** Hồ Trần Đình Nguyên
**MSSV:** 2A202600080

## 1. Architecture summary

The gateway enforces reliability through four layers: budget guard, cache, circuit breakers, and static fallback.

```
User Request
    |
    v
[ReliabilityGateway]
    |
    +---> [Budget guard]  cumulative_cost >= hard_limit?
    |          YES --> try cache, else static_fallback (cost_budget_exceeded)
    |          NO  --> continue
    |
    +---> [Cache check] (ResponseCache / SharedRedisCache)
    |          HIT?  --> return cached response  (route: cache_hit:<score>)
    |          MISS  --> continue
    |
    |     [80% budget warn] --> skip expensive providers, cheapest only
    |
    +---> [CircuitBreaker: primary]
    |          CLOSED? --> FakeLLMProvider("primary")   (route: primary:primary)
    |          OPEN?   --> CircuitOpenError, skip
    |
    +---> [CircuitBreaker: backup]
    |          CLOSED? --> FakeLLMProvider("backup")    (route: fallback:backup)
    |          OPEN?   --> CircuitOpenError, skip
    |
    +---> [Static fallback message]                     (route: static_fallback)
```

**Concurrency:** Requests within each scenario run with `ThreadPoolExecutor(max_workers=10)`. CircuitBreaker is protected by `threading.Lock` to prevent race conditions on state mutations.

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Detects real failures fast without tripping on single-request jitter |
| reset_timeout_seconds | 2 | Matches simulated provider recovery time; short enough to probe quickly |
| success_threshold | 1 | One successful probe re-closes the circuit in this simulation |
| cache TTL | 300 s | 5-minute freshness is appropriate for FAQ-style repeated queries |
| similarity_threshold | 0.92 | Character bigram at 0.92 avoids false hits on date-different queries (tested: "2024" vs "2026") |
| load_test requests | 200 | 1000 total across 5 scenarios — stable percentile estimates |
| concurrency | 10 | Matches typical gateway worker count; exercises thread-safety of circuit breakers |
| cost_budget (hard) | $0.10 | Hard limit per simulation run; above this, only cache or static_fallback |
| cost_budget (warn) | $0.08 | At 80%, routes only to cheapest provider to slow spending |

## 3. SLO definitions

Actual values are taken from `reports/metrics.json` (aggregate across all 5 scenarios, 1000 total requests).
Note: the aggregate intentionally includes stress scenarios (`both_degraded`, `primary_timeout_100`) that violate SLOs by design — this is expected chaos testing behavior, not a production regression.

| SLI | SLO target | Actual (aggregate) | Met? | Note |
|---|---|---:|---|---|
| Availability | >= 99% | 97.50% | ⚠️ | Dragged down by `both_degraded` (40%/40% fail rate) and `primary_timeout_100` scenarios |
| Latency P95 | < 2500 ms | 516 ms | ✅ | Well within target even under chaos |
| Fallback success rate | >= 95% | 86.91% | ⚠️ | Backup circuit trips under concurrent load in `primary_timeout_100`; in `all_healthy` scenario: 100% |
| Cache hit rate | >= 10% | 73.20% | ✅ | Consistently high across all scenarios |
| Recovery time | < 5000 ms | N/A* | — | |

*`recovery_time_ms` is null because with `concurrency=10`, all 200 requests per scenario complete before the 2-second reset timeout elapses — no full OPEN→HALF_OPEN→CLOSED cycle fits within the simulation window. This is expected for short-duration load tests. In `all_healthy` baseline alone: availability=100%, P95=238ms, all SLOs met.

## 4. Metrics

Paste or summarize `reports/metrics.json` (200 requests/scenario × 5 scenarios = 1000 total, concurrency=10).

| Metric | Value |
|---|---:|
| total_requests | 1000 |
| availability | 0.9750 |
| error_rate | 0.0250 |
| latency_p50_ms | 282.0 |
| latency_p95_ms | 516.0 |
| latency_p99_ms | 531.0 |
| fallback_success_rate | 0.8691 |
| cache_hit_rate | 0.7320 |
| circuit_open_count | 5 |
| recovery_time_ms | null (see SLO note) |
| estimated_cost | 0.103106 |
| estimated_cost_saved | 0.732 |

## 5. Cache comparison

| Metric | Without cache | With cache (memory) | Delta |
|---|---:|---:|---|
| latency_p50_ms | 281.07 | 281.0 | ~0% |
| latency_p95_ms | 516.0 | 516.0 | ~0% |
| estimated_cost | 0.245024 | 0.050182 | **−79.5%** |
| estimated_cost_saved | 0.0 | 0.479 | +$0.479 |
| cache_hit_rate | 0.0 | 0.7983 | +79.8pp |
| circuit_open_count | 46 | 9 | **−80.4%** |
| availability | 0.9500 | 0.9933 | +4.3pp |

**Key insight:** Latency P50/P95 are dominated by provider sleep time — cache hits serve in <1 ms but the population average barely moves because hits are counted with 0 ms latency out of the latencies_ms list. The dramatic wins are cost (−80%) and circuit stability (−80% opens) — cache absorbs repeated queries that would otherwise hammer failing providers.

## 6. Redis shared cache

### Why in-memory cache is insufficient for multi-instance deployments

Each gateway process owns a separate `ResponseCache` object. Two instances serving the same query independently compute the response twice — no sharing, no cost savings across instances.

### How `SharedRedisCache` solves this

`SharedRedisCache` stores entries in Redis using `HSET key mapping={query, response}` with automatic TTL via `EXPIRE`. Any gateway instance pointing at the same Redis URL sees the same cache state. A hit cached by instance A is immediately available to instance B.

### Evidence of shared state

```python
# test_shared_state_across_instances (tests/test_redis_cache.py)
c1 = SharedRedisCache(redis_url="redis://localhost:6379/0", prefix="rl:test:shared:")
c2 = SharedRedisCache(redis_url="redis://localhost:6379/0", prefix="rl:test:shared:")
c1.set("shared query", "shared response")
cached, _ = c2.get("shared query")
assert cached == "shared response"  # ✅ PASSED
```

All 6 Redis tests pass: connection, set+get, TTL expiry, shared state, privacy bypass, false-hit detection.

### Redis CLI output

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:b2a52f7dc795
rl:cache:9e413fd814eb
rl:cache:095946136fea
rl:cache:8baa2cfa11fa
```

### In-memory vs Redis cache comparison

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 297.0 | 281.0 | Redis slightly lower (persistent across resets) |
| latency_p95_ms | 531.0 | 516.0 | Redis slightly better |
| cache_hit_rate | 0.7983 | 0.8133 | Redis higher — entries persist between scenarios |
| circuit_open_count | 9 | 9 | Same — circuit behavior independent of cache backend |
| estimated_cost_saved | 0.479 | 0.488 | Redis saves ~2% more |

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary circuit opens; system stays available via backup | circuit_open_count > 0 ✅; availability > 0.5 ✅ | ✅ pass |
| primary_flaky_50 | Circuit oscillates; mix of primary and fallback | fallback_successes > 0 ✅; circuit_open_count > 0 ✅ | ✅ pass |
| all_healthy | Zero failures; circuit stays closed | availability >= 0.95 ✅; circuit_open_count == 0 ✅ | ✅ pass |
| both_degraded | Both at 40% fail; some static fallbacks | availability >= 0.5 ✅ | ✅ pass |
| backup_only_healthy | Primary fully down, backup healthy | fallback_successes > 0 ✅; circuit_open_count > 0 ✅ | ✅ pass |

**Concurrency note:** All scenarios ran with `ThreadPoolExecutor(max_workers=10)`. Under concurrent load, circuit breakers are protected by `threading.Lock` — no race conditions observed across 1000 total requests.

## 8. Failure analysis

**Remaining weakness: circuit breaker state is not shared across gateway instances.**

Each `CircuitBreaker` object lives in a single process. In a horizontally-scaled deployment with N gateway processes, instance A may detect that the primary provider is failing and open its circuit while instance B continues sending requests to the same broken provider. The result is a partial retry storm from B's perspective — exactly the failure mode circuit breakers are designed to prevent.

**What I would change before production:**
Store `failure_count`, `opened_at`, and `state` in Redis using atomic `INCR` / `EXPIRE` / `SET NX` so all instances share a single circuit state. This is the approach used by distributed circuit breaker implementations (e.g., Resilience4j with Redis).

## 9. Next steps

1. **Redis-backed circuit state:** Move `CircuitBreaker` counters and state into Redis keys with atomic operations (`INCR`, `SET NX`, `EXPIRE`) so the circuit is consistent across all gateway instances — prevents partial retry storms in scaled deployments.

2. **Concurrency scaling test:** Run simulation at `concurrency: 1`, `5`, `10`, `20` and plot P95 latency vs. throughput — this reveals the inflection point where provider saturation begins and whether circuit breakers stabilize or amplify load spikes.

3. **SLO alerting in CI:** Post-run, compare `metrics.json` values against defined SLO thresholds and exit non-zero if any SLO is breached — lets CI pipelines catch reliability regressions before they reach production.

---

## Bonus features implemented

| Feature | Status | Evidence |
|---|---|---|
| Concurrency (ThreadPoolExecutor) | ✅ | `run_scenario` uses `max_workers=concurrency` from config; CircuitBreaker thread-safe via `threading.Lock` |
| 5 chaos scenarios (min 3) | ✅ | primary_timeout_100, primary_flaky_50, all_healthy, both_degraded, backup_only_healthy |
| Property-based tests (hypothesis) | ✅ | `tests/test_circuit_breaker_property.py` — 200 examples, 3 properties |
| Prometheus export | ✅ | `src/reliability_lab/observability.py` — agent_requests_total, agent_latency_seconds, cache_hits_total, circuit_state |
| Cost-aware routing | ✅ | Gateway skips expensive providers at 80% budget; hard stop at 100% |
| Redis graceful degradation | ✅ | `SharedRedisCache.get/set` wrapped in `try/except`; gateway continues on Redis failure |
| SLO definition table | ✅ | Section 3 above with pass/fail per SLI |
