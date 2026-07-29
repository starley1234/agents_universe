"""Синтетические, но правдоподобные входные данные для всех семи пайплайнов.

Данные вымышленные: номера патентов, компании и цены не соответствуют
реальным. Их задача — прогнать граф целиком и показать форму входа,
которую ждут продакшн-коннекторы.
"""

from __future__ import annotations

from typing import Any


# --- №1 патенты -----------------------------------------------------------
def patents() -> list[dict[str, Any]]:
    return [
        {
            "id": "US11987654B2",
            "title": "Дедупликация потоковых данных скользящим хешем",
            "assignee": "Northlake Storage Inc.",
            "date": "2026-07-14",
            "domain": "storage",
            "claim": (
                "A method comprising: computing a rolling hash over a sliding window of "
                "an incoming byte stream, determining chunk boundaries where the rolling "
                "hash satisfies a predetermined bitmask condition, computing a strong "
                "content fingerprint for each chunk, querying a distributed chunk index "
                "for the fingerprint, and storing only chunks absent from the index while "
                "recording a reference to existing chunks in a manifest."
            ),
        },
        {
            "id": "US12001122B1",
            "title": "Адаптивное квотирование запросов по прогнозу нагрузки",
            "assignee": "Ridgeway Systems LLC",
            "date": "2026-07-21",
            "domain": "networking",
            "claim": (
                "A system comprising: a predictor generating a forecast of request arrival "
                "rate from historical telemetry, a controller adjusting a token bucket "
                "refill rate based on the forecast, and a rejection module returning a "
                "retry-after hint computed from the forecast horizon."
            ),
        },
        {
            "id": "US11955001B2",
            "title": "Термостабилизация аккумуляторных модулей фазопереходным материалом",
            "assignee": "Kite Thermal GmbH",
            "date": "2026-07-09",
            "domain": "hardware",
            "claim": (
                "A battery pack comprising cells embedded in a phase change material "
                "matrix, a thermally conductive foam interposed between adjacent cells, "
                "and a temperature sensor controlling a coolant pump."
            ),
        },
    ]


def product() -> dict[str, Any]:
    return {
        "name": "ChunkVault (стартап-бэкап)",
        "source": "github.com/example/chunkvault",
        "description": (
            "ChunkVault splits an incoming byte stream into variable-size chunks. "
            "We slide a window over the stream and compute a rolling hash; when the "
            "rolling hash matches a predetermined bitmask we cut a chunk boundary. "
            "Each chunk gets a BLAKE3 content fingerprint. Before writing we query our "
            "distributed chunk index; chunks already present are skipped and we only "
            "record a reference in the per-file manifest. Uploads are rate limited by "
            "a static token bucket."
        ),
    }


# --- №2 запчасти ----------------------------------------------------------
def part_request() -> dict[str, Any]:
    return {
        "part_number": "HTL-4471-A2",
        "name": "Hydraulic servo valve, 2-stage flapper-nozzle",
        "drawing_notes": (
            "Двухкаскадный электрогидравлический сервоклапан. Рабочее давление 210 бар, "
            "расход 38 л/мин при перепаде 70 бар. Корпус — нержавеющая сталь 17-4PH, "
            "монтажный интерфейс ISO 10372-04-05-0-92 (типоразмер 05). Катушка 28 VDC, "
            "сопротивление 80 Ом, разъём MS3106-14S. Диапазон температур -40..+135 C. "
            "Уплотнения FKM. Масса 2.4 кг. Ответная плита с четырьмя отверстиями M6."
        ),
        "quantity": 2,
        "list_price_usd": 10400,
        "lead_time_weeks": 26,
    }


def supplier_listings() -> list[dict[str, Any]]:
    return [
        {
            "id": "L-1",
            "supplier": "Baltic Surplus OU (Эстония)",
            "raw_code": "HTL4471A2",
            "text": "Servo valve 2 stage, 210 bar, ISO 10372 size 05, 28V coil, new in box",
            "price_usd": 480,
            "qty": 3,
            "condition": "new surplus",
            "country": "EE",
        },
        {
            "id": "L-2",
            "supplier": "Al-Nakheel Trading (ОАЭ)",
            "raw_code": "SV-2ST-05",
            "text": (
                "Two stage flapper nozzle electrohydraulic servo valve, 17-4PH stainless "
                "body, 38 lpm at 70 bar drop, 28 VDC 80 ohm coil, MS3106 connector, FKM seals"
            ),
            "price_usd": 1250,
            "qty": 1,
            "condition": "overhauled",
            "country": "AE",
        },
        {
            "id": "L-3",
            "supplier": "Kansai Kikai (Япония)",
            "raw_code": "HTL-4471-B1",
            "text": "Servo valve size 05, 210 bar, coil 24 VDC 40 ohm, NBR seals",
            "price_usd": 2100,
            "qty": 5,
            "condition": "new",
            "country": "JP",
        },
        {
            "id": "L-4",
            "supplier": "Midwest Pump Parts (США)",
            "raw_code": "P-88213",
            "text": "Hydraulic pump gear section, 210 bar, cast iron housing, 3/4 NPT ports",
            "price_usd": 320,
            "qty": 12,
            "condition": "used",
            "country": "US",
        },
    ]


