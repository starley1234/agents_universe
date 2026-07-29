"""Тесты агентского слоя: редактор, классификатор, gap-аналитик.

Правила Агента-Редактора проверяются БЕЗ базы — это чистая функция от
текста, и она должна быть воспроизводима. Остальное — на настоящем
PostgreSQL, потому что агенты общаются через базу и результат их работы
(предложения, оценки, связи) хранится там же.

Особое внимание — честности агентов: классификатор не должен предлагать
пункт, в котором не уверен, и не должен принимать от модели пункты вне
списка кандидатов; gap-аналитик не должен молчать о конфликте статуса.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness                                                    # noqa: E402
from harness import (check, make_config, make_store, section,      # noqa: E402
                     skip_section, summary)
from saps.agents.editor import check_text                          # noqa: E402
from saps.agents.gap import suggest_moc                            # noqa: E402
from saps.llm.stub import StubLLM                                  # noqa: E402


def main() -> int:
    section("Агент-Редактор: правила без модели")
    good = check_text("Наработка на отказ блока управления должна быть не "
                      "менее 10000 ч.")
    check("хорошее требование получает высокую оценку", good.score >= 0.9,
          f"{good.score}: {[i.code for i in good.issues]}")
    check("проверяемость", good.checks["verifiable"] is True)
    check("измеримость", good.checks["measurable"] is True)
    check("однозначность", good.checks["unambiguous"] is True)

    vague = check_text("Система должна иметь высокую надёжность и достаточное "
                       "быстродействие.")
    check("двусмысленное требование забраковано", vague.score < 0.5,
          str(vague.score))
    check("названы конкретные слова",
          any(i.code == "vague" for i in vague.issues))
    check("в замечании есть фрагмент текста",
          any(i.fragment for i in vague.issues if i.code == "vague"))

    no_modal = check_text("Блок управления размещается в отсеке номер три "
                          "рядом с гидроагрегатом.")
    check("без модального глагола — не требование",
          any(i.code == "no_modal" for i in no_modal.issues))
    check("проверяемость = False", no_modal.checks["verifiable"] is False)

    weak = check_text("Конструкция может выдерживать нагрузку.")
    check("слабая модальность поймана",
          any(i.code == "weak_modal" for i in weak.issues))

    not_measurable = check_text("Температура в отсеке должна быть в пределах "
                                "рабочего диапазона.")
    check("количественное требование без числа поймано",
          any(i.code == "not_measurable" for i in not_measurable.issues),
          str([i.code for i in not_measurable.issues]))
    measurable = check_text("Температура в отсеке должна быть не более 60 °С.")
    check("число с единицей снимает замечание",
          not any(i.code == "not_measurable" for i in measurable.issues))

    multi = check_text("Система должна включаться автоматически, экипаж должен "
                       "получать сигнализацию, а бортовой регистратор обязан "
                       "фиксировать событие.")
    check("несколько требований в одном поймано",
          any(i.code == "not_atomic" for i in multi.issues))
    check("атомарность = False", multi.checks["atomic"] is False)

    check("пустой текст -> нулевая оценка", check_text("").score == 0.0)
    check("пустой текст помечен", check_text("").issues[0].code == "empty")
    check("оценка всегда в [0..1]",
          all(0.0 <= check_text(t).score <= 1.0 for t in
              ["", "должен", "высокая надёжность " * 50]))
    check("результат воспроизводим",
          check_text("Система должна работать").score
          == check_text("Система должна работать").score,
          "в сертификации оценка обязана быть детерминированной")

    section("Подсказка метода подтверждения (MoC)")
    cases = [
        ("Лётные испытания должны подтвердить управляемость", "MC6"),
        ("Прочность должна быть подтверждена расчётом нагрузок", "MC2"),
        ("Отказобезопасность системы при единичном отказе", "MC3"),
        ("Блок оборудования должен пройти квалификацию", "MC9"),
        ("Стендовые испытания образцов материала", "MC4"),
    ]
    for text, expected in cases:
        moc, reason = suggest_moc(text)
        check(f"«{text[:38]}…» -> {expected}", moc == expected,
              f"получили {moc!r}")
        check(f"  обоснование для {expected}", bool(reason))
    empty_moc, _ = suggest_moc("Некоторый абстрактный текст ни о чём")
    check("нет подсказки — честный пустой ответ", empty_moc == "",
          "случайный MC2 хуже отсутствия подсказки")

    if harness.server() is None:
        skip_section("Агенты на базе данных", harness.SKIP_REASON)
        return summary("Агентский слой")

    from saps.agents import ClassifierAgent, EditorAgent, GapAgent
    from saps.agents.classifier import index_clauses
    from saps.llm import build_embedder
    from saps.rules.loader import load_builtin

    st = make_store(dim=64)
    cfg = make_config(embedding_dim=64)
    emb = build_embedder("hash", "hash-64", dim=64)

    section("Агент-Редактор на базе")
    r1 = st.create_requirement("REQ-1", "Наработка на отказ должна быть не "
                                        "менее 10000 ч.", node_code="УЗЕЛ-1")
    r2 = st.create_requirement("REQ-2", "Система должна иметь высокую "
                                        "надёжность.", node_code="УЗЕЛ-1")
    report = EditorAgent(cfg, st).run()
    check("обработаны оба требования", report.processed == 2)
    check("проблемное найдено",
          any(f["external_id"] == "REQ-2" for f in report.findings))
    check("оценка записана в базу",
          st.get_requirement(r2)["quality_score"] is not None)
    check("хорошее требование получило высокую оценку",
          float(st.get_requirement(r1)["quality_score"]) >= 0.9)
    check("без LLM предложений о переформулировке нет",
          report.suggestions == [],
          "переформулировать без модели нечем — молча выдумывать нельзя")

    rep2 = EditorAgent(cfg, st).run(suggest_rewrite=True)
    check("запрос переформулировки без LLM объяснён",
          any("LLM не настроена" in e for e in rep2.errors))

    llm = StubLLM(scripted=[
        '{"improved": "Система должна обеспечивать наработку на отказ не '
        'менее [указать значение] ч.", "comment": "убрана двусмысленность"}'])
    rep3 = EditorAgent(cfg, st, llm).run(requirement_ids=[r2],
                                         suggest_rewrite=True)
    check("с LLM создано предложение", len(rep3.suggestions) == 1,
          str(rep3.to_dict()["counts"]))
    if rep3.suggestions:
        sug = st.get_suggestion(rep3.suggestions[0])
        check("предложение содержит diff",
              bool(sug["text_before"]) and bool(sug["text_after"]))
        check("в обосновании есть формальный разбор",
              "Формальный разбор" in sug["rationale"])
        check("текст требования НЕ изменён агентом",
              st.get_requirement(r2)["text"] == "Система должна иметь высокую "
                                                "надёжность.",
              "агент не имеет права менять данные сам")

    section("Агент-Редактор: защита от вырожденного ответа модели")
    short_llm = StubLLM(scripted=['{"improved": "Надёжно."}'])
    rep4 = EditorAgent(cfg, st, short_llm).run(requirement_ids=[r2],
                                               suggest_rewrite=True)
    check("укороченный вдвое ответ отвергнут", rep4.suggestions == [],
          "модель иногда возвращает пересказ вместо требования")

    section("Агент-Классификатор")
    load_builtin(st, embedder=emb)
    check("справочник загружен", st.stats()["clauses"] > 50)
    r3 = st.create_requirement(
        "REQ-3", "Система управления должна обеспечивать отказобезопасность "
                 "при единичном отказе любого элемента.")
    st.set_requirement_embedding(r3, emb.embed_one(
        st.get_requirement(r3)["text"]))

    cls = ClassifierAgent(cfg, st, emb)
    rep = cls.run(requirement_ids=[r3])
    check("классификатор что-то нашёл", len(rep.suggestions) > 0,
          str(rep.to_dict()["counts"]))
    links = st.requirement_links(r3)
    check("связь создана", len(links) > 0)
    check("связь НЕ подтверждена автоматически",
          all(not l["confirmed"] for l in links),
          "подтверждает человек — иначе непроверенная привязка попадёт в базис")
    check("верный пункт в кандидатах",
          any(l["clause"] in ("25.671", "25.1309") for l in links),
          str([l["clause"] for l in links]))
    check("у предложения есть обоснование",
          bool(st.get_suggestion(rep.suggestions[0])["rationale"]))

    section("Классификатор: честность порога")
    strict = make_config(embedding_dim=64, classify_min_score_hash=0.99)
    rep_strict = ClassifierAgent(strict, st, emb).run(requirement_ids=[r3])
    check("при высоком пороге агент молчит", rep_strict.suggestions == [])
    check("причина пропуска объяснена",
          rep_strict.skipped and "порога" in rep_strict.skipped[0]["reason"],
          str(rep_strict.skipped))

    section("Классификатор: модель не может назвать пункт вне списка")
    liar = StubLLM(scripted=[
        '{"matches": [{"clause": "25.9999", "score": 0.99, '
        '"reason": "выдуманный пункт"}]}'])
    rep_llm = ClassifierAgent(cfg, st, emb, liar).run(requirement_ids=[r3],
                                                      use_llm=True)
    all_clauses = {l["clause"] for l in st.requirement_links(r3)}
    check("выдуманный пункт не попал в связи", "25.9999" not in all_clauses,
          str(all_clauses))
    check("прогон не упал", rep_llm.processed == 1)

    picky = StubLLM(scripted=[
        '{"matches": [{"clause": "25.1309", "score": 0.95, '
        '"reason": "требование об отказах систем"}]}'])
    rep_ok = ClassifierAgent(cfg, st, emb, picky).run(requirement_ids=[r3],
                                                      use_llm=True)
    chosen = [l for l in st.requirement_links(r3) if l["clause"] == "25.1309"]
    check("выбор модели из списка принят", bool(chosen))
    check("оценка модели сохранена",
          chosen and float(chosen[0]["score"]) >= 0.9, str(chosen))

    section("Агент-Gap-аналитик")
    gap = GapAgent(cfg, st)
    rep = gap.run()
    kinds = {f["kind"] for f in rep.findings}
    check("найдены дыры без связи с АП", "no_rule_link" in kinds)
    check("найдены дыры без MoC", "no_moc" in kinds)
    check("предложены методы подтверждения", len(rep.suggestions) > 0)
    moc_sug = [st.get_suggestion(s) for s in rep.suggestions]
    check("предложения именно про MoC",
          all(s["kind"] == "moc" for s in moc_sug))
    check("в обосновании назван код и его смысл",
          all("Предлагается" in s["rationale"] for s in moc_sug))

    st.add_compliance_item(r1, "MC2")
    rep = gap.run()
    no_evidence = [f for f in rep.findings if f["kind"] == "no_evidence"]
    check("MoC без доказательства — тоже дыра",
          any(f["external_id"] == "REQ-1" for f in no_evidence))

    section("Gap: конфликт статуса важнее пустого места")
    item = st.add_compliance_item(r1, "MC3")
    st.set_compliance_status(item, "compliant")
    rep = gap.run()
    conflict = [f for f in rep.findings if f["kind"] == "status_conflict"]
    check("«соответствует» без доказательств помечено", bool(conflict))
    check("тяжесть — critical",
          conflict and conflict[0]["severity"] == "critical",
          "ложная уверенность опаснее отсутствия данных")

    section("Индикатор здоровья сертификации")
    health = gap.health()
    check("здоровье в [0..1]", 0.0 <= health["health"] <= 1.0)
    check("есть составляющие", set(health["factors"]) ==
          {"rule_link", "moc", "evidence", "quality"})
    check("веса дают в сумме 1",
          abs(sum(health["weights"].values()) - 1.0) < 1e-9)
    check("подсчитаны пробелы", health["gaps"]["no_moc"] >= 0)
    check("есть человекочитаемый статус", bool(health["status"]))
    check("блокирующие перечислены", "REQ-1" in health["blocking"])
    empty = gap.health(node_code="НЕТ-ТАКОГО")
    check("пустой срез не падает",
          empty["total"] == 0 and empty["health"] == 0.0)
    check("пустой срез помечен", empty["status"] == "нет данных")

    by_node = gap.health_by_node()
    check("здоровье по узлам считается", len(by_node) >= 1)
    check("узлы отсортированы по возрастанию готовности",
          all(by_node[i]["health"] <= by_node[i + 1]["health"]
              for i in range(len(by_node) - 1)),
          "худшее должно быть сверху")

    st.close()
    harness.cleanup()
    return summary("Агентский слой")


if __name__ == "__main__":
    raise SystemExit(main())
