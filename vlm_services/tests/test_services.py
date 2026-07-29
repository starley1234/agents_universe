"""Предметные инварианты двенадцати сервисов.

Проверяем не «код не упал», а то, ради чего сервис существует: что доля
полки посчитана верно, контраст соответствует WCAG, опасный шаг ремонта
заблокирован, а спорная картинка ушла человеку.
"""

from __future__ import annotations

import pytest

from vlmkit import get_service, run_service
from vlmkit.demo import demo_image


def run(slug: str, scene: dict, n: int = 1, **params):
    svc = get_service(slug)
    imgs = [demo_image(f"i{i}.png", scene if i == 0 else {}) for i in range(n)]
    return svc.run(imgs, **params)


# --- №1 карточки товаров ---------------------------------------------------
def test_pim_truncates_title_at_word_boundary():
    long_title = "Кроссовки беговые мужские текстильные тёмно-синие с амортизацией подошвы"
    r = run("pim-cards", {"title": long_title, "attributes": {"brand": "X"}},
            marketplace="wildberries")
    assert r.data["title_len"] <= 60
    assert not r.data["title"].endswith(" ")
    assert " " not in r.data["title"][-1:], "не должно резать посреди слова"
    assert any("обрезан" in w for w in r.warnings)


def test_pim_respects_marketplace_limits():
    """У Ozon лимит 200 символов — тот же заголовок резать не надо."""
    long_title = "Кроссовки беговые мужские текстильные тёмно-синие с амортизацией подошвы"
    r = run("pim-cards", {"title": long_title}, marketplace="ozon")
    assert r.data["title"] == long_title


def test_pim_rejects_unknown_marketplace():
    from vlmkit.core import ServiceError

    with pytest.raises(ServiceError, match="площадка"):
        run("pim-cards", {}, marketplace="авито")


def test_pim_flags_banned_claims():
    r = run("pim-cards", {"title": "Лучший товар", "description": "Гарантия 100 процентов"})
    assert any("запрещённые" in w for w in r.warnings)
    assert not r.data["ready_to_publish"]


def test_pim_dedupes_keywords():
    r = run("pim-cards", {"keywords": ["кроссовки", "Кроссовки", "обувь", "кроссовки"]})
    assert r.data["keywords"] == ["кроссовки", "обувь"]


def test_pim_seller_brand_overrides_model():
    r = run("pim-cards", {"attributes": {"brand": "нечитаемо"}}, brand="Nike")
    assert r.data["attributes"]["brand"] == "Nike"


# --- №2 аудит выкладки -----------------------------------------------------
SHELF = {
    "facings": [
        {"brand": "Аква", "product": "0.5", "count": 10, "price_tag": True},
        {"brand": "Родник", "product": "0.5", "count": 30, "price_tag": True},
    ],
    "empty_slots": 0, "issues": [],
}


def test_retail_computes_share_of_shelf():
    r = run("retail-audit", SHELF, our_brand="Аква", min_sos_pct=30.0)
    assert r.data["total_facings"] == 40
    assert r.data["our_sos_pct"] == 25.0  # 10 из 40
    assert r.data["sos_ok"] is False


def test_retail_detects_missing_price_tag():
    scene = {"facings": [{"brand": "A", "product": "x", "count": 5, "price_tag": False}],
             "empty_slots": 0, "issues": []}
    r = run("retail-audit", scene)
    assert r.data["missing_price_tags"] == 1
    assert any(i["type"] == "нет ценника" for i in r.data["issues"])


def test_retail_planogram_gap():
    r = run("retail-audit", SHELF, planogram={"Аква": 50, "Родник": 50})
    akva = next(c for c in r.data["compliance"] if c["brand"] == "Аква")
    assert akva["gap_pct"] == -25.0 and akva["status"] == "недовыкладка"


