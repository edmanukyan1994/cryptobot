-- Idempotent align for БД, созданных со старым 01_schema.sql (Railway / старые volume).
-- Выполнится при первом init контейнера Postgres после добавления файла; на живой БД без пересоздания volume — один раз вручную:
--   psql "$DATABASE_URL" -f postgres/init/03_schema_application_align.sql

CREATE TABLE IF NOT EXISTS crypto_scoring_weights (
  id text PRIMARY KEY DEFAULT 'current',
  weights jsonb NOT NULL DEFAULT '{}',
  entry_threshold integer NOT NULL DEFAULT 45,
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO crypto_scoring_weights (id, weights, entry_threshold) VALUES (
  'current',
  '{"sr_signal":0.30,"candle_confirmation":0.25,"fvg_fibonacci":0.15,"rsi":0.12,"relative_strength":0.10,"momentum_1h":0.05,"volume":0.03,"ml_signal":0.00}'::jsonb,
  45
) ON CONFLICT (id) DO NOTHING;

ALTER TABLE crypto_demo_trades ADD COLUMN IF NOT EXISTS sl_price numeric;
ALTER TABLE crypto_demo_trades ADD COLUMN IF NOT EXISTS setup_type text DEFAULT 'normal';
ALTER TABLE crypto_demo_trades ADD COLUMN IF NOT EXISTS features_snapshot jsonb DEFAULT '{}';

ALTER TABLE crypto_market_global ADD COLUMN IF NOT EXISTS features_snapshot jsonb DEFAULT '{}';

ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS btc_regime text;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS btc_structure_4h text;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS market_mode text;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS btc_momentum text;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS relative_strength numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS volume_bucket text;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS volatility_bucket text;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS impulse_score numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS reversal_score numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS distance_to_support_pct numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS distance_to_resistance_pct numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS is_aggressive_bear boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS is_aggressive_bull boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS no_long_zone boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS no_short_zone boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS btc_move_strength numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS candle_score_long numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS candle_score_short numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS in_bullish_fvg boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS in_bearish_fvg boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS nearest_fvg text;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS nearest_fvg_dist_pct numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS in_bullish_ob boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS in_bearish_ob boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS ms_structure text;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS ms_bos_bullish boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS ms_bos_bearish boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS ms_choch_bullish boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS ms_choch_bearish boolean DEFAULT false;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS fib_level numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS fib_zone text;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS fib_direction text;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS fib_dist_pct numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS fib_score_long numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS fib_score_short numeric;
ALTER TABLE crypto_features_hourly ADD COLUMN IF NOT EXISTS target_4h smallint;

ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS mark_price numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS turnover_24h numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS high_price_24h numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS low_price_24h numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS prev_price_24h numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS prev_price_1h numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS open_interest numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS open_interest_value numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS funding_rate numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS bid1_price numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS bid1_size numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS ask1_price numeric;
ALTER TABLE crypto_prices_bybit ADD COLUMN IF NOT EXISTS ask1_size numeric;
