#!/usr/bin/env bash
# Выгрузка закрытых сделок за день (UTC) в CSV. Из корня проекта:
#   ./scripts/export_closed_trades_csv.sh 2026-04-23
set -euo pipefail
cd "$(dirname "$0")/.."
DAY="${1:?usage: $0 YYYY-MM-DD}"
[[ "$DAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "Дата должна быть YYYY-MM-DD"; exit 1; }
test -f .env && set -a && source .env && set +a
test -n "${DATABASE_URL:-}" || { echo "DATABASE_URL не задан (.env)"; exit 1; }
OUT="trades_${DAY}.csv"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c "
COPY (
  SELECT
    id, symbol, trade_type,
    round(amount_usdt::numeric, 2) AS amount_usdt,
    round(amount_crypto::numeric, 8) AS amount_crypto,
    round(entry_price::numeric, 8) AS entry_price,
    round(COALESCE(exit_price, 0)::numeric, 8) AS exit_price,
    round(COALESCE(sl_price, 0)::numeric, 8) AS sl_price,
    opened_at, closed_at,
    round((EXTRACT(EPOCH FROM (closed_at - opened_at)) / 3600.0)::numeric, 2) AS hold_hours,
    close_reason, setup_type,
    features_snapshot->>'entry_reason' AS entry_reason,
    features_snapshot->>'market_mode' AS market_mode_at_entry,
    round(pnl_usdt::numeric, 2) AS pnl_usdt
  FROM crypto_demo_trades
  WHERE status = 'closed'
    AND closed_at IS NOT NULL
    AND (closed_at AT TIME ZONE 'UTC')::date = '${DAY}'::date
  ORDER BY closed_at
) TO STDOUT WITH CSV HEADER
" > "$OUT"
echo "Wrote $OUT ($(wc -l < "$OUT") lines)"