def test_retail_flags_absent_brand_as_critical():
    r = run("retail-audit", SHELF, our_brand="Кристалл")
    assert any(i["severity"] == "critical" and "отсутствует" in i["type"]
               for i in r.data["issues"])


def test_retail_warns_on_cropped_shelf():
    """Обрезанная полка занижает SOS — об этом надо сказать, а не считать молча."""
    r = run("retail-audit", {**SHELF, "cropped": True})
    assert any("обрезана" in w for w in r.warnings)


# --- №3 охрана труда -------------------------------------------------------
def test_safety_separates_confident_from_doubtful():
    scene = {"people_count": 2, "violations": [
        {"rule": "no_helmet", "description": "без каски", "people": 1, "confidence": 0.95},
        {"rule": "no_vest", "description": "возможно без жилета", "people": 1,
         "confidence": 0.3}]}
    r = run("site-safety", scene, confidence_threshold=0.6)
    assert len(r.data["violations"]) == 1
    assert len(r.data["needs_review"]) == 1
    assert any("порога уверенности" in w for w in r.warnings)


def test_safety_triggers_stop_work_on_fall_risk():
    scene = {"people_count": 1, "violations": [
        {"rule": "no_harness", "description": "на высоте без страховки",
         "people": 1, "confidence": 0.9}]}
    r = run("site-safety", scene)
    assert r.data["stop_work_required"] is True
    assert r.data["risk_level"] in ("высокий", "критический")


def test_safety_no_stop_work_for_minor_violation():
    scene = {"people_count": 1, "violations": [
        {"rule": "no_gloves", "description": "без перчаток", "people": 1,
         "confidence": 0.9}]}
    r = run("site-safety", scene)
    assert r.data["stop_work_required"] is False


def test_safety_ranks_by_risk():
    scene = {"people_count": 3, "violations": [
        {"rule": "no_gloves", "description": "", "people": 1, "confidence": 0.9},
        {"rule": "crane_zone", "description": "", "people": 2, "confidence": 0.9}]}
    r = run("site-safety", scene)
    assert r.data["violations"][0]["rule"] == "crane_zone"


# --- №4 сметчик ------------------------------------------------------------
ROOMS = {"rooms": [{"name": "Гостиная", "length_m": 5.0, "width_m": 4.0,
                    "height_m": 3.0, "doors": 1, "windows": 1, "wet": False}]}


def test_estimator_computes_areas():
    r = run("blueprint-estimator", ROOMS, works=["laminate"])
    room = r.data["rooms"][0]
    assert room["floor_m2"] == 20.0
    # периметр 18 × высота 3 = 54, минус дверь 1.6 и окно 1.8
    assert room["wall_m2"] == pytest.approx(50.6, abs=0.1)


def test_estimator_adds_waste_margin():
    """Материал всегда берут с запасом на подрезку — иначе не хватит."""
    r = run("blueprint-estimator", ROOMS, works=["laminate"])
    lam = next(m for m in r.data["materials"] if m["key"] == "laminate")
    assert lam["qty"] == 20.0
    assert lam["qty_with_waste"] > lam["qty"]
    assert lam["waste_pct"] == 7


def test_estimator_detects_wet_room_by_name():
    scene = {"rooms": [{"name": "Санузел", "length_m": 2.0, "width_m": 2.0,
                        "doors": 1, "windows": 0}]}
    r = run("blueprint-estimator", scene, works=["tile"])
    assert r.data["rooms"][0]["wet"] is True
    assert any(m["key"] == "tile" for m in r.data["materials"])


def test_estimator_skips_room_without_dimensions():
    scene = {"rooms": [{"name": "Кладовая", "length_m": 0, "width_m": 0}]}
    r = run("blueprint-estimator", scene)
    assert r.data["rooms"] == []
    assert any("не распознаны" in w for w in r.warnings)