# --- №3 чертежи -----------------------------------------------------------
def drawing() -> dict[str, Any]:
    """Результат Vision-агента подан как «уже распознанный» слой векторов."""
    return {
        "sheet": "ТЭЦ-3 / лист 14 / схема питательной воды / 1974",
        "scale_mm_per_px": 12.5,
        "symbols": [
            {"id": "S1", "type": "pump", "tag": "Н-1", "xy": [120, 300]},
            {"id": "S2", "type": "gate_valve", "tag": "ЗД-11", "xy": [260, 300]},
            {"id": "S3", "type": "check_valve", "tag": "ОК-3", "xy": [380, 300]},
            {"id": "S4", "type": "heat_exchanger", "tag": "ПВД-5", "xy": [520, 300]},
            {"id": "S5", "type": "gate_valve", "tag": "ЗД-12", "xy": [660, 300]},
            {"id": "S6", "type": "tank", "tag": "Д-1", "xy": [800, 300]},
            {"id": "S7", "type": "pump", "tag": "Н-2", "xy": [120, 460]},
            {"id": "S8", "type": "gate_valve", "tag": "ЗД-21", "xy": [260, 460]},
            {"id": "S9", "type": "instrument", "tag": "PT-104", "xy": [450, 240]},
        ],
        "lines": [
            {"a": [130, 300], "b": [255, 300], "kind": "process"},
            {"a": [268, 300], "b": [375, 300], "kind": "process"},
            {"a": [388, 300], "b": [515, 300], "kind": "process"},
            {"a": [528, 300], "b": [655, 300], "kind": "process"},
            {"a": [668, 300], "b": [795, 300], "kind": "process"},
            {"a": [130, 460], "b": [255, 460], "kind": "process"},
            {"a": [268, 460], "b": [520, 460], "kind": "process"},
            {"a": [520, 460], "b": [520, 310], "kind": "process"},
            {"a": [450, 250], "b": [450, 300], "kind": "signal"},
        ],
        "texts": [
            {"text": "Ду150 Ру25 ст.20", "xy": [190, 285]},
            {"text": "Ду150 Ру25", "xy": [320, 285]},
            {"text": "ПВД-5 F=340 м2", "xy": [520, 270]},
            {"text": "Ду100 Ру25", "xy": [380, 445]},
        ],
    }


# --- №4 энергия -----------------------------------------------------------
def energy_site() -> dict[str, Any]:
    prices = [
        38, 35, 33, 32, 34, 41, 58, 74, 96, 88, 79, 72,
        70, 68, 71, 83, 112, 148, 131, 104, 82, 66, 52, 43,
    ]
    return {
        "site": "Литейный цех №2",
        "tariff": {
            "energy_usd_mwh": prices,
            "demand_charge_usd_per_kw": 18.5,
            "billing_peak_kw": 9400,
            "peak_window_hours": list(range(16, 21)),
        },
        "weather": {"temp_c": 31, "cloud_pct": 15, "wind_ms": 2},
        "baseline_kw": [
            2200, 2150, 2100, 2100, 2200, 2600, 3400, 4200, 5200, 5400, 5300, 5100,
            4900, 5000, 5200, 5800, 6400, 6600, 6100, 5200, 4300, 3300, 2700, 2400,
        ],
        "jobs": [
            {"id": "J1", "name": "Индукционная печь A", "kw": 2400, "hours": 3,
             "earliest": 6, "latest": 22, "preferred_start": 16, "shiftable": True},
            {"id": "J2", "name": "Индукционная печь B", "kw": 2400, "hours": 2,
             "earliest": 6, "latest": 22, "preferred_start": 17, "shiftable": True},
            {"id": "J3", "name": "Термокамера отпуска", "kw": 900, "hours": 4,
             "earliest": 0, "latest": 23, "preferred_start": 18, "shiftable": True},
            {"id": "J4", "name": "Линия формовки", "kw": 700, "hours": 8,
             "earliest": 8, "latest": 16, "preferred_start": 8, "shiftable": False},
        ],
    }


