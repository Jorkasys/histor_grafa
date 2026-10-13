-- ===========================================================================
--  Схема базы historian2grafana. Выполняется при каждом старте приложения,
--  поэтому всё — «IF NOT EXISTS»: повторный запуск ничего не ломает.
-- ===========================================================================

-- Настройки приложения (подключение к историку и т. п.)
CREATE TABLE IF NOT EXISTS app_setting (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Справочник тегов: заполняется из dbo.Tag историка
CREATE TABLE IF NOT EXISTS tag (
    tag_id      serial PRIMARY KEY,
    name        text NOT NULL UNIQUE,           -- имя тега в историке (TagName)
    description text,
    unit        text,
    datatype    text NOT NULL DEFAULT 'float64', -- float64 | bool | string
    min_value   double precision,
    max_value   double precision,
    active      boolean NOT NULL DEFAULT true,   -- подходит под маски и копируется
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- До какого момента скопирован каждый тег
CREATE TABLE IF NOT EXISTS tag_sync (
    tag_id       integer PRIMARY KEY REFERENCES tag (tag_id) ON DELETE CASCADE,
    synced_until timestamptz NOT NULL,
    row_count    bigint NOT NULL DEFAULT 0,      -- строк этого тега в tag_value
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Значения тегов. quality: 0 — хорошее, 1 — плохое в историке (значение NULL)
CREATE TABLE IF NOT EXISTS tag_value (
    ts         timestamptz NOT NULL,
    tag_id     integer NOT NULL,
    value      double precision,
    value_text text,
    quality    smallint NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS tag_value_tag_ts_idx ON tag_value (tag_id, ts DESC);

-- Последнее значение каждого тега — удобно для своих запросов в Grafana
CREATE OR REPLACE VIEW tag_latest AS
SELECT t.tag_id, t.name, t.description, t.unit, t.datatype, t.active,
       v.ts, v.value, v.value_text, v.quality, s.synced_until, s.row_count
FROM tag t
LEFT JOIN LATERAL (
    SELECT x.ts, x.value, x.value_text, x.quality
    FROM tag_value x
    WHERE x.tag_id = t.tag_id
    ORDER BY x.ts DESC
    LIMIT 1) v ON true
LEFT JOIN tag_sync s ON s.tag_id = t.tag_id;
