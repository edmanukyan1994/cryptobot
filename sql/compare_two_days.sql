-- Сравнение двух календарных дней по закрытым сделкам (UTC).
-- Поменяй литералы дат в каждом блоке при необходимости.
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/compare_two_days.sql

WITH closed AS (
  SELECT
    (closed_at AT TIME ZONE 'UTC')::date AS day_utc,
    pnl_usdt,
    trade_type,
    setup_type,
    close_reason,
    EXTRACT(EPOCH FROM (closed_at - opened_at)) / 3600.0 AS hold_h,
    features_snapshot
  FROM crypto_demo_trades
  WHERE status = 'closed'
    AND closed_at IS NOT NULL
    AND opened_at IS NOT NULL
),
day AS (
  SELECT * FROM closed
  WHERE day_utc IN ('2026-04-22'::date, '2026-04-23'::date)
)
SELECT
  '0_daily_kpi' AS section,
  day_utc,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl,
  ROUND(AVG(pnl_usdt)::numeric, 1) AS avg_pnl,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pnl_usdt > 0) / NULLIF(COUNT(*), 0), 1) AS win_rate_pct,
  ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY hold_h)::numeric, 2) AS median_hold_h
FROM day
GROUP BY day_utc
ORDER BY day_utc;

WITH closed AS (
  SELECT
    (closed_at AT TIME ZONE 'UTC')::date AS day_utc,
    pnl_usdt,
    trade_type,
    setup_type,
    split_part(COALESCE(close_reason, ''), ',', 1) AS exit_primary,
    EXTRACT(EPOCH FROM (closed_at - opened_at)) / 3600.0 AS hold_h,
    features_snapshot
  FROM crypto_demo_trades
  WHERE status = 'closed'
    AND closed_at IS NOT NULL
    AND opened_at IS NOT NULL
),
day AS (
  SELECT * FROM closed
  WHERE day_utc IN ('2026-04-22'::date, '2026-04-23'::date)
)
SELECT
  '1_exit_primary_by_day' AS section,
  day_utc,
  exit_primary,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM day
GROUP BY day_utc, exit_primary
ORDER BY day_utc, sum_pnl ASC;

WITH closed AS (
  SELECT
    (closed_at AT TIME ZONE 'UTC')::date AS day_utc,
    pnl_usdt,
    trade_type,
    COALESCE(setup_type, '(null)') AS setup_type,
    features_snapshot
  FROM crypto_demo_trades
  WHERE status = 'closed'
    AND closed_at IS NOT NULL
),
day AS (
  SELECT * FROM closed
  WHERE day_utc IN ('2026-04-22'::date, '2026-04-23'::date)
)
SELECT
  '2_setup_type_by_day' AS section,
  day_utc,
  setup_type,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM day
GROUP BY day_utc, setup_type
ORDER BY day_utc, sum_pnl ASC;

WITH closed AS (
  SELECT
    (closed_at AT TIME ZONE 'UTC')::date AS day_utc,
    pnl_usdt,
    trade_type
  FROM crypto_demo_trades
  WHERE status = 'closed'
    AND closed_at IS NOT NULL
),
day AS (
  SELECT * FROM closed
  WHERE day_utc IN ('2026-04-22'::date, '2026-04-23'::date)
)
SELECT
  '3_long_short_by_day' AS section,
  day_utc,
  trade_type,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM day
GROUP BY day_utc, trade_type
ORDER BY day_utc, trade_type;

WITH closed AS (
  SELECT
    (closed_at AT TIME ZONE 'UTC')::date AS day_utc,
    pnl_usdt,
    COALESCE(features_snapshot->>'market_mode', '(null)') AS market_mode
  FROM crypto_demo_trades
  WHERE status = 'closed'
    AND closed_at IS NOT NULL
),
day AS (
  SELECT * FROM closed
  WHERE day_utc IN ('2026-04-22'::date, '2026-04-23'::date)
)
SELECT
  '4_market_mode_snapshot_by_day' AS section,
  day_utc,
  market_mode,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM day
GROUP BY day_utc, market_mode
ORDER BY day_utc, sum_pnl ASC;

WITH closed AS (
  SELECT
    (closed_at AT TIME ZONE 'UTC')::date AS day_utc,
    symbol,
    pnl_usdt
  FROM crypto_demo_trades
  WHERE status = 'closed'
    AND closed_at IS NOT NULL
),
day AS (
  SELECT * FROM closed
  WHERE day_utc IN ('2026-04-22'::date, '2026-04-23'::date)
)
SELECT
  '5_worst_symbols_22' AS section,
  symbol,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM day
WHERE day_utc = '2026-04-22'::date
GROUP BY symbol
ORDER BY sum_pnl ASC
LIMIT 12;

WITH closed AS (
  SELECT
    (closed_at AT TIME ZONE 'UTC')::date AS day_utc,
    symbol,
    pnl_usdt
  FROM crypto_demo_trades
  WHERE status = 'closed'
    AND closed_at IS NOT NULL
),
day AS (
  SELECT * FROM closed
  WHERE day_utc IN ('2026-04-22'::date, '2026-04-23'::date)
)
SELECT
  '6_worst_symbols_23' AS section,
  symbol,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM day
WHERE day_utc = '2026-04-23'::date
GROUP BY symbol
ORDER BY sum_pnl ASC
LIMIT 12;

WITH closed AS (
  SELECT
    (closed_at AT TIME ZONE 'UTC')::date AS day_utc,
    pnl_usdt,
    COALESCE(NULLIF(features_snapshot->>'entry_reason', ''), '(null)') AS entry_reason
  FROM crypto_demo_trades
  WHERE status = 'closed'
    AND closed_at IS NOT NULL
),
day AS (
  SELECT * FROM closed
  WHERE day_utc IN ('2026-04-22'::date, '2026-04-23'::date)
)
SELECT
  '7_entry_reason_top_loss_23' AS section,
  entry_reason,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM day
WHERE day_utc = '2026-04-23'::date
GROUP BY entry_reason
ORDER BY sum_pnl ASC
LIMIT 15;

WITH closed AS (
  SELECT
    (closed_at AT TIME ZONE 'UTC')::date AS day_utc,
    pnl_usdt,
    COALESCE(NULLIF(features_snapshot->>'entry_reason', ''), '(null)') AS entry_reason
  FROM crypto_demo_trades
  WHERE status = 'closed'
    AND closed_at IS NOT NULL
),
day AS (
  SELECT * FROM closed
  WHERE day_utc IN ('2026-04-22'::date, '2026-04-23'::date)
)
SELECT
  '8_entry_reason_top_win_22' AS section,
  entry_reason,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM day
WHERE day_utc = '2026-04-22'::date
GROUP BY entry_reason
ORDER BY sum_pnl DESC
LIMIT 15;