def test_estimator_computes_cost():
    r = run("blueprint-estimator", ROOMS, works=["laminate"], prices={"laminate": 1000})
    lam = next(m for m in r.data["materials"] if m["key"] == "laminate")
    assert lam["cost"] == pytest.approx(lam["qty_with_waste"] * 1000)
    assert r.data["total_cost"] > 0


def test_estimator_warns_without_prices():
    r = run("blueprint-estimator", ROOMS, works=["laminate"])
    assert r.data["total_cost"] == 0
    assert any("цены не заданы" in w for w in r.warnings)


# --- №5 UX-критик ----------------------------------------------------------
def test_ux_measures_contrast_by_wcag():
    """Чёрное на белом — 21:1, это эталон формулы."""
    scene = {"text_samples": [{"where": "текст", "fg_hex": "#000000",
                               "bg_hex": "#FFFFFF", "size": "normal"}]}
    r = run("ux-critic", scene)
    c = r.data["contrast_checks"][0]
    assert c["ratio"] == 21.0 and c["passes"]


def test_ux_flags_low_contrast_as_issue():
    scene = {"text_samples": [{"where": "подзаголовок", "fg_hex": "#CCCCCC",
                               "bg_hex": "#FFFFFF", "size": "normal"}]}
    r = run("ux-critic", scene)
    assert not r.data["contrast_checks"][0]["passes"]
    assert any(i["category"] == "contrast" for i in r.data["issues"])


def test_ux_missing_cta_is_critical():
    scene = {"primary_cta": {"text": "", "visible": False, "above_fold": False}}
    r = run("ux-critic", scene)
    assert any(i["severity"] == "critical" and i["category"] == "cta"
               for i in r.data["issues"])


def test_ux_impact_is_a_conservative_range():
    """Обещать точный процент роста конверсии по скриншоту нельзя."""
    scene = {"primary_cta": {"text": "", "visible": False}}
    r = run("ux-critic", scene)
    lo, hi = r.data["conversion_impact_range_pct"]
    assert 0 <= lo < hi <= 25


def test_ux_warns_when_no_colors_given():
    r = run("ux-critic", {"text_samples": []})
    assert any("не назвала цвета" in w for w in r.warnings)


# --- №6 тренды -------------------------------------------------------------
def test_trends_detect_growth_from_dates():
    svc = get_service("trend-scout")
    imgs, meta = [], []
    for i in range(6):
        fresh = i < 3
        imgs.append(demo_image(f"p{i}.png", {"frames": [
            {"name": f"p{i}", "features": ["бархат"] if fresh else ["лён"]}]}))
        meta.append({"date": f"2026-0{7 if fresh else 1}-10", "engagement": 100})
    r = svc.run(imgs, meta=meta)
    velvet = next(t for t in r.data["trends"] if t["feature"] == "бархат")
    assert velvet["direction"] == "растёт"
    linen = next(t for t in r.data["trends"] if t["feature"] == "лён")
    assert linen["direction"] == "угасает"


def test_trends_admit_small_sample():
    """По трём кадрам тренда нет — сервис обязан это сказать."""
    r = run("trend-scout", {"frames": [{"name": "a", "features": ["бархат"]}]}, n=2)
    assert any("гипотезы, а не тренды" in w for w in r.warnings)


def test_trends_warn_without_dates():
    svc = get_service("trend-scout")
    imgs = [demo_image(f"p{i}.png", {"frames": [{"name": f"p{i}",
            "features": ["стиль"]}]}) for i in range(6)]
    r = svc.run(imgs)
    assert any("нет дат" in w for w in r.warnings)


# --- №7 нутрициолог --------------------------------------------------------
MEAL = {"items": [{"name": "Рис", "grams": 100, "per100g":
                   {"protein_g": 2.7, "fat_g": 0.3, "carb_g": 28.0, "fiber_g": 0.4},
                   "confidence": 0.9}], "plate_scale_known": True}


