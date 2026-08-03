"""Circuit Breaker — detects when the agent is stuck in a loop.

Two signals are checked:
  1. ``repetition_count`` — how many times the reflector flagged repetition
  2. ``entropy_score``    — diversity of recent actions (0.0 = stuck, 1.0 = diverse)

If either crosses the threshold the agent is halted.
"""

from __future__ import annotations

from loguru import logger

MAX_REPETITION = 3
MIN_ENTROPY = 0.15


def should_halt(repetition_count: int, entropy_score: float) -> bool:
    """Return ``True`` if the agent should be halted."""
    if repetition_count >= MAX_REPETITION:
        logger.error(
            "Circuit breaker: repetition_count={} >= {}",
            repetition_count,
            MAX_REPETITION,
        )
        return True

    if entropy_score < MIN_ENTROPY:
        logger.error(
            "Circuit breaker: entropy_score={:.3f} < {}",
            entropy_score,
            MIN_ENTROPY,
        )
        return True

    return False
