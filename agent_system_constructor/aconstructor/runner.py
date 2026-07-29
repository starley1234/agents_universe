"""Исполнитель прогонов: очередь, пул воркеров, учёт токенов и таймауты.

Пайплайны синхронные и упираются в сеть (вызовы LLM), поэтому очередь и
пул потоков, а не asyncio: так один медленный прогон не блокирует API, а
код пайплайнов остаётся простым, без заражения async.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import replace
from typing import Any, Callable

from langchain_core.callbacks import BaseCallbackHandler

from .config import Settings, settings as default_settings
from .core import get_pipeline, new_state
from .store import Run, RunStore

log = logging.getLogger("aconstructor.runner")

# Ориентировочные цены, $/1M токенов. Нужны, чтобы показывать стоимость
# прогона в интерфейсе; уточняются под конкретный контракт с провайдером.
PRICES = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
}


def price_of(model: str, tin: int, tout: int) -> float:
    """Стоимость вызова. Совпадение по самому длинному префиксу.

    Порядок перебора важен: `gpt-4o-mini` начинается с `gpt-4o`, и наивный
    первый-подошедший дал бы счёт в 16 раз больше реального.
    """
    if not model:
        return 0.0
    matches = [k for k in PRICES if model.startswith(k)]
    if not matches:
        return 0.0
    pin, pout = PRICES[max(matches, key=len)]
    return round(tin / 1e6 * pin + tout / 1e6 * pout, 6)


class UsageTracker(BaseCallbackHandler):
    """Считает токены по всем вызовам модели внутри одного прогона."""

    def __init__(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0
        self._lock = threading.Lock()

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        usage = {}
        try:
            usage = (response.llm_output or {}).get("token_usage") or {}
        except AttributeError:
            pass
        if not usage:
            for gens in getattr(response, "generations", []) or []:
                for g in gens:
                    meta = getattr(getattr(g, "message", None), "usage_metadata", None)
                    if meta:
                        usage = {"prompt_tokens": meta.get("input_tokens", 0),
                                 "completion_tokens": meta.get("output_tokens", 0)}
        with self._lock:
            self.calls += 1
            self.tokens_in += int(usage.get("prompt_tokens") or 0)
            self.tokens_out += int(usage.get("completion_tokens") or 0)

    def as_dict(self, model: str) -> dict[str, Any]:
        return {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "llm_calls": self.calls,
                "cost_usd": price_of(model, self.tokens_in, self.tokens_out)}


class RunTimeout(Exception):
    pass


def execute(pipeline: str, task: dict, cfg: Settings) -> tuple[dict, dict]:
    """Синхронно выполнить пайплайн. Возвращает (состояние, расход)."""
    p = get_pipeline(pipeline)
    graph = p.build(cfg=cfg)
    tracker = UsageTracker()
    state = graph.invoke(new_state(task), config={"callbacks": [tracker]})
    return state, tracker.as_dict(cfg.resolved_model())


class Runner:
    """Очередь прогонов с пулом воркеров.

    Очередь ограничена: при перегрузке лучше честно отказать (429), чем
    копить задачи, которые клиент уже не ждёт.
    """

    def __init__(self, store: RunStore, cfg: Settings | None = None,
                 workers: int = 2, max_queue: int = 100, timeout_s: float = 600.0):
        self.store = store
        self.cfg = cfg or default_settings()
        self.timeout_s = timeout_s
        self._q: queue.Queue[str] = queue.Queue(maxsize=max_queue)
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._active: dict[str, float] = {}
        self._lock = threading.Lock()
        self._n_workers = workers

    def start(self) -> None:
        stale = self.store.requeue_stale()
        if stale:
            log.warning("помечено как прерванные после рестарта: %d", stale)
        for i in range(self._n_workers):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("воркеров запущено: %d", self._n_workers)

    def stop(self, wait: float = 5.0) -> None:
        self._stop.set()
        for _ in self._threads:
            try:
                self._q.put_nowait("")
            except queue.Full:
                pass
        for t in self._threads:
            t.join(timeout=wait)
        self._threads.clear()

    def submit(self, pipeline: str, task: dict, provider: str | None = None,
               model: str | None = None, created_by: str | None = None) -> Run:
        get_pipeline(pipeline)  # ранняя проверка: неизвестный slug — сразу KeyError
        run = self.store.create(pipeline, task, provider or self.cfg.provider,
                                model or self.cfg.resolved_model(), created_by)
        try:
            self._q.put_nowait(run.id)
        except queue.Full:
            self.store.fail(run.id, "очередь переполнена, повторите позже")
            raise RuntimeError("очередь переполнена") from None
        return run

    def run_sync(self, pipeline: str, task: dict, provider: str | None = None,
                 model: str | None = None, created_by: str | None = None) -> Run:
        """Выполнить немедленно, минуя очередь — для CLI и коротких задач."""
        run = self.store.create(pipeline, task, provider or self.cfg.provider,
                                model or self.cfg.resolved_model(), created_by)
        self._execute(run.id)
        return self.store.get(run.id)  # type: ignore[return-value]

    @property
    def queue_depth(self) -> int:
        return self._q.qsize()

    @property
    def active(self) -> dict[str, float]:
        with self._lock:
            return {k: round(time.time() - v, 1) for k, v in self._active.items()}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if not run_id:
                break
            try:
                self._execute(run_id)
            except Exception:  # noqa: BLE001 — воркер не должен умирать
                log.exception("воркер: необработанная ошибка на %s", run_id)
            finally:
                self._q.task_done()

    def _execute(self, run_id: str) -> None:
        run = self.store.get(run_id)
        if run is None or run.status == "cancelled":
            return
        cfg = self.cfg
        if run.provider:
            cfg = replace(cfg, provider=run.provider)
        if run.model:
            cfg = replace(cfg, model=run.model)

        self.store.mark_running(run_id)
        with self._lock:
            self._active[run_id] = time.time()
        t0 = time.time()
        try:
            state, usage = _with_timeout(
                lambda: execute(run.pipeline, run.task, cfg), self.timeout_s)
            self.store.finish(run_id, state, usage)
            log.info("прогон %s (%s) готов за %.1f c, находок %d, $%.4f",
                     run_id, run.pipeline, time.time() - t0,
                     len(state.get("findings") or []), usage.get("cost_usd", 0))
        except RunTimeout:
            self.store.fail(run_id, f"превышен лимит времени {self.timeout_s:.0f} с")
            log.error("прогон %s: таймаут", run_id)
        except Exception as exc:  # noqa: BLE001
            self.store.fail(run_id, f"{type(exc).__name__}: {exc}")
            log.exception("прогон %s упал", run_id)
        finally:
            with self._lock:
                self._active.pop(run_id, None)


def _with_timeout(fn: Callable[[], Any], timeout_s: float) -> Any:
    """Выполнить с ограничением по времени.

    Поток не убиваем — в Python это небезопасно; мы перестаём его ждать и
    помечаем прогон упавшим. Поток-сирота завершится сам на следующем
    сетевом ответе, так как daemon=True.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        raise RunTimeout()
    if "error" in box:
        raise box["error"]
    return box["value"]