def test_nutrition_computes_kcal_by_atwater():
    """Калории считаем сами: 2.7*4 + 28*4 + 0.3*9 = 125.5 ≈ 126."""
    r = run("nutrition-plate", MEAL)
    assert r.data["totals"]["kcal"] == 126


def test_nutrition_returns_honest_range():
    r = run("nutrition-plate", MEAL)
    lo, hi = r.data["kcal_range"]
    kcal = r.data["totals"]["kcal"]
    assert lo < kcal < hi
    assert r.data["uncertainty_pct"] == 30


def test_nutrition_warns_without_scale():
    r = run("nutrition-plate", {**MEAL, "plate_scale_known": False})
    assert any("масштаб не определён" in w for w in r.warnings)


def test_nutrition_skips_item_without_weight():
    scene = {"items": [{"name": "Соус", "grams": 0, "per100g": {"fat_g": 50}}],
             "plate_scale_known": True}
    r = run("nutrition-plate", scene)
    assert r.data["items"] == []
    assert any("масса не оценена" in w for w in r.warnings)


def test_nutrition_advises_on_fat_excess():
    scene = {"items": [{"name": "Масло", "grams": 100, "per100g":
                        {"protein_g": 0.5, "fat_g": 82.0, "carb_g": 0.8},
                        "confidence": 0.9}], "plate_scale_known": True}
    r = run("nutrition-plate", scene)
    assert any("жиры" in a for a in r.data["advice"])


def test_nutrition_always_carries_disclaimer():
    r = run("nutrition-plate", MEAL)
    assert "не медицинская рекомендация" in r.data["disclaimer"]
    assert "не медицинская рекомендация" in r.report


# --- №8 помощник для слабовидящих ------------------------------------------
def test_sight_puts_hazards_first():
    """Ступенька важнее вывески, даже если модель назвала её последней."""
    scene = {"objects": [{"what": "витрина магазина", "direction": "слева",
                          "distance_steps": 10}],
             "hazards": [{"what": "ступенька вниз", "direction": "прямо",
                          "distance_steps": 2, "confidence": 0.9}],
             "path_clear": False}
    r = run("sight-assistant", scene, mode="navigation")
    assert "ступенька" in r.data["speech"][0].lower()
    assert r.data["path_clear"] is False


def test_sight_keeps_phrases_short():
    """Длинную фразу в наушнике не дослушают."""
    scene = {"hazards": [{"what": "очень длинное описание препятствия " * 6,
                          "direction": "прямо", "distance_steps": 1,
                          "confidence": 0.9}]}
    r = run("sight-assistant", scene)
    assert all(len(s) <= 110 for s in r.data["speech"]), r.data["speech"]


def test_sight_hedges_uncertain_hazards():
    scene = {"hazards": [{"what": "яма", "direction": "прямо", "distance_steps": 2,
                          "confidence": 0.55}]}
    r = run("sight-assistant", scene)
    assert any("кажется" in s.lower() for s in r.data["speech"])


def test_sight_says_path_clear_when_safe():
    r = run("sight-assistant", {"hazards": [], "objects": [], "path_clear": True})
    assert any("свободен" in s.lower() for s in r.data["speech"])


def test_sight_reading_mode_returns_text():
    scene = {"text_found": [{"text": "Борщ 350 рублей", "where": "меню"}],
             "hazards": []}
    r = run("sight-assistant", scene, mode="reading")
    assert any("Борщ" in s for s in r.data["speech"])


def test_sight_reports_empty_frame():
    r = run("sight-assistant", {"hazards": [], "objects": [], "text_found": []})
    assert any("ничего не распознано" in w for w in r.warnings)


# --- №9 документы ----------------------------------------------------------
DOC = {"doc_type": "invoice", "fields": {"number": "1", "date": "01.01.2026",
                                         "supplier": "ООО", "total": "1000.00"},
       "line_items": [{"name": "товар", "qty": 10, "price": 100, "sum": 1000}],
       "field_confidence": {"number": 0.95, "date": 0.95, "supplier": 0.9,
                            "total": 0.95}}


