-- =============================================================================
--  ХОЛДИНГ: MDM + СЕРТИФИКАЦИЯ + ЦИФРОВОЙ ДВОЙНИК (v5.7 / schema=public)
--  База: v5.3. Восстановлено: object_xref, установка GUC.
--  Исправлено: синхронизация object_codes при UPDATE/soft-delete.
--  PostgreSQL 14+ | Multi-tenant RLS | Partitioning | Vector | LTree
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
-- CREATE EXTENSION IF NOT EXISTS pg_cron;
SET search_path = public;

-- =============================================================================
--  БЛОК 1. НСИ (СПРАВОЧНИКИ)
-- =============================================================================
CREATE TABLE org_units (
    id        text PRIMARY KEY CHECK (id ~ '^[A-Z0-9_]+$'),
    parent_id text REFERENCES org_units(id),
    name      text NOT NULL,
    path      ltree NOT NULL DEFAULT ''
);
CREATE INDEX ix_org_path ON org_units USING GIST (path);

CREATE TABLE types (
    id           text PRIMARY KEY CHECK (id ~ '^[a-z0-9_]+$'),
    parent_id    text REFERENCES types(id),
    display_name text NOT NULL,
    path         ltree NOT NULL DEFAULT '',
    schema       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX ix_types_path ON types USING GIST (path);

CREATE TABLE sources (
    id    text PRIMARY KEY,
    name  text NOT NULL,
    kind  text NOT NULL,
    trust int  NOT NULL CHECK (trust BETWEEN 0 AND 100)
);

CREATE TABLE uom (
    code        text PRIMARY KEY,           -- Цифровой код (например, '355')
    base_code   text REFERENCES uom(code),  -- Ссылка на базовую единицу для конвертации
    factor      numeric NOT NULL DEFAULT 1.0, -- Множитель относительно базовой единицы
    name        text NOT NULL,              -- Наименование (Минута)
    -- Новые поля для соответствия ОКЕИ/ISO
    symbol_nat  text,                       -- Условное нац. (мин)
    symbol_intl text,                       -- Условное межд. (min)
    code_nat    text,                       -- Кодовое нац. (МИН)
    code_intl   text                        -- Кодовое межд. (MIN)
);
-- Индексы для быстрого поиска по кодам (для импорта/экспорта)
CREATE INDEX ix_uom_codes ON uom (code_nat, code_intl);

CREATE TABLE users (
    id        text PRIMARY KEY,
    name      text NOT NULL,
    org_id    text NOT NULL REFERENCES org_units(id),
    role      text NOT NULL DEFAULT 'viewer'
                CHECK (role IN ('viewer','editor','admin')),
    is_active boolean NOT NULL DEFAULT true
);

-- =============================================================================
--  БЛОК 2. ОБЪЕКТЫ (Секционирование по дате создания)
-- =============================================================================
-- Реестр уникальных кодов (из v5.3)
CREATE TABLE object_codes (
    master_code text NOT NULL,
    type_id     text NOT NULL REFERENCES types(id),
    org_id      text NOT NULL REFERENCES org_units(id),
    object_id   uuid NOT NULL,
    PRIMARY KEY (master_code, type_id, org_id)
);
-- Уникальность кода без учёта регистра
CREATE UNIQUE INDEX uk_object_codes_upper
    ON object_codes (upper(master_code), type_id, org_id);

CREATE TABLE objects (
    id          uuid NOT NULL DEFAULT uuid_generate_v4(),
    type_id     text NOT NULL REFERENCES types(id),
    org_id      text NOT NULL REFERENCES org_units(id),
    org_path    ltree NOT NULL DEFAULT '',
    master_code text NOT NULL,
    state       text NOT NULL DEFAULT 'draft',
    merged_into uuid,
    is_dirty    boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
CREATE INDEX ix_objects_id      ON objects (id);
CREATE INDEX ix_objects_orgpath ON objects USING GIST (org_path);

-- ВОССТАНОВЛЕНО из v5.2: кросс-референсы внешних источников
CREATE TABLE object_xref (
    source_id text NOT NULL REFERENCES sources(id),
    remote_id text NOT NULL,
    object_id uuid NOT NULL,
    PRIMARY KEY (source_id, remote_id)
);

CREATE TABLE IF NOT EXISTS code_series (
  type_id text NOT NULL REFERENCES types(id),
  org_id  text NOT NULL REFERENCES org_units(id),
  last_value bigint NOT NULL DEFAULT 0,
  prefix text,
  pad int NOT NULL DEFAULT 6,
  PRIMARY KEY (type_id, org_id)
);

-- =============================================================================
--  БЛОК 3. АТРИБУТЫ (EAV + Time-Travel + Partitioning)
-- =============================================================================
CREATE TABLE object_properties (
    id           bigserial,
    object_id    uuid NOT NULL,
    org_path     ltree NOT NULL DEFAULT '',
    key          text NOT NULL CHECK (key ~ '^[[:alpha:]][[:alnum:]_.]*$'),
    value        jsonb NOT NULL,
    uom_code     text,
    source_id    text NOT NULL REFERENCES sources(id),
    actor_id     text,
    confidence   float NOT NULL DEFAULT 1.0,
    valid_period tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
    is_current   boolean GENERATED ALWAYS AS (upper(valid_period) IS NULL) STORED,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
CREATE INDEX ix_prop_current ON object_properties (object_id, key) WHERE is_current;
CREATE INDEX ix_prop_obj_id  ON object_properties (object_id);
CREATE INDEX ix_prop_orgpath ON object_properties USING GIST (org_path);

-- =============================================================================
--  БЛОК 4. ГРАФЫ (EBOM/MBOM)
-- =============================================================================
CREATE TABLE object_links (
    id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id    uuid NOT NULL,
    child_id     uuid NOT NULL,
    link_type    text NOT NULL,
    qty          numeric NOT NULL DEFAULT 1.0,
    designator   text,
    valid_period tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
    CHECK (parent_id <> child_id)
);
CREATE INDEX ix_links_parent ON object_links (parent_id) WHERE upper(valid_period) IS NULL;

-- =============================================================================
--  БЛОК 5. БЕЙСЛАЙНЫ (Append-Only, hash-chain, compliance + signature)
-- =============================================================================
CREATE TABLE baselines (
    id             uuid NOT NULL DEFAULT uuid_generate_v4(),
    seq            bigserial,
    object_id      uuid NOT NULL,
    org_path       ltree NOT NULL DEFAULT '',
    code           text NOT NULL,
    snapshot       jsonb NOT NULL,
    snapshot_hash  text NOT NULL,
    prev_hash      text,
    compliance_ref jsonb NOT NULL DEFAULT '{}',
    signature      jsonb,
    signed_hash    text,
    actor_id       text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
CREATE INDEX ix_baselines_obj  ON baselines (object_id, created_at DESC);
CREATE INDEX ix_baselines_comp ON baselines USING GIN (compliance_ref jsonb_path_ops);

-- =============================================================================
--  БЛОК 6. АВТО-ПАРТИЦИОНИРОВАНИЕ (namespace = public)
-- =============================================================================
CREATE OR REPLACE FUNCTION ensure_partition(p_parent regclass, p_year int)
RETURNS void AS $$
DECLARE
    v_base  text;
    v_child text;
BEGIN
    SELECT c.relname INTO v_base FROM pg_class c WHERE c.oid = p_parent;
    v_child := format('%s_y%s', v_base, p_year);
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = v_child AND n.nspname = 'public'
    ) THEN
        EXECUTE format(
            'CREATE TABLE public.%I PARTITION OF public.%I FOR VALUES FROM (%L) TO (%L)',
            v_child, v_base,
            format('%s-01-01', p_year), format('%s-01-01', p_year + 1));
    END IF;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ensure_all_partitions() RETURNS void AS $$
DECLARE
    v_tab  text;
    v_year int := extract(year FROM now())::int;
BEGIN
    FOREACH v_tab IN ARRAY ARRAY['public.objects','public.object_properties','public.baselines']
    LOOP
        PERFORM ensure_partition(v_tab::regclass, v_year);
        PERFORM ensure_partition(v_tab::regclass, v_year + 1);
    END LOOP;
END $$ LANGUAGE plpgsql;

-- Стартовые секции + DEFAULT (2025, 2026 явно, как в v5.2).
CREATE TABLE objects_y2025    PARTITION OF objects FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE objects_y2026    PARTITION OF objects FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE objects_default  PARTITION OF objects DEFAULT;
CREATE TABLE object_properties_y2025 PARTITION OF object_properties FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE object_properties_y2026 PARTITION OF object_properties FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE object_properties_default PARTITION OF object_properties DEFAULT;
CREATE TABLE baselines_y2025  PARTITION OF baselines FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE baselines_y2026  PARTITION OF baselines FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE baselines_default PARTITION OF baselines DEFAULT;

-- =============================================================================
--  БЛОК 7. ЦИФРОВОЙ ДВОЙНИК И LLM
-- =============================================================================
CREATE TABLE object_state (
    object_id    uuid PRIMARY KEY,
    type_id      text,
    org_path     ltree,
    attributes   jsonb NOT NULL DEFAULT '{}',
    -- Новые поля:
    display_name text GENERATED ALWAYS AS (COALESCE(attributes->>'name', attributes->>'имя')) STORED,
    display_desc text GENERATED ALWAYS AS (COALESCE(attributes->>'description', attributes->>'описание')) STORED,
    search       tsvector,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_state_name ON object_state (display_name);
CREATE INDEX ix_state_org ON object_state USING GIST (org_path);
CREATE INDEX ix_state_gin ON object_state USING GIN (attributes jsonb_path_ops);

CREATE TABLE object_embeddings (
    object_id  uuid PRIMARY KEY,
    embedding  vector(1024) NOT NULL,
    model      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_emb_hnsw ON object_embeddings USING hnsw (embedding vector_cosine_ops);

-- =============================================================================
--  БЛОК 8. MULTI-TENANT ROW LEVEL SECURITY
--  Примечание: app.* здесь — имена custom GUC сессии, НЕ имя схемы.
-- =============================================================================
CREATE OR REPLACE FUNCTION get_app_user() RETURNS text AS $$
    SELECT NULLIF(current_setting('app.current_user_id', true), '');
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION get_app_role() RETURNS text AS $$
    SELECT COALESCE(NULLIF(current_setting('app.role', true), ''), 'viewer');
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION current_user_org_path() RETURNS ltree AS $$
    SELECT ou.path
    FROM users u JOIN org_units ou ON ou.id = u.org_id
    WHERE u.id = get_app_user() AND u.is_active;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION tenant_can_see(p_org_path ltree) RETURNS boolean AS $$
    SELECT get_app_role() = 'admin' OR p_org_path <@ current_user_org_path();
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION tenant_can_write(p_org_path ltree) RETURNS boolean AS $$
    SELECT get_app_role() = 'admin'
        OR (get_app_role() = 'editor' AND p_org_path <@ current_user_org_path());
$$ LANGUAGE sql STABLE;

ALTER TABLE objects           ENABLE ROW LEVEL SECURITY;
ALTER TABLE objects           FORCE  ROW LEVEL SECURITY;
ALTER TABLE object_properties ENABLE ROW LEVEL SECURITY;
ALTER TABLE object_properties FORCE  ROW LEVEL SECURITY;
ALTER TABLE baselines         ENABLE ROW LEVEL SECURITY;
ALTER TABLE baselines         FORCE  ROW LEVEL SECURITY;

CREATE POLICY p_obj_select ON objects FOR SELECT USING (tenant_can_see(org_path));
CREATE POLICY p_obj_insert ON objects FOR INSERT WITH CHECK (tenant_can_write(org_path));
CREATE POLICY p_obj_update ON objects FOR UPDATE USING (tenant_can_write(org_path))
                                              WITH CHECK (tenant_can_write(org_path));
CREATE POLICY p_obj_delete ON objects FOR DELETE USING (get_app_role() = 'admin');

CREATE POLICY p_prop_select ON object_properties FOR SELECT USING (tenant_can_see(org_path));
CREATE POLICY p_prop_insert ON object_properties FOR INSERT WITH CHECK (tenant_can_write(org_path));
CREATE POLICY p_prop_update ON object_properties FOR UPDATE USING (tenant_can_write(org_path))
                                                        WITH CHECK (tenant_can_write(org_path));
CREATE POLICY p_prop_delete ON object_properties FOR DELETE USING (get_app_role() = 'admin');

CREATE POLICY p_base_select ON baselines FOR SELECT USING (tenant_can_see(org_path));
CREATE POLICY p_base_insert ON baselines FOR INSERT WITH CHECK (tenant_can_write(org_path));

-- =============================================================================
--  БЛОК 9. ТРИГГЕРЫ И ЛОГИКА
-- =============================================================================
CREATE OR REPLACE FUNCTION tg_path() RETURNS trigger AS $$
BEGIN
    IF NEW.parent_id IS NULL THEN
        NEW.path := text2ltree(NEW.id);
    ELSE
        SELECT path || text2ltree(NEW.id) INTO NEW.path FROM org_units WHERE id = NEW.parent_id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_org_path
    BEFORE INSERT OR UPDATE OF parent_id ON org_units
    FOR EACH ROW EXECUTE FUNCTION tg_path();

CREATE OR REPLACE FUNCTION refresh_object_state(p_object_id uuid) RETURNS void AS $$
DECLARE v_org_path ltree; v_type text;
BEGIN
    SELECT o.org_path, o.type_id INTO v_org_path, v_type
    FROM objects o WHERE o.id = p_object_id ORDER BY o.created_at DESC LIMIT 1;
    IF v_org_path IS NULL THEN RETURN; END IF;
    INSERT INTO object_state(object_id, type_id, org_path, attributes, updated_at)
    SELECT p_object_id, v_type, v_org_path, COALESCE(jsonb_object_agg(key, value), '{}'), now()
    FROM object_properties WHERE object_id = p_object_id AND is_current
    ON CONFLICT (object_id) DO UPDATE SET
        attributes = EXCLUDED.attributes, org_path = EXCLUDED.org_path,
        type_id = EXCLUDED.type_id, updated_at = now();
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION upsert_property(
    p_object_id uuid, p_key text, p_value jsonb, p_source_id text, 
    p_uom text DEFAULT NULL, p_actor_id text DEFAULT NULL
) RETURNS text AS $$
DECLARE
    v_new_trust int; v_old_trust int; v_old_id bigint; v_old_ca timestamptz; v_org_path ltree;
BEGIN
    SELECT trust INTO v_new_trust FROM sources WHERE id = p_source_id;
    SELECT org_path INTO v_org_path
    FROM objects WHERE id = p_object_id ORDER BY created_at DESC LIMIT 1;
    SELECT id, created_at, (SELECT trust FROM sources WHERE id = source_id)
    INTO v_old_id, v_old_ca, v_old_trust
    FROM object_properties WHERE object_id = p_object_id AND key = p_key AND is_current;
    IF v_old_id IS NOT NULL THEN
        IF v_new_trust < v_old_trust THEN RETURN 'rejected_low_trust'; END IF;
        UPDATE object_properties SET valid_period = tstzrange(lower(valid_period), now(), '[)')
        WHERE id = v_old_id AND created_at = v_old_ca;
    END IF;
    INSERT INTO object_properties(object_id, org_path, key, value, uom_code, source_id, actor_id)
    VALUES (p_object_id, v_org_path, p_key, p_value, p_uom, p_source_id, p_actor_id);
    UPDATE objects SET is_dirty = true, updated_at = now() WHERE id = p_object_id;
    PERFORM refresh_object_state(p_object_id);
    RETURN 'ok';
END $$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE FUNCTION tg_baseline_chain() RETURNS trigger AS $$
BEGIN
    IF NEW.org_path = '' OR NEW.org_path IS NULL THEN
        SELECT org_path INTO NEW.org_path FROM objects
        WHERE id = NEW.object_id ORDER BY created_at DESC LIMIT 1;
    END IF;
    SELECT snapshot_hash INTO NEW.prev_hash FROM baselines
    WHERE object_id = NEW.object_id ORDER BY created_at DESC, seq DESC LIMIT 1;
    NEW.snapshot_hash := encode(digest(
        COALESCE(NEW.prev_hash,'GENESIS')
        || '|' || NEW.object_id::text
        || '|' || NEW.code
        || '|' || NEW.snapshot::text
        || '|' || NEW.compliance_ref::text
        || '|' || NEW.actor_id, 'sha256'), 'hex');
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_baseline_chain
    BEFORE INSERT ON baselines
    FOR EACH ROW EXECUTE FUNCTION tg_baseline_chain();

-- =============================================================================
--  БЛОК 10. ВЕРИФИКАЦИЯ ЦЕПОЧКИ
-- =============================================================================
CREATE OR REPLACE FUNCTION verify_baseline_chain(p_object_id uuid)
RETURNS TABLE (seq bigint, baseline_id uuid, created_at timestamptz, status text) AS $$
DECLARE
    r record; v_prev text := NULL; v_first boolean := true; v_recalc text; v_status text;
BEGIN
    FOR r IN
        SELECT * FROM baselines WHERE object_id = p_object_id
        ORDER BY created_at ASC, seq ASC
    LOOP
        v_recalc := encode(digest(
            COALESCE(r.prev_hash,'GENESIS')
            || '|' || r.object_id::text || '|' || r.code
            || '|' || r.snapshot::text || '|' || r.compliance_ref::text
            || '|' || r.actor_id, 'sha256'), 'hex');
        IF v_recalc <> r.snapshot_hash THEN
            v_status := 'HASH_MISMATCH';
        ELSIF v_first AND r.prev_hash IS NOT NULL THEN
            v_status := 'BROKEN_LINK';
        ELSIF NOT v_first AND r.prev_hash IS DISTINCT FROM v_prev THEN
            v_status := 'BROKEN_LINK';
        ELSIF r.signature IS NOT NULL AND r.signed_hash IS DISTINCT FROM r.snapshot_hash THEN
            v_status := 'SIGNED_HASH_DRIFT';
        ELSE
            v_status := 'OK';
        END IF;
        seq := r.seq; baseline_id := r.id; created_at := r.created_at; status := v_status;
        RETURN NEXT;
        v_prev := r.snapshot_hash; v_first := false;
    END LOOP;
END $$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION is_chain_valid(p_object_id uuid) RETURNS boolean AS $$
    SELECT NOT EXISTS (SELECT 1 FROM verify_baseline_chain(p_object_id) WHERE status <> 'OK');
$$ LANGUAGE sql STABLE;

-- ---------------------------------------------------------------------------
--  Дефолты объекта + синхронизация реестра кодов
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION tg_object_set_defaults() RETURNS trigger AS $$
BEGIN
    IF NEW.org_path IS NULL OR NEW.org_path = ''::ltree THEN
        SELECT path INTO NEW.org_path FROM org_units WHERE id = NEW.org_id;
    END IF;
    IF NEW.org_path IS NULL THEN
        NEW.org_path := text2ltree('HOLDING');
    END IF;
    -- Резервируем код в реестре только для «живых» объектов
    IF NEW.deleted_at IS NULL THEN
        INSERT INTO object_codes (master_code, type_id, org_id, object_id)
        VALUES (NEW.master_code, NEW.type_id, NEW.org_id, NEW.id);
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_objects_before_ins
    BEFORE INSERT ON objects
    FOR EACH ROW EXECUTE FUNCTION tg_object_set_defaults();

-- ФИКС РЕГРЕССА: при смене кода / soft-delete синхронизируем object_codes.
CREATE OR REPLACE FUNCTION tg_object_sync_codes() RETURNS trigger AS $$
BEGIN
    -- Soft-delete: освобождаем код
    IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
        DELETE FROM object_codes WHERE object_id = NEW.id;
        RETURN NEW;
    END IF;
    -- Восстановление из soft-delete
    IF NEW.deleted_at IS NULL AND OLD.deleted_at IS NOT NULL THEN
        INSERT INTO object_codes (master_code, type_id, org_id, object_id)
        VALUES (NEW.master_code, NEW.type_id, NEW.org_id, NEW.id);
        RETURN NEW;
    END IF;
    -- Смена кода / типа / орг-единицы у живого объекта
    IF NEW.deleted_at IS NULL AND
       (NEW.master_code, NEW.type_id, NEW.org_id)
       IS DISTINCT FROM (OLD.master_code, OLD.type_id, OLD.org_id) THEN
        DELETE FROM object_codes WHERE object_id = NEW.id;
        INSERT INTO object_codes (master_code, type_id, org_id, object_id)
        VALUES (NEW.master_code, NEW.type_id, NEW.org_id, NEW.id);
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_objects_after_upd
    AFTER UPDATE OF master_code, type_id, org_id, deleted_at ON objects
    FOR EACH ROW EXECUTE FUNCTION tg_object_sync_codes();

-- Жёсткое удаление объекта -> освобождаем код
CREATE OR REPLACE FUNCTION tg_object_del_codes() RETURNS trigger AS $$
BEGIN
    DELETE FROM object_codes WHERE object_id = OLD.id;
    RETURN OLD;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_objects_after_del
    AFTER DELETE ON objects
    FOR EACH ROW EXECUTE FUNCTION tg_object_del_codes();

-- 1. Создаем универсальную функцию для расчета пути типов
CREATE OR REPLACE FUNCTION tg_type_path() RETURNS trigger AS $$
BEGIN
    IF NEW.parent_id IS NULL THEN
        NEW.path := text2ltree(NEW.id);
    ELSE
        SELECT path || text2ltree(NEW.id) INTO NEW.path FROM types WHERE id = NEW.parent_id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

-- 2. Создаем триггер для таблицы types
CREATE TRIGGER trg_type_path
    BEFORE INSERT OR UPDATE OF parent_id ON types
    FOR EACH ROW EXECUTE FUNCTION tg_type_path();

-- 3. (Опционально) Обновляем существующие пути, если они уже сломаны
-- Это рекурсивное обновление для всех записей
UPDATE types SET parent_id = parent_id;

-- =============================================================================
--  БЛОК 11. ИНИЦИАЛИЗАЦИЯ
-- =============================================================================
INSERT INTO org_units(id, name) VALUES ('HOLDING', 'Главный офис');
INSERT INTO sources(id, name, kind, trust)
VALUES ('plm', 'Teamcenter', 'plm', 90), ('ai', 'AI Matcher', 'ai', 30);
INSERT INTO users(id, name, org_id, role)
VALUES ('admin', 'Системный админ', 'HOLDING', 'admin');
SELECT ensure_all_partitions();

-- ВОССТАНОВЛЕНО из v5.2: контекст сессии для RLS.
SET app.current_user_id = 'admin';
SET app.role            = 'admin';
