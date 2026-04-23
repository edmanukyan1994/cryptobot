-- Закрытые сделки за один календарный день (UTC).
-- 1) Поменяй дату в WHERE (одна строка ниже).
-- 2) Запуск из каталога проекта:
--      cd /Users/edgarmanukyan/Desktop/cryptobot
--      set -a && source .env && set +a
--      psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/export_closed_trades_by_day.sql
--
-- CSV: chmod +x scripts/export_closed_trades_csv.sh && ./scripts/export_closed_trades_csv.sh 2026-04-23

SELECT
  id,
  symbol,
  trade_type,
  round(amount_usdt::numeric, 2) AS amount_usdt,
  round(amount_crypto::numeric, 8) AS amount_crypto,
  round(entry_price::numeric, 8) AS entry_price,
  round(COALESCE(exit_price, 0)::numeric, 8) AS exit_price,
  round(COALESCE(sl_price, 0)::numeric, 8) AS sl_price,
  opened_at,
  closed_at,
  round((EXTRACT(EPOCH FROM (closed_at - opened_at)) / 3600.0)::numeric, 2) AS hold_hours,
  close_reason,
  setup_type,
  features_snapshot->>'entry_reason' AS entry_reason,
  features_snapshot->>'market_mode' AS market_mode_at_entry,
  round(pnl_usdt::numeric, 2) AS pnl_usdt
FROM crypto_demo_trades
WHERE status = 'closed'
  AND closed_at IS NOT NULL
  AND (closed_at AT TIME ZONE 'UTC')::date = '2026-04-23'::date
ORDER BY closed_at;