def test_doc_verifies_line_items_sum():
    r = run("doc-extractor", DOC)
    check = next(c for c in r.data["checks"] if "сумма позиций" in c["name"])
    assert check["ok"] is True
    assert r.data["routing"] == "auto"


def test_doc_catches_sum_mismatch():
    """Расхождение итога с позициями — повод не пропускать документ автоматом."""
    bad = {**DOC, "fields": {**DOC["fields"], "total": "9999.00"}}
    r = run("doc-extractor", bad)
    assert any(not c["ok"] for c in r.data["checks"])
    assert r.data["routing"] == "manual_review"


def test_doc_validates_inn_checksum():
    good = {**DOC, "fields": {**DOC["fields"], "inn": "7707083893"}}
    assert next(c for c in run("doc-extractor", good).data["checks"]
                if "ИНН" in c["name"])["ok"] is True
    bad = {**DOC, "fields": {**DOC["fields"], "inn": "1234567890"}}
    assert next(c for c in run("doc-extractor", bad).data["checks"]
                if "ИНН" in c["name"])["ok"] is False


def test_doc_routes_unreadable_to_human():
    """Символ «?» в значении означает, что оператор должен посмотреть сам."""
    scene = {**DOC, "fields": {**DOC["fields"], "supplier": "ООО «Севе?ный»"}}
    r = run("doc-extractor", scene)
    assert r.data["routing"] == "manual_review"
    assert any("?" in reason for reason in r.data["routing_reasons"])


def test_doc_routes_low_confidence_to_human():
    scene = {**DOC, "field_confidence": {**DOC["field_confidence"], "total": 0.4}}
    r = run("doc-extractor", scene, min_confidence=0.75)
    assert r.data["routing"] == "manual_review"
    assert "total" in r.data["low_confidence_fields"]


def test_doc_flags_missing_required_fields():
    scene = {**DOC, "fields": {"number": "1"}}
    r = run("doc-extractor", scene, required_fields=["number", "date", "total"])
    assert set(r.data["missing_required"]) == {"date", "total"}
    assert r.data["routing"] == "manual_review"


# --- №10 модерация ---------------------------------------------------------
def test_moderation_blocks_confident_violation():
    scene = {"scores": {"hate": 0.95}, "ambiguous": False}
    r = run("content-moderator", scene)
    assert r.data["action"] == "block"


def test_moderation_allows_clean_image():
    scene = {"scores": {"hate": 0.01, "sexual": 0.0}, "ambiguous": False}
    r = run("content-moderator", scene)
    assert r.data["action"] == "allow"


def test_moderation_sends_grey_zone_to_human():
    """Серая зона — к модератору: и блокировка, и пропуск здесь дорого стоят."""
    scene = {"scores": {"hate": 0.55}, "ambiguous": False}
    r = run("content-moderator", scene)
    assert r.data["action"] == "review"
    assert r.data["human_review_required"] is True


def test_moderation_ambiguous_always_to_human():
    scene = {"scores": {"hate": 0.9}, "ambiguous": True,
             "ambiguity_reason": "возможна сатира"}
    r = run("content-moderator", scene)
    assert r.data["action"] == "review", "неоднозначное не блокируем автоматом"


def test_moderation_minors_force_review():
    scene = {"scores": {"sexual": 0.25}, "depicts_minors": True}
    r = run("content-moderator", scene)
    assert r.data["action"] == "review"
    assert any("несовершеннолетн" in w for w in r.warnings)


def test_moderation_strictness_changes_threshold():
    scene = {"scores": {"weapons": 0.25}, "ambiguous": False}
    assert run("content-moderator", scene, strictness="lenient").data["action"] == "allow"
    assert run("content-moderator", scene, strictness="strict").data["action"] == "limit"


