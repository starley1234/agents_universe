"""Схема PostgreSQL: staging + production + прослеживаемость.

ПОЧЕМУ ДВА СЛОЯ (ТЗ п.3.1). Staging хранит СЫРОЕ: что именно пришло из
Word-файла или из Teamcenter, без нормализации, вместе с байтовым
хешем источника. Production хранит нормализованное требование, с
которым работают агенты и инженер. Разделение нужно ровно для одного
вопроса, который в сертификации задают всегда: «а что было в исходном
документе?» Если бы импорт писал сразу в production, ответить было бы
нечем — нормализация необратима.

ПРОСЛЕЖИВАЕМОСТЬ КАК СВОЙСТВО СХЕМЫ, А НЕ КАК ОТЧЁТ. У каждого
требования есть: источник (source_document + staging-запись), полная
история изменений (requirement_revision — append-only), связь с пунктом
авиационных правил (requirement_rule_link), назначенный метод
подтверждения (compliance_item) и приложенное доказательство
(evidence). Отчёт о покрытии — это SELECT, а не отдельный подсчёт,
который может разойтись с данными.

ПОЧЕМУ ИЗМЕНЕНИЯ ТРЕБОВАНИЙ — ПРЕДЛОЖЕНИЯ, А НЕ ПРАВКИ (ТЗ п.6.2).
Агент никогда не меняет requirement.text напрямую: он создаёт
suggestion со статусом pending. Инженер видит diff и принимает или
отклоняет. Принятие пишет новую строку в requirement_revision и меняет
текст. Так у любого изменения есть автор, время, основание и
возможность отката — без этого система непригодна для процесса, за
которым стоит регулятор.

pgvector используется для семантического поиска (ТЗ п.4): подбор пункта
АП по смыслу требования и поиск дублей. Расширение создаётся при
инициализации; если его нет — понятная ошибка, а не падение на первом
запросе.
"""
from __future__ import annotations

#: Статусы требования в производственном слое.
REQUIREMENT_STATUSES = ("draft", "in_review", "approved", "rejected",
                        "obsolete")

#: Статусы предложения агента (ТЗ п.6.2: было/стало -> Принять).
SUGGESTION_STATUSES = ("pending", "accepted", "rejected", "superseded")

#: Методы подтверждения соответствия (Means of Compliance).
#: Коды MC0..MC9 — по Appendix A к AMC 21.A.15(b); та же система принята
#: в отечественной практике сертификации и понятна инженеру без перевода.
MOC_CODES = {
    "MC0": "Заявление о соответствии / ссылка на проектные данные",
    "MC1": "Анализ проекта (design review), описания и чертежи",
    "MC2": "Расчёт / анализ, обосновывающие отчёты",
    "MC3": "Оценка безопасности (safety assessment)",
    "MC4": "Лабораторные испытания",
    "MC5": "Наземные испытания на изделии",
    "MC6": "Лётные испытания",
    "MC7": "Инспекция проекта / аудит",
    "MC8": "Моделирование (simulation)",
    "MC9": "Квалификация оборудования",
}

#: Статусы пункта доказательной документации.
COMPLIANCE_STATUSES = ("planned", "in_progress", "submitted", "compliant",
                       "non_compliant", "not_applicable")


