"""Tests for core agent logic — circuit breaker, state, planner."""

from __future__ import annotations

import uuid

import pytest

from astra.core.circuit_breaker import should_halt


# ── Circuit Breaker ──────────────────────────────────────────

def test_should_halt_on_high_repetition():
    assert should_halt(repetition_count=5, entropy_score=0.8) is True


def test_should_halt_on_low_entropy():
    assert should_halt(repetition_count=0, entropy_score=0.05) is True


def test_should_not_halt_normal():
    assert should_halt(repetition_count=1, entropy_score=0.7) is False


def test_should_not_halt_exact_threshold():
    # entropy == MIN_ENTROPY (0.15) should NOT halt (only < 0.15 halts)
    assert should_halt(repetition_count=2, entropy_score=0.15) is False


def test_should_halt_exact_repetition_threshold():
    # repetition == MAX_REPETITION (3) SHOULD halt
    assert should_halt(repetition_count=3, entropy_score=0.9) is True


# ── State ────────────────────────────────────────────────────

def test_make_initial_state():
    from astra.core.state import make_initial_state

    sid = uuid.uuid4()
    pid = uuid.uuid4()
    state = make_initial_state(session_id=sid, project_id=pid, goal="test goal")

    assert state["session_id"] == sid
    assert state["project_id"] == pid
    assert len(state["messages"]) == 1
    assert state["messages"][0].content == "test goal"
    assert state["current_plan"] == []
    assert state["current_step_index"] == 0
    assert state["is_halted"] is False
    assert state["entropy_score"] == 1.0


# ── Graph compilation ────────────────────────────────────────

def test_graph_compiles():
    from astra.core.agent import agent_graph

    # Just verify the graph compiles without errors
    assert agent_graph is not None


# ── Planner (mock) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_plan_fallback(monkeypatch):
    """When LLM fails, planner should return a single-step fallback."""
    from astra.core import planner

    async def mock_chat(*args, **kwargs):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr("astra.core.planner.llm_gateway.chat", mock_chat)

    steps = await planner.generate_plan("do something", [], "")
    assert len(steps) == 1
    assert steps[0] == "do something"
