"""Исполнитель: очередь, таймауты, учёт расхода, устойчивость к падениям."""

from __future__ import annotations

import time

import pytest

from aconstructor.config import Settings
from aconstructor.core import REGISTRY, Pipeline, load_registry, register, task_input
from aconstructor.data import samples
from aconstructor.runner import RunTimeout, Runner, UsageTracker, _with_timeout, price_of
from aconstructor.store import RunStore

load_registry()


@pytest.fixture()
def store(tmp_path):
    s = RunStore(tmp_path / "r.db")
    yield s
    s.close()


@pytest.fixture()
def runner(store):
    r = Runner(store, Settings(provider="fake"), workers=2, timeout_s=10)
    r.start()
    yield r
    r.stop()


def wait_for(store, run_id, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        run = store.get(run_id)
        if run and run.status in ("done", "failed", "cancelled"):
            return run
        time.sleep(0.02)
    raise AssertionError(f"прогон {run_id} не завершился за {timeout} с")


def test_sync_run_completes(runner, store):
    run = runner.run_sync("urban-scout", {})
    assert run.status == "done"
    assert run.report.startswith("#")
    assert run.duration_s >= 0


def test_queued_run_completes(runner, store):
    run = runner.submit("energy-hacker", {})
    done = wait_for(store, run.id)
    assert done.status == "done"
    assert done.findings_n > 0


def test_many_runs_are_all_processed(runner, store):
    ids = [runner.submit("urban-scout", {}).id for _ in range(6)]
    for i in ids:
        assert wait_for(store, i).status == "done"


def test_unknown_pipeline_rejected_before_queueing(runner):
    with pytest.raises(KeyError):
        runner.submit("нетакого", {})


def test_failing_pipeline_is_recorded_not_crashing(runner, store):
    """Падение одного прогона не должно убивать воркер."""

    def boom(cfg=None):
        raise RuntimeError("сломался билд графа")

    register(Pipeline(slug="boom-test", title="t", summary="s",
                      build=boom, demo_task=dict))
    try:
        bad = wait_for(store, runner.submit("boom-test", {}).id)
        assert bad.status == "failed"
        assert "сломался" in bad.error
        # воркеры живы и берут следующую задачу
        good = wait_for(store, runner.submit("urban-scout", {}).id)
        assert good.status == "done"
    finally:
        REGISTRY.pop("boom-test", None)


def test_queue_overflow_returns_error(store):
    r = Runner(store, Settings(provider="fake"), workers=1, max_queue=2)
    # воркеры не запущены — очередь гарантированно переполнится
    r.submit("urban-scout", {})
    r.submit("urban-scout", {})
    with pytest.raises(RuntimeError, match="переполнена"):
        r.submit("urban-scout", {})


def test_cancelled_run_is_not_executed(store):
    r = Runner(store, Settings(provider="fake"), workers=1)
    run = r.submit("urban-scout", {})
    assert store.cancel(run.id) is True
    r.start()
    time.sleep(0.4)
    r.stop()
    assert store.get(run.id).status == "cancelled"


def test_timeout_helper_raises():
    with pytest.raises(RunTimeout):
        _with_timeout(lambda: time.sleep(2), timeout_s=0.1)


def test_timeout_helper_propagates_error():
    with pytest.raises(ValueError, match="ой"):
        _with_timeout(lambda: (_ for _ in ()).throw(ValueError("ой")), timeout_s=5)


def test_stale_runs_marked_on_start(store):
    run = store.create("urban-scout", {})
    store.mark_running(run.id)
    r = Runner(store, Settings(provider="fake"), workers=1)
    r.start()
    r.stop()
    assert store.get(run.id).status == "failed"


def test_price_calculation():
    assert price_of("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)
    assert price_of("неизвестная-модель", 1_000_000, 0) == 0.0
    assert price_of("", 1000, 1000) == 0.0


def test_price_uses_longest_prefix():
    """gpt-4o-mini начинается с gpt-4o — нельзя брать первое совпадение."""
    mini = price_of("gpt-4o-mini-2024-07-18", 1_000_000, 0)
    full = price_of("gpt-4o-2024-08-06", 1_000_000, 0)
    assert mini == pytest.approx(0.15)
    assert full == pytest.approx(2.50)
    assert mini < full


def test_usage_tracker_reads_langchain_metadata():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    t = UsageTracker()
    msg = AIMessage(content="x")
    msg.usage_metadata = {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42}
    t.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]))
    d = t.as_dict("gpt-4o-mini")
    assert d["tokens_in"] == 30 and d["tokens_out"] == 12 and d["llm_calls"] == 1


# --- контракт входных данных ----------------------------------------------
def test_task_input_keeps_empty_client_input():
    """Пустой список от клиента — валидный вход, а не повод подставить демо."""
    assert task_input({"parcels": []}, "parcels", samples.parcels) == []
    assert task_input({}, "parcels", samples.parcels) == samples.parcels()


def test_empty_input_yields_empty_result_not_demo_data(runner):
    """Клиент прислал пустой список — счёт за демо-выдумку выставлять нельзя."""
    run = runner.run_sync("urban-scout", {"parcels": [], "buildings": []})
    assert run.status == "done"
    assert run.findings_n == 0
    assert run.result["buildable"] == []
