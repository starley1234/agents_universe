"""Тесты maos.orchestrator.router: semantic router агентов.

Проверяется на РЕАЛЬНОМ hash-эмбеддере (детерминированный, без сети) —
семантическое сходство здесь не про "понимание смысла" (hash не умеет),
а про то, что векторная арифметика реализована и подключена корректно:
идентичные/похожие по составу слов тексты должны давать высокое
сходство, непохожие — низкое, и это должно решать выбор агента.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.llm.embeddings import HashEmbedder                      # noqa: E402
from maos.orchestrator.router import route          # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def main() -> int:
    emb = HashEmbedder(dim=128)

    def agent(slug, name, description, keywords="", with_embedding=True):
        e = emb.embed_one(f"{name} {description} {keywords}") if with_embedding else None
        return {"id": len(slug), "slug": slug, "name": name,
               "description": description, "keywords": keywords, "embedding": e}

    coder = agent("coder", "Coder", "Пишет и правит код на Python и JavaScript",
                  "код программирование баги")
    writer = agent("writer", "Writer", "Пишет статьи маркетинговые тексты рекламу",
                   "текст маркетинг реклама статья")
    analyst = agent("analyst", "Analyst", "Анализирует данные строит графики отчёты",
                    "данные аналитика отчёт")

    section("route: явное семантическое совпадение")
    d = route("Напиши код на Python для сортировки списка", [coder, writer, analyst], emb)
    check("выбран coder", d.agent_slug == "coder")
    check("метод semantic", d.method == "semantic")
    check("score > 0", d.score > 0)

    d2 = route("Напиши рекламный текст для нового продукта",
              [coder, writer, analyst], emb)
    check("выбран writer для рекламного текста", d2.agent_slug == "writer")

    d3 = route("Построй отчёт по данным продаж", [coder, writer, analyst], emb)
    check("выбран analyst для отчёта по данным", d3.agent_slug == "analyst")

    section("route: пустой список агентов")
    try:
        route("что угодно", [], emb)
        check("пустой список без default кидает ошибку", False)
    except ValueError:
        check("пустой список без default кидает ошибку", True)
    d4 = route("что угодно", [], emb, default_agent="coder")
    check("пустой список с default_agent возвращает его", d4.agent_slug == "coder")
    check("метод default", d4.method == "default")

    section("route: keyword-фолбэк, если у агента ещё нет эмбеддинга")
    no_emb_agent = agent("newbie", "Newbie", "программирование код отладка",
                         with_embedding=False)
    d5 = route("нужна помощь с кодом и отладкой", [no_emb_agent], emb)
    check("keyword-фолбэк сработал (нет эмбеддинга у единственного агента)",
         d5.agent_slug == "newbie")
    check("метод keyword", d5.method == "keyword")

    section("route: min_score отсекает неуверенные совпадения")
    unrelated = agent("unrelated", "Unrelated", "нечто совершенно постороннее")
    d6 = route("абсолютно неожиданный запрос ни о чём", [unrelated], emb,
              min_score=0.999)
    check("высокий порог заставляет откатиться на keyword/default",
         d6.method in ("keyword", "default"))

    section("route: детерминированность (важно для тестируемости системы)")
    d7a = route("Напиши код на Python", [coder, writer, analyst], emb)
    d7b = route("Напиши код на Python", [coder, writer, analyst], emb)
    check("одинаковый запрос даёт одинаковый выбор", d7a.agent_slug == d7b.agent_slug)
    check("одинаковый запрос даёт одинаковый score", d7a.score == d7b.score)

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