# --- №11 оценщик -----------------------------------------------------------
COMPS = [{"price": 10000}, {"price": 20000}]


def test_appraiser_gives_two_scenarios():
    """По фото подлинность не доказать — отсюда две вилки, а не одна цифра."""
    r = run("appraiser", {"condition": "excellent", "defects": []}, n=3,
            comparables=COMPS)
    est = r.data["estimate"]
    assert est["if_authentic"][0] < est["if_authentic"][1]
    assert est["if_replica"][1] < est["if_authentic"][0]


def test_appraiser_condition_lowers_price():
    good = run("appraiser", {"condition": "excellent", "defects": []}, n=3,
               comparables=COMPS).data["estimate"]["if_authentic"]
    poor = run("appraiser", {"condition": "poor", "defects": []}, n=3,
               comparables=COMPS).data["estimate"]["if_authentic"]
    assert poor[1] < good[1]


def test_appraiser_defects_lower_price():
    clean = run("appraiser", {"condition": "good", "defects": []}, n=3,
                comparables=COMPS).data["estimate"]["if_authentic"][1]
    cracked = run("appraiser", {"condition": "good", "defects":
                  [{"type": "трещина", "where": "корпус", "severity": "high"}]},
                  n=3, comparables=COMPS).data["estimate"]["if_authentic"][1]
    assert cracked < clean


def test_appraiser_no_estimate_without_comparables():
    r = run("appraiser", {"condition": "good"}, n=3)
    assert r.data["estimate"] is None
    assert any("база сравнимых продаж" in w for w in r.warnings)


def test_appraiser_always_disclaims():
    r = run("appraiser", {"condition": "good"}, n=3, comparables=COMPS)
    assert any("очн" in w for w in r.warnings)


def test_appraiser_flags_replica_signs():
    r = run("appraiser", {"condition": "good", "replica_signs": ["современные винты"]},
            n=3, comparables=COMPS)
    assert r.data["expert_review_recommended"] is True
    assert "реплик" in r.data["authenticity_verdict"]


# --- №12 ремонт ------------------------------------------------------------
CAP = {"device": "Принтер", "device_confident": True,
       "visible_parts": [{"name": "конденсатор 450В", "position": "слева",
                          "state": "вздут"}],
       "hazards": ["capacitor"],
       "next_steps": [{"action": "Выпаять конденсатор", "target": "C7",
                       "tool": "паяльник", "caution": ""}]}


def test_repair_blocks_steps_until_safety_confirmed():
    """Совет лезть в конденсатор без разрядки — это удар током."""
    r = run("repair-guide", CAP)
    assert r.data["steps_blocked"] is True
    assert set(r.data["unmet_requirements"]) == {"обесточено", "разряжено"}
    assert all(s["blocked"] for s in r.data["next_steps"])


def test_repair_unblocks_after_confirmation():
    r = run("repair-guide", CAP, confirmed=["обесточено", "разряжено"])
    assert r.data["steps_blocked"] is False
    assert r.data["unmet_requirements"] == []


def test_repair_detects_hazard_from_part_names():
    """Опасность ловим и тогда, когда модель не перечислила её явно."""
    scene = {**CAP, "hazards": []}
    r = run("repair-guide", scene)
    assert any(h["key"] == "capacitor" for h in r.data["hazards"])


def test_repair_warns_novice_about_hazards():
    r = run("repair-guide", CAP, skill="novice")
    assert any("сервисному центру" in w for w in r.warnings)


def test_repair_no_hazard_no_block():
    scene = {"device": "Стул", "device_confident": True,
             "visible_parts": [{"name": "ножка", "position": "снизу", "state": "шатается"}],
             "hazards": [],
             "next_steps": [{"action": "Подтянуть болт", "target": "ножка",
                             "tool": "ключ", "caution": ""}]}
    r = run("repair-guide", scene)
    assert r.data["steps_blocked"] is False
