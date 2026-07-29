"""Журнал прогонов: жизненный цикл, устойчивость, сводка."""

from __future__ import annotations

import time

import pytest

from aconstructor.store import RunStore


@pytest.fixture()
def store(tmp_path):
    s = RunStore(tmp_path / "t.db")
    yield s
    s.close()


def test_create_and_get(store):
    run = store.create("energy-hacker", {"a": 1}, provider="fake", model="m")
    got = store.get(run.id)
    assert got.pipeline == "energy-hacker"
    assert got.status == "queued"
    assert got.task == {"a": 1}


def test_full_lifecycle(store):
    run = store.create("urban-scout", {})
    store.mark_running(run.id)
    assert store.get(run.id).status == "running"
    store.finish(run.id, {"report": "# ок", "findings": [1, 2, 3], "artifacts": {}},
                 {"tokens_in": 100, "tokens_out": 50, "cost_usd": 0.01})
    done = store.get(run.id)
    assert done.status == "done"
    assert done.findings_n == 3
    assert done.report == "# ок"
    assert done.cost_usd == 0.01
    assert done.duration_s is not None


def test_failure_is_recorded(store):
    run = store.create("doc-restorer", {})
    store.mark_running(run.id)
    store.fail(run.id, "провайдер недоступен")
    got = store.get(run.id)
    assert got.status == "failed"
    assert "недоступен" in got.error


def test_artifacts_saved_and_fetched(store):
    run = store.create("doc-restorer", {})
    store.finish(run.id, {"report": "r", "artifacts": {
        "revit_script": "x" * 200,      # файл на выгрузку
        "short": "мало",                # слишком короткое
        "sheet": "y" * 200,             # служебное состояние, не файл
    }}, {})
    names = {a["name"] for a in store.artifacts(run.id)}
    assert names == {"revit_script"}, "в артефакты идут только файлы на выгрузку"
    art = store.artifact(run.id, "revit_script")
    assert art["kind"] == "py" and len(art["content"]) == 200


def test_cancel_only_queued(store):
    a = store.create("urban-scout", {})
    assert store.cancel(a.id) is True
    assert store.get(a.id).status == "cancelled"

    b = store.create("urban-scout", {})
    store.mark_running(b.id)
    assert store.cancel(b.id) is False, "работающий прогон отменять нечем"


def test_requeue_stale_after_restart(store):
    a = store.create("urban-scout", {})
    store.mark_running(a.id)
    store.create("urban-scout", {})
    assert store.requeue_stale() == 2
    assert store.get(a.id).status == "failed"
    assert "рестарт" in store.get(a.id).error


def test_survives_reopen(tmp_path):
    """Главное свойство журнала: прогон переживает перезапуск процесса."""
    path = tmp_path / "p.db"
    s1 = RunStore(path)
    run = s1.create("energy-hacker", {"x": 1})
    s1.finish(run.id, {"report": "# сохранено", "findings": []}, {})
    s1.close()

    s2 = RunStore(path)
    got = s2.get(run.id)
    assert got is not None and got.report == "# сохранено"
    s2.close()


def test_list_filters(store):
    store.create("a-pipe", {})
    b = store.create("b-pipe", {})
    store.mark_running(b.id)
    assert len(store.list()) == 2
    assert len(store.list(pipeline="a-pipe")) == 1
    assert len(store.list(status="running")) == 1
    assert store.list(limit=1)[0].pipeline == "b-pipe", "сортировка новые сверху"


def test_stats_aggregates(store):
    for i in range(3):
        r = store.create("p", {})
        store.finish(r.id, {"report": "", "findings": [1]}, {"cost_usd": 0.5})
    f = store.create("p", {})
    store.fail(f.id, "ошибка")
    s = store.stats()
    assert s["total"] == 4
    assert s["completed"] == 3
    assert s["total_cost_usd"] == 1.5
    assert s["total_findings"] == 3
    assert s["success_rate"] == 0.75


def test_summary_excludes_heavy_fields(store):
    run = store.create("p", {"big": "x" * 1000})
    store.finish(run.id, {"report": "y" * 1000, "findings": []}, {})
    d = store.get(run.id).summary()
    assert "result" not in d and "report" not in d and "task" not in d
    assert d["id"] and d["status"] == "done"


def test_purge_removes_old_runs_and_artifacts(store):
    run = store.create("p", {})
    store.finish(run.id, {"report": "r", "artifacts": {"a_md": "z" * 100}}, {})
    store._conn.execute("UPDATE runs SET created_at=? WHERE id=?",
                        (time.time() - 40 * 86400, run.id))
    store._conn.commit()
    assert store.purge(older_than_days=30) == 1
    assert store.get(run.id) is None
    assert store.artifacts(run.id) == []