# --- №5 рецептура ---------------------------------------------------------
def gcms() -> dict[str, Any]:
    return {
        "sample": "Референс: премиальный кондиционер для белья «Amber Musk»",
        "peaks": [
            {"rt": 4.12, "area_pct": 18.4, "mz": [136, 121, 93], "hint": "limonene-like"},
            {"rt": 6.05, "area_pct": 12.1, "mz": [154, 139, 71], "hint": "linalool-like"},
            {"rt": 8.77, "area_pct": 9.6, "mz": [204, 161, 105], "hint": "sesquiterpene"},
            {"rt": 11.30, "area_pct": 21.8, "mz": [192, 164, 147], "hint": "coumarin-like"},
            {"rt": 14.02, "area_pct": 15.2, "mz": [258, 213, 109], "hint": "macrocyclic musk"},
            {"rt": 16.41, "area_pct": 8.3, "mz": [220, 191, 43], "hint": "woody amber"},
            {"rt": 18.90, "area_pct": 6.1, "mz": [278, 149], "hint": "phthalate/solvent"},
            {"rt": 21.15, "area_pct": 8.5, "mz": [246, 231, 187], "hint": "amber ketone"},
        ],
        "target_cost_usd_kg": 45.0,
    }


def ingredient_db() -> list[dict[str, Any]]:
    return [
        {"cas": "5989-27-5", "name": "d-Limonene", "rt": 4.10, "mz": [136, 121, 93],
         "family": "citrus", "price_usd_kg": 6, "ifra_max_pct": 20, "odor": "свежая цедра"},
        {"cas": "78-70-6", "name": "Linalool", "rt": 6.08, "mz": [154, 139, 71],
         "family": "floral", "price_usd_kg": 14, "ifra_max_pct": 15, "odor": "ландыш, свежесть"},
        {"cas": "87-44-5", "name": "Caryophyllene", "rt": 8.80, "mz": [204, 161, 105],
         "family": "spicy", "price_usd_kg": 22, "ifra_max_pct": 10, "odor": "перечно-древесный"},
        {"cas": "91-64-5", "name": "Coumarin", "rt": 11.28, "mz": [146, 118, 89],
         "family": "gourmand", "price_usd_kg": 19, "ifra_max_pct": 5, "odor": "сено, миндаль"},
        {"cas": "104-55-2", "name": "Cinnamal", "rt": 11.35, "mz": [132, 131, 103],
         "family": "spicy", "price_usd_kg": 12, "ifra_max_pct": 0.5, "odor": "корица"},
        {"cas": "24851-98-7", "name": "Hedione", "rt": 11.31, "mz": [192, 164, 147],
         "family": "floral", "price_usd_kg": 28, "ifra_max_pct": 40, "odor": "жасмин, прозрачность"},
        {"cas": "111879-80-2", "name": "Habanolide (macrocyclic musk)", "rt": 14.05,
         "mz": [258, 213, 109], "family": "musk", "price_usd_kg": 165, "ifra_max_pct": 30,
         "odor": "чистый мускус"},
        {"cas": "105-95-3", "name": "Ethylene brassylate", "rt": 14.20, "mz": [270, 227, 98],
         "family": "musk", "price_usd_kg": 38, "ifra_max_pct": 30, "odor": "мягкий мускус"},
        {"cas": "54464-57-2", "name": "Iso E Super", "rt": 16.38, "mz": [220, 191, 43],
         "family": "woody", "price_usd_kg": 34, "ifra_max_pct": 25, "odor": "бархатное дерево"},
        {"cas": "68155-66-8", "name": "Amber Core / Amberketal", "rt": 21.12,
         "mz": [246, 231, 187], "family": "amber", "price_usd_kg": 210, "ifra_max_pct": 15,
         "odor": "амбра, тепло"},
        {"cas": "1222-05-5", "name": "Galaxolide (polycyclic musk)", "rt": 21.20,
         "mz": [258, 243, 213], "family": "musk", "price_usd_kg": 26, "ifra_max_pct": 20,
         "odor": "пудровый мускус"},
        {"cas": "84-74-2", "name": "Dibutyl phthalate (solvent)", "rt": 18.88,
         "mz": [278, 149], "family": "solvent", "price_usd_kg": 4, "ifra_max_pct": 0,
         "odor": "нет (запрещён в косметике)"},
        {"cas": "57-55-6", "name": "Dipropylene glycol (DPG)", "rt": 18.92, "mz": [134, 75],
         "family": "solvent", "price_usd_kg": 3, "ifra_max_pct": 100, "odor": "нет"},
    ]


