"""Двенадцать сервисов. `load_all()` импортирует и регистрирует все."""

from __future__ import annotations

import importlib

MODULES = (
    "pim_cards",          # 1. карточки товаров
    "retail_audit",       # 2. аудит выкладки
    "site_safety",        # 3. охрана труда на стройке
    "blueprint_estimator",  # 4. сметчик по чертежам
    "ux_critic",          # 5. UX/UI критик
    "trend_scout",        # 6. визуальные тренды
    "nutrition_plate",    # 7. фото-тарелка
    "sight_assistant",    # 8. помощник для слабовидящих
    "doc_extractor",      # 9. сложные документы
    "content_moderator",  # 10. модерация контента
    "appraiser",          # 11. оценщик антиквариата
    "repair_guide",       # 12. помощник по ремонту
)

_loaded = False


def load_all() -> None:
    global _loaded
    if _loaded:
        return
    for name in MODULES:
        importlib.import_module(f".{name}", __package__)
    _loaded = True
