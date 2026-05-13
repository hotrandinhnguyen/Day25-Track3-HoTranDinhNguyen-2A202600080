"""Property-based tests for CircuitBreaker using Hypothesis.

Verifies that state transitions are always valid under random failure/success sequences.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState

VALID_TRANSITIONS: set[tuple[str, str]] = {
    ("closed", "open"),
    ("open", "half_open"),
    ("half_open", "closed"),
    ("half_open", "open"),
}


@given(
    failure_threshold=st.integers(min_value=1, max_value=5),
    success_threshold=st.integers(min_value=1, max_value=3),
    actions=st.lists(st.booleans(), min_size=1, max_size=50),
)
@settings(max_examples=200)
def test_state_transitions_always_valid(
    failure_threshold: int,
    success_threshold: int,
    actions: list[bool],
) -> None:
    """True = success, False = failure. Every transition must be in VALID_TRANSITIONS."""
    cb = CircuitBreaker(
        name="test",
        failure_threshold=failure_threshold,
        reset_timeout_seconds=9999.0,  # Disable timeout-based transitions
        success_threshold=success_threshold,
    )
    for is_success in actions:
        if is_success:
            cb.record_success()
        else:
            cb.record_failure()

    for entry in cb.transition_log:
        pair = (str(entry["from"]), str(entry["to"]))
        assert pair in VALID_TRANSITIONS, f"Invalid transition: {pair}"


@given(
    failure_threshold=st.integers(min_value=1, max_value=5),
    failures=st.integers(min_value=0, max_value=10),
)
@settings(max_examples=100)
def test_circuit_opens_after_threshold(failure_threshold: int, failures: int) -> None:
    """After >= failure_threshold consecutive failures, state must be OPEN."""
    cb = CircuitBreaker(
        name="test",
        failure_threshold=failure_threshold,
        reset_timeout_seconds=9999.0,
        success_threshold=1,
    )
    for _ in range(failures):
        cb.record_failure()

    if failures >= failure_threshold:
        assert cb.state == CircuitState.OPEN
    else:
        assert cb.state == CircuitState.CLOSED


@given(
    failure_threshold=st.integers(min_value=1, max_value=5),
    n_failures=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=100)
def test_open_circuit_raises_immediately(failure_threshold: int, n_failures: int) -> None:
    """When OPEN, allow_request() must return False and call() must raise CircuitOpenError."""
    cb = CircuitBreaker(
        name="test",
        failure_threshold=failure_threshold,
        reset_timeout_seconds=9999.0,
        success_threshold=1,
    )
    for _ in range(failure_threshold):
        cb.record_failure()

    assert cb.state == CircuitState.OPEN
    assert not cb.allow_request()

    raised = False
    try:
        cb.call(lambda: None)
    except CircuitOpenError:
        raised = True
    assert raised, "CircuitOpenError must be raised when circuit is OPEN"
