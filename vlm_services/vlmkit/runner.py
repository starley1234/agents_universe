"""Продовая обвязка вызова сервиса: кеш, повторы, бюджет, учёт расхода.

Сервисы из `services/` остаются чистыми — они про предметную область.
Всё, что делает их пригодными к эксплуатации за деньги, собрано здесь:

- **кеш** по хешам изображений и параметрам;
- **повторы** на сетевых сбоях и 429 — но не на ошибках самого запроса;
- **бюджет** на сутки: превышение останавливает сервис, а не разоряет;
- **учёт** токенов и стоимости по каждому прогону.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import replace
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from .config import Settings, settings as default_settings
from .core import Result, ServiceError, get_service
from .images import ImageError
from .store import Store, cache_key

log = logging.getLogger("vlmkit.runner")

# Цены за 1M токенов, ($вход, $выход). Для показа стоимости в интерфейсе
# и для контроля бюджета; уточняются под контракт с провайдером.
PRICES = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
}

# Ошибки, которые имеет смысл повторить: сеть моргнула, провайдер
# притормозил. Повторять 400 «плохая картинка» бессмысленно — это трата
# денег и времени на заведомо тот же ответ.
RETRYABLE = ("rate limit", "429", "timeout", "timed out", "connection",
             "temporarily", "503", "502", "504", "overloaded", "unavailable")


def price_of(model: str, tin: int, tout: int) -> float:
    """Стоимость по самому длинному совпавшему префиксу.

    Порядок важен: `gpt-4o-mini` начинается с `gpt-4o`, и наивный
    первый-подошедший завысил бы счёт в 16 раз.
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

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        usage: dict[str, Any] = {}
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
        self.calls += 1
        self.tokens_in += int(usage.get("prompt_tokens") or 0)
        self.tokens_out += int(usage.get("completion_tokens") or 0)


class BudgetExceeded(Exception):
    """Дневной лимит трат исчерпан."""


class Runner:
    """Выполняет сервис с кешем, повторами и контролем бюджета."""

    def __init__(self, store: Store | None = None, cfg: Settings | None = None,
                 max_retries: int = 2, daily_budget_usd: float = 0.0,
                 use_cache: bool = True):
        self.store = store
        self.cfg = cfg or default_settings()
        self.max_retries = max(0, max_retries)
        self.daily_budget_usd = daily_budget_usd
        self.use_cache = use_cache
        self._lock = threading.Lock()

    # --- бюджет -----------------------------------------------------------
    def spent_today(self) -> float:
        if self.store is None:
            return 0.0
        return self.store.spend_since(time.time() - 86400)

    def check_budget(self) -> None:
        if not self.daily_budget_usd or self.store is None:
            return
        spent = self.spent_today()
        if spent >= self.daily_budget_usd:
            raise BudgetExceeded(
                f"дневной лимит ${self.daily_budget_usd:.2f} исчерпан "
                f"(потрачено ${spent:.2f}); повысьте VLM_DAILY_BUDGET_USD "
                f"или дождитесь следующих суток")

    # --- запуск -----------------------------------------------------------
    def run(self, slug: str, images: Any = None, params: dict | None = None,
            provider: str | None = None, model: str | None = None,
            client: str | None = None, request_id: str | None = None,
            no_cache: bool = False) -> Result:
        params = dict(params or {})
        cfg = self.cfg
        if provider:
            cfg = replace(cfg, provider=provider)
        if model:
            cfg = replace(cfg, model=model)

        svc = get_service(slug, cfg)
        if images is None:
            demo = svc.demo()
            images, params = demo.get("images"), {**demo.get("params", {}), **params}

        # Ошибки входных данных ловим до бюджета и до сети: клиенту нужен
        # понятный 400, а не «лимит исчерпан».
        svc.check_params(params)
        refs = svc.prepare(images)

        self.check_budget()

        key = None
        if self.store is not None and self.use_cache and not no_cache:
            key = cache_key(slug, [r.sha256 for r in refs], params,
                            cfg.provider, cfg.resolved_model())
            cached = self.store.cache_get(key)
            if cached is not None:
                result = Result(**{**cached, "service": slug})
                # Сэкономлено ровно столько, сколько стоил исходный вызов:
                # эту сумму мы бы заплатили провайдеру повторно.
                saved = float(cached.get("cost_usd") or 0.0)
                result.cached = True
                result.cost_usd = 0.0
                self.store.cache_hit(key, saved)
                self.store.log_run(slug, "ok", provider=cfg.provider,
                                   model=cfg.resolved_model(), duration_s=0.0,
                                   images_n=len(refs),
                                   images_kb=sum(r.size_kb for r in refs),
                                   cached=True, warnings_n=len(result.warnings),
                                   client=client, request_id=request_id)
                log.info("%s: ответ из кеша (%s)", slug, key[:12])
                return result

        tracker = UsageTracker()
        svc._vlm = None  # пересоздать модель под возможный другой провайдер
        t0 = time.time()
        try:
            result = self._with_retries(svc, refs, params, tracker)
        except (ServiceError, ImageError) as exc:
            self._log_error(slug, cfg, refs, exc, client, request_id, time.time() - t0)
            raise
        except Exception as exc:  # noqa: BLE001
            self._log_error(slug, cfg, refs, exc, client, request_id, time.time() - t0)
            raise

        cost = price_of(cfg.resolved_model(), tracker.tokens_in, tracker.tokens_out)
        result.cost_usd = cost
        result.tokens_in = tracker.tokens_in
        result.tokens_out = tracker.tokens_out

        if self.store is not None:
            if key:
                self.store.cache_put(key, slug, result.as_dict())
            self.store.log_run(
                slug, "ok", provider=cfg.provider, model=cfg.resolved_model(),
                duration_s=result.duration_s, images_n=len(refs),
                images_kb=sum(r.size_kb for r in refs), tokens_in=tracker.tokens_in,
                tokens_out=tracker.tokens_out, cost_usd=cost,
                warnings_n=len(result.warnings), client=client, request_id=request_id)
        return result

    def _with_retries(self, svc: Any, refs: list, params: dict,
                      tracker: UsageTracker) -> Result:
        """Повторяем только то, что имеет шанс пройти со второй попытки."""
        attempt = 0
        while True:
            try:
                return svc.run_prepared(refs, tracker=tracker, **params)
            except (ServiceError, ImageError):
                raise  # ошибка запроса: повтор даст тот же результат
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if attempt >= self.max_retries or not any(w in msg for w in RETRYABLE):
                    raise
                attempt += 1
                delay = min(8.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.3)
                log.warning("%s: попытка %d после ошибки «%s», пауза %.1f с",
                            svc.slug, attempt, str(exc)[:120], delay)
                time.sleep(delay)

    def _log_error(self, slug: str, cfg: Settings, refs: list, exc: Exception,
                   client: str | None, request_id: str | None, dur: float) -> None:
        if self.store is None:
            return
        self.store.log_run(slug, "error", provider=cfg.provider,
                           model=cfg.resolved_model(), duration_s=round(dur, 3),
                           images_n=len(refs),
                           images_kb=sum(r.size_kb for r in refs),
                           error=f"{type(exc).__name__}: {exc}",
                           client=client, request_id=request_id)
