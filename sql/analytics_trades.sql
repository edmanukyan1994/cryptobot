-- Аналитика закрытых сделок (демо). Запуск:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/analytics_trades.sql
--
-- Чтобы смотреть только последние N дней — в каждом блоке раскомментируйте
--   AND closed_at >= now() - interval '30 days'
-- в подзапросе `base`.

-- 1) Общие KPI
WITH base AS (
  SELECT *
  FROM crypto_demo_trades
  WHERE status = 'closed'
)
SELECT
  '1_kpi_overall' AS section,
  COUNT(*) AS closed_trades,
  ROUND(SUM(pnl_usdt)::numeric, 2) AS total_pnl,
  ROUND(AVG(pnl_usdt)::numeric, 2) AS avg_pnl,
  COUNT(*) FILTER (WHERE pnl_usdt > 0) AS wins,
  ROUND(
    100.0 * COUNT(*) FILTER (WHERE pnl_usdt > 0) / NULLIF(COUNT(*), 0),
    1
  ) AS win_rate_pct
FROM base;

-- 2) Причина закрытия (агрегированные префиксы)
WITH base AS (
  SELECT *
  FROM crypto_demo_trades
  WHERE status = 'closed'
),
reason_bucket AS (
  SELECT
    CASE
      WHEN close_reason IS NULL THEN '(null)'
      WHEN close_reason LIKE 'opposite_forecast%' THEN 'opposite_forecast*'
      WHEN close_reason LIKE 'weak_forecast%' THEN 'weak_forecast*'
      WHEN close_reason LIKE 'forecast_decay%' THEN 'forecast_decay*'
      ELSE close_reason
    END AS bucket,
    pnl_usdt
  FROM base
)
SELECT
  '2_by_close_reason_bucket' AS section,
  bucket AS close_reason,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl,
  ROUND(AVG(pnl_usdt)::numeric, 0) AS avg_pnl
FROM reason_bucket
GROUP BY bucket
ORDER BY sum_pnl ASC;

-- 3) Тип сетапа
WITH base AS (
  SELECT *
  FROM crypto_demo_trades
  WHERE status = 'closed'
)
SELECT
  '3_by_setup_type' AS section,
  COALESCE(setup_type, '(null)') AS setup_type,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl,
  ROUND(AVG(pnl_usdt)::numeric, 0) AS avg_pnl
FROM base
GROUP BY setup_type
ORDER BY sum_pnl ASC;

-- 4) Long / short
WITH base AS (
  SELECT *
  FROM crypto_demo_trades
  WHERE status = 'closed'
)
SELECT
  '4_by_trade_type' AS section,
  trade_type,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM base
GROUP BY trade_type;

-- 5) Режим рынка на входе (из снимка признаков)
WITH base AS (
  SELECT *
  FROM crypto_demo_trades
  WHERE status = 'closed'
)
SELECT
  '5_by_market_mode_snapshot' AS section,
  COALESCE(features_snapshot->>'market_mode', '(null)') AS market_mode,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM base
GROUP BY COALESCE(features_snapshot->>'market_mode', '(null)')
ORDER BY sum_pnl ASC;

-- 6a) Худшие символы
WITH base AS (
  SELECT *
  FROM crypto_demo_trades
  WHERE status = 'closed'
)
SELECT
  '6_worst_symbols' AS section,
  symbol,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM base
GROUP BY symbol
ORDER BY sum_pnl ASC
LIMIT 15;

-- 6b) Лучшие символы
WITH base AS (
  SELECT *
  FROM crypto_demo_trades
  WHERE status = 'closed'
)
SELECT
  '6_best_symbols' AS section,
  symbol,
  COUNT(*) AS n,
  ROUND(SUM(pnl_usdt)::numeric, 0) AS sum_pnl
FROM base
GROUP BY symbol
ORDER BY sum_pnl DESC
LIMIT 15;

-- 7) Открытые позиции
SELECT
  '7_open_positions' AS section,
  COUNT(*) AS open_cnt,
  ROUND(SUM(amount_usdt)::numeric, 0) AS exposure_usdt
FROM crypto_demo_trades
WHERE status = 'open';
