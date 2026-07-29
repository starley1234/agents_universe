"""Тесты dataforge.mdm.matching: вероятностный матчинг, survivorship,
слияние в golden record, авто-слияние по порогу (guardrail), отклонение
кандидатов.

Реальный embedded PostgreSQL, реальный rapidfuzz (если недоступен —
тесты этого модуля пропускаются с понятным сообщением).
"""
from __future__ import annotations

import re
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
    _ = psycopg.__name__
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

try:
    import rapidfuzz  # type: ignore
    _ = rapidfuzz.__name__
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = (SKIP_REASON + "; " if SKIP_REASON else "") + "rapidfuzz не установлен"

_srv = None
if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="forge_mdm_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"


def _fresh_dsn() -> str:
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(_srv.get_uri(), autocommit=True)
    try:
        admin.execute(f"CREATE DATABASE {name}")
    finally:
        admin.close()
    return re.sub(r"/postgres(\?|$)", f"/{name}\\1", _srv.get_uri())


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
    if not HAVE_DEPS:
        print(f"test_mdm_matching: тесты пропущены — {SKIP_REASON}")
        return 0

    from dataforge.db.store import Store
    from dataforge.mdm.matching import (
        MdmError,
        apply_survivorship,
        auto_merge_high_confidence,
        compare_records,
        find_match_candidates,
        merge_candidate,
        reject_candidate,
    )

    st = Store(_fresh_dsn())

    section("compare_records: сходство строк и чисел")
    identical = compare_records({"name": "ООО Ромашка"}, {"name": "ООО Ромашка"},
                                ["name"])
    check("идентичные строки дают score 1.0", identical == 1.0)
    similar = compare_records({"name": "ООО Ромашка"}, {"name": "ОOO Ромашки"},
                              ["name"])
    check("похожие с опечаткой строки дают высокий, но не идеальный score",
         0.7 < similar < 1.0, str(similar))
    identical_case_insensitive = compare_records(
        {"name": "ООО Ромашка"}, {"name": "ооо  ромашка"}, ["name"])
    check("сравнение регистронезависимо (лишние пробелы токенизация игнорирует)",
         identical_case_insensitive == 1.0)
    different = compare_records({"name": "ООО Ромашка"}, {"name": "ИП Сидоров"},
                                ["name"])
    check("совсем разные строки дают низкий score", different < 0.5)
    empty_case = compare_records({"name": ""}, {"name": "x"}, ["name"])
    check("пустое значение даёт score 0 по этому полю", empty_case == 0.0)
    num_close = compare_records({"qty": 100}, {"qty": 101}, ["qty"])
    check("близкие числа дают высокий score", num_close > 0.9)
    weighted = compare_records(
        {"name": "X", "inn": "111"}, {"name": "Y", "inn": "111"},
        ["name", "inn"], weights={"name": 0.1, "inn": 10.0})
    check("веса влияют на итоговый score (совпадающий ИНН доминирует)",
         weighted > 0.9)
    try:
        compare_records({"a": 1}, {"a": 1}, ["a"], weights={"a": 0.0})
        check("сумма весов 0 -> MdmError", False)
    except MdmError:
        check("сумма весов 0 -> MdmError", True)

    section("apply_survivorship: приоритет источников и fallback")
    st.set_survivorship_rule("counterparty", "name", ["1c", "crm"])
    result = apply_survivorship(st, "counterparty", [
        ("crm", {"name": "ООО Ромашка (CRM)", "email": "crm@x.ru"}),
        ("1c", {"name": "ООО Ромашка (1С)", "inn": "1234567890"}),
    ])
    check("name взято из приоритетного источника 1c",
         result["name"] == "ООО Ромашка (1С)")
    check("поле без правила (email) взято как есть (fallback)",
         result["email"] == "crm@x.ru")
    check("поле без правила (inn) тоже взято", result["inn"] == "1234567890")

    result_no_rule = apply_survivorship(st, "part", [
        ("erp", {"sku": "A1"}), ("mes", {"sku": "A1-mes"}),
    ])
    check("без survivorship-правила берётся первое непустое значение",
         result_no_rule["sku"] == "A1")

    result_priority_empty = apply_survivorship(st, "counterparty", [
        ("crm", {"name": "Из CRM"}), ("1c", {"name": ""}),
    ])
    check("если приоритетный источник дал пустое значение — используется fallback",
         result_priority_empty["name"] == "Из CRM")

    section("find_match_candidates: попарное сравнение Silver-записей")
    sid = st.upsert_source("crm", "file", {})
    did = st.upsert_dataset(sid, "customers", layer="bronze")
    b1 = st.insert_bronze(did, {"name": "ООО Ромашка", "inn": "1234567890"})
    b2 = st.insert_bronze(did, {"name": "ооо  ромашка", "inn": "1234567890"})
    b3 = st.insert_bronze(did, {"name": "ЗАО Совсем Другое", "inn": "0000000000"})
    s1 = st.insert_silver(did, b1, {"name": "ООО Ромашка", "inn": "1234567890"})
    s2 = st.insert_silver(did, b2, {"name": "ооо  ромашка", "inn": "1234567890"})
    s3 = st.insert_silver(did, b3, {"name": "ЗАО Совсем Другое", "inn": "0000000000"})
    del s3

    candidates = find_match_candidates(st, "counterparty", did,
                                       fields=["name", "inn"], review_threshold=0.5)
    check("найден ровно 1 кандидат (похожая пара)", len(candidates) == 1)
    check("кандидат ссылается на правильные записи",
         {candidates[0]["record_a_id"], candidates[0]["record_b_id"]} == {s1, s2})
    check("score в разумном диапазоне (высокое сходство)",
         candidates[0]["score"] > 0.8)

    no_candidates = find_match_candidates(st, "counterparty", did,
                                          fields=["name"], review_threshold=1.01)
    check("при пороге выше максимально возможного score кандидатов нет",
         len(no_candidates) == 0)

    section("merge_candidate: подтверждение -> golden record")
    st.set_survivorship_rule("counterparty", "name", ["crm"])
    cand_id = candidates[0]["id"]
    gold_id = merge_candidate(st, cand_id, "counterparty", "human:tester")
    check("gold_id > 0", gold_id > 0)
    gold = st.get_gold_entity(gold_id)
    check("golden record содержит согласованные атрибуты",
         gold["attributes"]["inn"] == "1234567890")
    check("статус кандидата стал confirmed_match",
         st.get_match_candidate(cand_id)["decision"] == "confirmed_match")
    links = st.links_for_gold(gold_id)
    check("обе исходные Silver-записи привязаны", len(links) == 2)
    check("lineage: рёбра Silver->Gold добавлены",
         len(st.lineage_edges_into(f"gold:entity:{gold_id}")) == 2)

    section("merge_candidate: повторное слияние решённого кандидата -> MdmError")
    try:
        merge_candidate(st, cand_id, "counterparty", "human:tester2")
        check("повторное слияние решённого кандидата -> MdmError", False)
    except MdmError as exc:
        check("повторное слияние решённого кандидата -> MdmError", True)
        check("сообщение объясняет, что кандидат уже решён", "решён" in str(exc))

    section("merge_candidate: слияние с уже привязанной записью объединяет атрибуты")
    b4 = st.insert_bronze(did, {"name": "ООО Ромашка ИНН верный", "inn": "1234567890",
                               "phone": "+7-000"}, )
    s4 = st.insert_silver(did, b4, {"name": "ООО Ромашка ИНН верный",
                                    "inn": "1234567890", "phone": "+7-000"})
    cand2_id = st.create_match_candidate("counterparty", s1, s4, 0.9)
    gold_id2 = merge_candidate(st, cand2_id, "counterparty", "human:tester")
    check("слияние присоединилось к УЖЕ существующей golden record",
         gold_id2 == gold_id)
    check("новое поле (phone) добавилось в golden record",
         st.get_gold_entity(gold_id)["attributes"].get("phone") == "+7-000")

    section("reject_candidate: отклонение")
    b5 = st.insert_bronze(did, {"name": "Полностью другая компания", "inn": "9999999999"})
    s5 = st.insert_silver(did, b5, {"name": "Полностью другая компания", "inn": "9999999999"})
    cand3_id = st.create_match_candidate("counterparty", s1, s5, 0.55)
    ok = reject_candidate(st, cand3_id, "human:tester", reason="ложное совпадение")
    check("reject_candidate вернул True", ok)
    check("статус стал rejected", st.get_match_candidate(cand3_id)["decision"] == "rejected")
    try:
        reject_candidate(st, cand3_id, "human:tester")
        check("повторное отклонение решённого кандидата -> MdmError", False)
    except MdmError:
        check("повторное отклонение решённого кандидата -> MdmError", True)
    try:
        reject_candidate(st, 999999, "human:x")
        check("отклонение отсутствующего кандидата -> MdmError", False)
    except MdmError:
        check("отклонение отсутствующего кандидата -> MdmError", True)

    section("auto_merge_high_confidence: guardrail — порог соблюдается численно")
    b6 = st.insert_bronze(did, {"name": "Идентичная Копия Компании", "inn": "5555555555"})
    b7 = st.insert_bronze(did, {"name": "Идентичная Копия Компании", "inn": "5555555555"})
    s6 = st.insert_silver(did, b6, {"name": "Идентичная Копия Компании", "inn": "5555555555"})
    s7 = st.insert_silver(did, b7, {"name": "Идентичная Копия Компании", "inn": "5555555555"})
    high_score_cand = st.create_match_candidate("counterparty", s6, s7, 0.99)
    b8 = st.insert_bronze(did, {"name": "Частично Похожая Организация", "inn": "6666666666"})
    s8 = st.insert_silver(did, b8, {"name": "Частично Похожая Организация", "inn": "6666666666"})
    low_score_cand = st.create_match_candidate("counterparty", s6, s8, 0.70)

    merged = auto_merge_high_confidence(st, "counterparty", auto_threshold=0.9)
    check("кандидат с score >= порога слит автоматически",
         st.get_match_candidate(high_score_cand)["decision"] == "auto_merged")
    check("кандидат с score < порога остался pending (guardrail)",
         st.get_match_candidate(low_score_cand)["decision"] == "pending")
    check("auto_merge_high_confidence вернул хотя бы один gold_id", len(merged) >= 1)

    section("auto_merge_high_confidence: фильтр по entity_type")
    b9 = st.insert_bronze(did, {"name": "Деталь А", "sku": "P1"})
    s9 = st.insert_silver(did, b9, {"name": "Деталь А", "sku": "P1"})
    b10 = st.insert_bronze(did, {"name": "Деталь А копия", "sku": "P1"})
    s10 = st.insert_silver(did, b10, {"name": "Деталь А копия", "sku": "P1"})
    part_cand = st.create_match_candidate("part", s9, s10, 0.95)
    merged_cp_only = auto_merge_high_confidence(st, "counterparty", auto_threshold=0.9)
    check("auto_merge не трогает кандидатов другого entity_type",
         st.get_match_candidate(part_cand)["decision"] == "pending")
    del merged_cp_only

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
