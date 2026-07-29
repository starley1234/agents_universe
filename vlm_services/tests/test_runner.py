"""Продовая обвязка: кеш, повторы, бюджет, журнал, учёт стоимости."""

from __future__ import annotations

import time

import pytest

from vlmkit.config import Settings
from vlmkit.core import ServiceError, get_service
from vlmkit.demo import demo_image
from vlmkit.runner import BudgetExceeded, Runner, UsageTracker, price_of
from vlmkit.store import Store, cache_key

SHELF = {"facings": [{"brand": "A", "product": "x", "count": 5, "price_tag": True}],
         "empty_slots": 0}


@pytest.fixture()
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture()
def runner(store):
    return Runner(store, Settings(provider="fake"))


def imgs(scene=None, n=1):
    return [demo_image(f"i{i}.png", scene if i == 0 else {}) for i in range(n)]


# --- стоимость -------------------------------------------------------------
def test_price_uses_longest_prefix():
    """gpt-4o-mini начинается с gpt-4o — первое совпадение завысило бы счёт."""
    assert price_of("gpt-4o-mini-2024-07-18", 1_000_000, 0) == pytest.approx(0.15)
    assert price_of("gpt-4o-2024-08-06", 1_000_000, 0) == pytest.approx(2.50)


def test_price_unknown_model_is_zero():
    """Незнакомую модель считаем по нулю, а не по чужому прайсу."""
    assert price_of("собственная-модель", 1_000_000, 1_000_000) == 0.0
    assert price_of("", 100, 100) == 0.0


def test_usage_tracker_reads_metadata():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    t = UsageTracker()
    msg = AIMessage(content="x")
    msg.usage_metadata = {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}
    t.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]))
    assert t.tokens_in == 900 and t.tokens_out == 100 and t.calls == 1


# --- кеш -------------------------------------------------------------------
def test_cache_key_is_order_independent():
    """Порядок ключей в параметрах не должен плодить разные записи кеша."""
    a = cache_key("s", ["h1", "h2"], {"x": 1, "y": 2}, "openai", "m")
    b = cache_key("s", ["h2", "h1"], {"y": 2, "x": 1}, "openai", "m")
    assert a == b


def test_cache_key_depends_on_model():
    """Ответ разных моделей подменять друг другом нельзя."""
    a = cache_key("s", ["h"], {}, "openai", "gpt-4o")
    b = cache_key("s", ["h"], {}, "openai", "gpt-4o-mini")
    assert a != b


def test_cache_key_depends_on_params():
    a = cache_key("s", ["h"], {"min_sos_pct": 30}, "openai", "m")
    b = cache_key("s", ["h"], {"min_sos_pct": 40}, "openai", "m")
    assert a != b


def test_second_identical_call_comes_from_cache(runner, store):
    """Главная экономия: тот же кадр не оплачивается дважды."""
    photos = imgs(SHELF)
    first = runner.run("retail-audit", photos, {"our_brand": "A"})
    second = runner.run("retail-audit", photos, {"our_brand": "A"})
    assert first.cached is False and second.cached is True
    assert second.data == first.data
    assert store.stats()["cache_hit_rate"] == 0.5


def test_different_params_are_not_cached_together(runner):
    photos = imgs(SHELF)
    runner.run("retail-audit", photos, {"our_brand": "A", "min_sos_pct": 30})
    second = runner.run("retail-audit", photos, {"our_brand": "A", "min_sos_pct": 90})
    assert second.cached is False
    assert second.data["sos_target_pct"] == 90


def test_no_cache_flag_forces_fresh_call(runner):
    photos = imgs(SHELF)
    runner.run("retail-audit", photos, {"our_brand": "A"})
    again = runner.run("retail-audit", photos, {"our_brand": "A"}, no_cache=True)
    assert again.cached is False


def test_cache_can_be_disabled_globally(store):
    r = Runner(store, Settings(provider="fake"), use_cache=False)
    photos = imgs(SHELF)
    r.run("retail-audit", photos, {"our_brand": "A"})
    assert r.run("retail-audit", photos, {"our_brand": "A"}).cached is False


def test_cache_respects_ttl():
    s = Store(":memory:", cache_ttl_s=0.05)
    s.cache_put("k", "svc", {"service": "svc", "data": {}})
    assert s.cache_get("k") is not None
    time.sleep(0.1)
    assert s.cache_get("k") is None, "просроченная запись не должна возвращаться"
    s.close()


def test_cache_records_savings(store):
    store.cache_put("k", "svc", {"service": "svc", "cost_usd": 0.02})
    store.cache_hit("k", 0.02)
    assert store.cache_stats() == {"entries": 1, "hits": 1, "cost_saved_usd": 0.02}


# --- бюджет ----------------------------------------------------------------
def test_budget_blocks_when_exhausted(store):
    store.log_run("x", "ok", cost_usd=5.0)
    r = Runner(store, Settings(provider="fake"), daily_budget_usd=1.0)
    with pytest.raises(BudgetExceeded, match="лимит"):
        r.run("retail-audit", imgs(SHELF), {"our_brand": "A"})