# --- №6 сертификация ------------------------------------------------------
def cert_project() -> dict[str, Any]:
    return {
        "product": "Инфузионный насос IP-200",
        "standard": "IEC 60601-1 + ISO 14971 (FDA 510(k))",
        "sections": [
            "Device Description",
            "Intended Use and Indications",
            "Risk Management File (ISO 14971)",
            "Electrical Safety Test Report (IEC 60601-1)",
            "Software Lifecycle (IEC 62304)",
            "Biocompatibility (ISO 10993)",
            "Substantial Equivalence Comparison",
        ],
        "evidence": [
            {"id": "E1", "kind": "test_log", "title": "Dielectric strength 4000 VAC, 1 min",
             "result": "pass", "date": "2026-05-02", "covers": ["Electrical Safety Test Report (IEC 60601-1)"]},
            {"id": "E2", "kind": "test_log", "title": "Leakage current, single fault 480 uA",
             "result": "pass", "date": "2026-05-03", "covers": ["Electrical Safety Test Report (IEC 60601-1)"]},
            {"id": "E3", "kind": "doc", "title": "FMEA, 84 позиции, RPN после мер <= 12",
             "result": "approved", "date": "2026-04-18", "covers": ["Risk Management File (ISO 14971)"]},
            {"id": "E4", "kind": "drawing", "title": "Сборочный чертёж корпуса, rev C",
             "result": "released", "date": "2026-03-11", "covers": ["Device Description"]},
            {"id": "E5", "kind": "doc", "title": "SOUP-список и план верификации ПО",
             "result": "approved", "date": "2026-06-01", "covers": ["Software Lifecycle (IEC 62304)"]},
            {"id": "E6", "kind": "doc", "title": "Predicate device K221234 сравнение по 12 параметрам",
             "result": "draft", "date": "2026-06-20",
             "covers": ["Substantial Equivalence Comparison", "Intended Use and Indications"]},
        ],
    }


# --- №7 участки -----------------------------------------------------------
def parcels() -> list[dict[str, Any]]:
    return [
        {
            "cadastre": "77:04:0002015:118", "address": "ул. Заводская, 14с3",
            "area_m2": 2400, "shape": {"w": 60, "h": 40},
            "zoning": "П-2 (производственно-складская)",
            "price_usd": 190000,
            "constraints": [
                {"type": "ЛЭП 110 кВ", "kind": "setback", "offset_m": 20, "side": "north"},
            ],
        },
        {
            "cadastre": "77:04:0002015:203", "address": "Промышленный пр-д, 7",
            "area_m2": 1500, "shape": {"w": 50, "h": 30},
            "zoning": "О-1 (общественно-деловая)",
            "price_usd": 240000,
            "constraints": [
                {"type": "Охранная зона газопровода", "kind": "setback", "offset_m": 10, "side": "south"},
                {"type": "Красная линия", "kind": "setback", "offset_m": 5, "side": "west"},
            ],
        },
        {
            "cadastre": "77:04:0002016:044", "address": "тупик Северный, 2",
            "area_m2": 900, "shape": {"w": 30, "h": 30},
            "zoning": "П-2 (производственно-складская)",
            "price_usd": 70000,
            "constraints": [
                {"type": "ЗОУИТ аэродрома (высота)", "kind": "height_limit", "max_height_m": 8},
                {"type": "Водоохранная зона", "kind": "setback", "offset_m": 12, "side": "east"},
            ],
        },
    ]


def building_types() -> list[dict[str, Any]]:
    return [
        {"name": "Автомойка на 2 поста", "w": 22, "d": 14, "height_m": 6,
         "parking": 4, "capex_usd": 210000, "noi_usd_year": 78000},
        {"name": "Склад-лайт 600 м2", "w": 40, "d": 15, "height_m": 9,
         "parking": 3, "capex_usd": 320000, "noi_usd_year": 96000},
        {"name": "Торговый павильон", "w": 18, "d": 12, "height_m": 5,
         "parking": 8, "capex_usd": 150000, "noi_usd_year": 62000},
    ]