def schema_sql(schema: str, dim: int) -> str:
    """SQL создания схемы. Идемпотентно: CREATE ... IF NOT EXISTS."""
    s = schema
    return f"""
CREATE SCHEMA IF NOT EXISTS {s};

-- ================= СЛОЙ ИМПОРТА (STAGING, ТЗ п.3.1) =================

-- Документ-источник: файл Word/Excel или выгрузка из Teamcenter.
-- content_hash позволяет понять, что тот же файл импортируют повторно,
-- и не плодить дубли требований на ровном месте.
CREATE TABLE IF NOT EXISTS {s}.source_document(
  id            BIGSERIAL PRIMARY KEY,
  kind          TEXT NOT NULL,              -- word | excel | teamcenter | manual
  name          TEXT NOT NULL,
  uri           TEXT DEFAULT '',            -- путь к файлу или item_id в TC
  content_hash  TEXT DEFAULT '',            -- sha256 исходных байтов
  imported_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  imported_by   TEXT DEFAULT '',
  meta          JSONB NOT NULL DEFAULT '{{}}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_srcdoc_hash ON {s}.source_document(content_hash);

-- Сырая запись из источника. Ничего не нормализовано: как пришло, так и
-- лежит. raw JSONB хранит всё, что дал парсер (заголовки, таблицы,
-- атрибуты), чтобы позже можно было переразобрать без повторного
-- импорта файла.
CREATE TABLE IF NOT EXISTS {s}.staging_record(
  id            BIGSERIAL PRIMARY KEY,
  document_id   BIGINT NOT NULL REFERENCES {s}.source_document(id) ON DELETE CASCADE,
  ord           INTEGER NOT NULL DEFAULT 0,   -- порядок в исходном документе
  external_id   TEXT DEFAULT '',              -- [REQ-123] или item_id из TC
  section_path  TEXT DEFAULT '',              -- иерархия заголовков: 1 > 1.2
  raw_text      TEXT NOT NULL DEFAULT '',
  raw           JSONB NOT NULL DEFAULT '{{}}'::jsonb,
  -- new | promoted | duplicate | skipped
  status        TEXT NOT NULL DEFAULT 'new',
  note          TEXT DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_staging_doc ON {s}.staging_record(document_id, ord);
CREATE INDEX IF NOT EXISTS idx_staging_ext ON {s}.staging_record(external_id);

-- ================= ПРОИЗВОДСТВЕННЫЙ СЛОЙ (PRODUCTION) ================

-- Узел изделия (ТЗ п.3.3: «здоровье сертификации» по конкретному узлу).
CREATE TABLE IF NOT EXISTS {s}.product_node(
  id            BIGSERIAL PRIMARY KEY,
  code          TEXT NOT NULL UNIQUE,        -- АСДБ.04.32.8734.00.99
  name          TEXT NOT NULL DEFAULT '',
  parent_id     BIGINT REFERENCES {s}.product_node(id) ON DELETE SET NULL,
  tc_uid        TEXT DEFAULT '',             -- UID объекта в Teamcenter
  meta          JSONB NOT NULL DEFAULT '{{}}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_node_parent ON {s}.product_node(parent_id);

-- Требование. text — текущая (принятая) формулировка; менять её напрямую
-- нельзя нигде, кроме apply_suggestion/update_requirement, которые
-- обязаны писать revision.
CREATE TABLE IF NOT EXISTS {s}.requirement(
  id            BIGSERIAL PRIMARY KEY,
  external_id   TEXT NOT NULL,               -- REQ-123, уникален в рамках базы
  title         TEXT DEFAULT '',
  text          TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'draft',
  node_id       BIGINT REFERENCES {s}.product_node(id) ON DELETE SET NULL,
  owner         TEXT DEFAULT '',             -- инженер, за которым закреплено
  document_id   BIGINT REFERENCES {s}.source_document(id) ON DELETE SET NULL,
  staging_id    BIGINT REFERENCES {s}.staging_record(id) ON DELETE SET NULL,
  tc_uid        TEXT DEFAULT '',             -- UID в Teamcenter, если оттуда
  tc_synced_at  TIMESTAMPTZ,                 -- когда последний раз выгружали
  attributes    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
  -- Оценка Агента-Редактора [0..1] и его разбор; NULL = ещё не проверяли.
  quality_score REAL,
  quality       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
  embedding     vector({dim}),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(external_id)
);
CREATE INDEX IF NOT EXISTS idx_req_node ON {s}.requirement(node_id);
CREATE INDEX IF NOT EXISTS idx_req_owner ON {s}.requirement(owner);
CREATE INDEX IF NOT EXISTS idx_req_status ON {s}.requirement(status);

-- История изменений: append-only. Каждая правка текста/статуса требования
-- обязана оставить здесь строку — это и есть traceability.
CREATE TABLE IF NOT EXISTS {s}.requirement_revision(
  id            BIGSERIAL PRIMARY KEY,
  requirement_id BIGINT NOT NULL REFERENCES {s}.requirement(id) ON DELETE CASCADE,
  version       INTEGER NOT NULL,
  text_before   TEXT DEFAULT '',
  text_after    TEXT DEFAULT '',
  status_before TEXT DEFAULT '',
  status_after  TEXT DEFAULT '',
  reason        TEXT DEFAULT '',             -- почему изменено
  actor         TEXT NOT NULL DEFAULT '',    -- кто: инженер или agent:<имя>
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(requirement_id, version)
);

-- ================= АВИАЦИОННЫЕ ПРАВИЛА (ТЗ п.3.2) ====================

-- Пункт авиационных правил: АП-21, АП-25 и т.п. Загружается справочником;
-- embedding нужен для подбора по смыслу.
CREATE TABLE IF NOT EXISTS {s}.rule_clause(
  id            BIGSERIAL PRIMARY KEY,
  ruleset       TEXT NOT NULL,               -- АП-25
  clause        TEXT NOT NULL,               -- 25.1309
  title         TEXT NOT NULL DEFAULT '',
  text          TEXT NOT NULL DEFAULT '',
  keywords      TEXT NOT NULL DEFAULT '',    -- подстраховка для поиска без LLM
  embedding     vector({dim}),
  meta          JSONB NOT NULL DEFAULT '{{}}'::jsonb,
  UNIQUE(ruleset, clause)
);

-- Связь требования с пунктом правил. Предложена агентом -> подтверждена
-- человеком. confirmed отделён от предложенного намеренно: в отчёт для
-- регулятора попадают только подтверждённые связи.
CREATE TABLE IF NOT EXISTS {s}.requirement_rule_link(
  id            BIGSERIAL PRIMARY KEY,
  requirement_id BIGINT NOT NULL REFERENCES {s}.requirement(id) ON DELETE CASCADE,
  clause_id     BIGINT NOT NULL REFERENCES {s}.rule_clause(id) ON DELETE CASCADE,
  score         REAL DEFAULT 0,              -- уверенность классификатора
  source        TEXT NOT NULL DEFAULT 'agent',  -- agent | human | import
  confirmed     BOOLEAN NOT NULL DEFAULT FALSE,
  confirmed_by  TEXT DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(requirement_id, clause_id)
);

-- ============ ДОКАЗАТЕЛЬНАЯ ДОКУМЕНТАЦИЯ (ТЗ п.3.2 gap) ==============

-- Пункт доказательства: какой MoC назначен требованию и в каком он
-- состоянии. Отсутствие строки здесь = «дыра» в покрытии.
CREATE TABLE IF NOT EXISTS {s}.compliance_item(
  id            BIGSERIAL PRIMARY KEY,
  requirement_id BIGINT NOT NULL REFERENCES {s}.requirement(id) ON DELETE CASCADE,
  moc           TEXT NOT NULL,               -- MC0..MC9
  status        TEXT NOT NULL DEFAULT 'planned',
  responsible   TEXT DEFAULT '',
  planned_date  DATE,
  note          TEXT DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(requirement_id, moc)
);
CREATE INDEX IF NOT EXISTS idx_compl_req ON {s}.compliance_item(requirement_id);

-- Доказательство: отчёт, протокол испытаний, чертёж.
CREATE TABLE IF NOT EXISTS {s}.evidence(
  id            BIGSERIAL PRIMARY KEY,
  compliance_id BIGINT NOT NULL REFERENCES {s}.compliance_item(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL DEFAULT 'report',  -- report | test | drawing | audit
  title         TEXT NOT NULL DEFAULT '',
  uri           TEXT DEFAULT '',             -- ссылка на файл/датасет в TC
  issued_at     DATE,
  note          TEXT DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_evidence_compl ON {s}.evidence(compliance_id);

-- ============ ПРЕДЛОЖЕНИЯ АГЕНТОВ (ТЗ п.6.2: diff -> Принять) ========

CREATE TABLE IF NOT EXISTS {s}.suggestion(
  id            BIGSERIAL PRIMARY KEY,
  requirement_id BIGINT NOT NULL REFERENCES {s}.requirement(id) ON DELETE CASCADE,
  agent         TEXT NOT NULL,               -- editor | classifier | gap | plugin:<имя>
  kind          TEXT NOT NULL DEFAULT 'text',-- text | rule_link | moc | attribute
  text_before   TEXT DEFAULT '',
  text_after    TEXT DEFAULT '',
  payload       JSONB NOT NULL DEFAULT '{{}}'::jsonb,  -- для не-текстовых предложений
  rationale     TEXT DEFAULT '',             -- почему агент так считает
  score         REAL,
  status        TEXT NOT NULL DEFAULT 'pending',
  decided_by    TEXT DEFAULT '',
  decided_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sugg_status ON {s}.suggestion(status, requirement_id);

-- ============ ЖУРНАЛ (аудит действий системы и людей) ================

CREATE TABLE IF NOT EXISTS {s}.audit_log(
  id            BIGSERIAL PRIMARY KEY,
  ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor         TEXT NOT NULL DEFAULT '',    -- инженер или agent:<имя>
  -- import | promote | suggest | accept | export | tc_write | agent_run
  action        TEXT NOT NULL,
  object_type   TEXT DEFAULT '',
  object_id     BIGINT,
  detail        TEXT DEFAULT '',
  data          JSONB NOT NULL DEFAULT '{{}}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_obj ON {s}.audit_log(object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON {s}.audit_log(ts DESC);
"""


def vector_index_sql(schema: str) -> list[str]:
    """Индексы pgvector — отдельно от основной схемы.

    ivfflat строится по УЖЕ НАПОЛНЕННОЙ таблице (пустой список векторов
    даёт бесполезный индекс), поэтому создаются они по команде
    обслуживания, а не при инициализации.
    """
    return [
        f"CREATE INDEX IF NOT EXISTS idx_req_embedding ON {schema}.requirement "
        f"USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)",
        f"CREATE INDEX IF NOT EXISTS idx_clause_embedding ON {schema}.rule_clause "
        f"USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)",
    ]
