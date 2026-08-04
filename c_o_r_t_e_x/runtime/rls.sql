-- Starter migration for durable C.O.R.T.E.X. state.
-- Review with a DBA before production. `app.tenant_id` must be set by the
-- authenticated gateway connection/transaction.
CREATE TABLE IF NOT EXISTS cortex_state (
    namespace text NOT NULL,
    state_key text NOT NULL,
    value jsonb NOT NULL,
    version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL DEFAULT 'cortex',
    PRIMARY KEY (namespace, state_key)
);
CREATE INDEX IF NOT EXISTS cortex_state_namespace_idx ON cortex_state(namespace);
ALTER TABLE cortex_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS cortex_state_tenant_policy ON cortex_state;
CREATE POLICY cortex_state_tenant_policy ON cortex_state
    USING (
      current_setting('app.tenant_id', true) IS NULL
      OR namespace LIKE current_setting('app.tenant_id', true) || '/%'
    )
    WITH CHECK (
      current_setting('app.tenant_id', true) IS NULL
      OR namespace LIKE current_setting('app.tenant_id', true) || '/%'
    );

CREATE TABLE IF NOT EXISTS cortex_events (
    event_id uuid PRIMARY KEY,
    event_type text NOT NULL,
    correlation_id uuid NOT NULL,
    causation_id uuid,
    source text NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cortex_events_correlation_idx ON cortex_events(correlation_id, occurred_at);