def test_budget_allows_within_limit(store):
    store.log_run("x", "ok", cost_usd=0.5)
    r = Runner(store, Settings(provider="fake"), daily_budget_usd=10.0)
    assert r.run("retail-audit", imgs(SHELF), {"our_brand": "A"}).service == "retail-audit"


def test_budget_ignores_old_spend(store):
    """Лимит суточный: вчерашние траты не должны блокировать сегодня."""
    store.log_run("x", "ok", cost_usd=99.0)
    with store._tx() as c:
        c.execute("UPDATE runs SET created_at=?", (time.time() - 86400 * 2,))
    r = Runner(store, Settings(provider="fake"), daily_budget_usd=1.0)
    assert r.run("retail-audit", imgs(SHELF), {"our_brand": "A"}) is not None


def test_zero_budget_means_unlimited(store):
    store.log_run("x", "ok", cost_usd=1000.0)
    r = Runner(store, Settings(provider="fake"), daily_budget_usd=0.0)
    assert r.run("retail-audit", imgs(SHELF), {"our_brand": "A"}) is not None


# --- ошибки и повторы ------------------------------------------------------
def test_bad_params_fail_before_budget_check(store):
    """Клиенту нужен внятный 400, а не «бюджет исчерпан» из-за опечатки."""
    store.log_run("x", "ok", cost_usd=99.0)
    r = Runner(store, Settings(provider="fake"), daily_budget_usd=1.0)
    with pytest.raises(ServiceError, match="неизвестные параметры"):
        r.run("retail-audit", imgs(SHELF), {"опечатка": 1})


def test_retries_on_transient_error(store, monkeypatch):
    calls = {"n": 0}
    svc = get_service("retail-audit", Settings(provider="fake"))
    original = svc.run_prepared

    def flaky(refs, tracker=None, **params):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Rate limit exceeded, try again")
        return original(refs, tracker=tracker, **params)

    monkeypatch.setattr(svc, "run_prepared", flaky)
    monkeypatch.setattr("vlmkit.runner.get_service", lambda *a, **k: svc)
    monkeypatch.setattr("vlmkit.runner.time.sleep", lambda s: None)

    r = Runner(store, Settings(provider="fake"), max_retries=3)
    assert r.run("retail-audit", imgs(SHELF), {"our_brand": "A"}) is not None
    assert calls["n"] == 3


def test_does_not_retry_permanent_error(store, monkeypatch):
    """Повторять ошибку в самом запросе — трата денег на тот же ответ."""
    calls = {"n": 0}
    svc = get_service("retail-audit", Settings(provider="fake"))

    def broken(refs, tracker=None, **params):
        calls["n"] += 1
        raise ServiceError("некорректные входные данные")

    monkeypatch.setattr(svc, "run_prepared", broken)
    monkeypatch.setattr("vlmkit.runner.get_service", lambda *a, **k: svc)

    r = Runner(store, Settings(provider="fake"), max_retries=3)
    with pytest.raises(ServiceError):
        r.run("retail-audit", imgs(SHELF), {"our_brand": "A"})
    assert calls["n"] == 1, "постоянную ошибку повторять нельзя"


def test_error_is_logged(store, monkeypatch):
    svc = get_service("retail-audit", Settings(provider="fake"))
    monkeypatch.setattr(svc, "run_prepared",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("бум")))
    monkeypatch.setattr("vlmkit.runner.get_service", lambda *a, **k: svc)
    r = Runner(store, Settings(provider="fake"), max_retries=0)
    with pytest.raises(RuntimeError):
        r.run("retail-audit", imgs(SHELF), {"our_brand": "A"})
    rows = store.runs(status="error")
    assert len(rows) == 1 and "бум" in rows[0]["error"]


# --- журнал ----------------------------------------------------------------
def test_run_is_logged_with_metrics(runner, store):
    runner.run("retail-audit", imgs(SHELF), {"our_brand": "A"}, client="acme")
    row = store.runs()[0]
    assert row["service"] == "retail-audit" and row["status"] == "ok"
    assert row["images_n"] == 1 and row["images_kb"] > 0
    assert row["client"] == "acme"


def test_stats_aggregate(runner, store):
    runner.run("retail-audit", imgs(SHELF), {"our_brand": "A"})
    runner.run("ux-critic", imgs({"text_samples": []}))
    s = store.stats()
    assert s["total_runs"] == 2
    assert {r["service"] for r in s["by_service"]} == {"retail-audit", "ux-critic"}
    assert s["success_rate"] == 1.0


def test_purge_removes_old_entries(store):
    store.log_run("svc", "ok")
    with store._tx() as c:
        c.execute("UPDATE runs SET created_at=?", (time.time() - 86400 * 40,))
    assert store.purge_runs(30) == 1
    assert store.runs() == []


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "p.db"
    a = Store(path)
    a.log_run("svc", "ok", cost_usd=0.01)
    a.cache_put("k", "svc", {"service": "svc", "data": {"x": 1}})
    a.close()

    b = Store(path)
    assert len(b.runs()) == 1
    assert b.cache_get("k")["data"] == {"x": 1}
    b.close()
