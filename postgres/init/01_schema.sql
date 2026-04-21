CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS crypto_demo_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  initial_balance numeric NOT NULL DEFAULT 10000,
  current_balance numeric NOT NULL DEFAULT 10000,
  is_active boolean NOT NULL DEFAULT true,
  last_notified_balance numeric,
  telegram_chat_id text NOT NULL DEFAULT '',
  currency text NOT NULL DEFAULT 'USDT',
  notification_language text NOT NULL DEFAULT 'ru',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_demo_trades (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id uuid NOT NULL REFERENCES crypto_demo_accounts(id) ON DELETE CASCADE,
  symbol text NOT NULL,
  trade_type text NOT NULL CHECK (trade_type IN ('long', 'short')),
  amount_usdt numeric NOT NULL,
  amount_crypto numeric NOT NULL,
  entry_price numeric NOT NULL,
  sl_price numeric,
  exit_price numeric,
  pnl_usdt numeric,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  leverage numeric DEFAULT 1,
  close_reason text,
  peak_pnl_usdt numeric DEFAULT 0,
  trough_pnl_usdt numeric DEFAULT 0,
  forecast_id uuid,
  forecast_direction text,
  forecast_probability numeric,
  features_snapshot jsonb DEFAULT '{}',
  setup_type text DEFAULT 'normal',
  mirrored_to_bybit boolean NOT NULL DEFAULT false,
  bybit_order_link_id text,
  opened_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_trades_account_status ON crypto_demo_trades(account_id, status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON crypto_demo_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_opened_at ON crypto_demo_trades(opened_at);

CREATE TABLE IF NOT EXISTS crypto_strategy_params (
  id text PRIMARY KEY DEFAULT 'current',
  version integer DEFAULT 1,
  strategy_mode text DEFAULT 'range',
  min_probability numeric DEFAULT 55,
  min_confidence numeric DEFAULT 50,
  max_risk_score numeric DEFAULT 75,
  position_size_percent numeric DEFAULT 5,
  max_total_exposure numeric DEFAULT 25,
  max_symbol_exposure numeric DEFAULT 7,
  max_positions_high_corr integer DEFAULT 3,
  base_stop_loss numeric DEFAULT 2.5,
  adaptive_sl_min numeric DEFAULT 2.0,
  base_take_profit numeric DEFAULT 6.0,
  tp1_percent numeric DEFAULT 2.0,
  tp1_close_pct numeric DEFAULT 40,
  tp2_percent numeric DEFAULT 4.0,
  tp2_close_pct numeric DEFAULT 30,
  runner_trail_pct numeric DEFAULT 0.8,
  runner_trail_mode text DEFAULT 'atr',
  runner_trail_atr_mult numeric DEFAULT 1.2,
  trail_start_percent numeric DEFAULT 1.0,
  be_stop_after_tp1 boolean DEFAULT true,
  daily_drawdown_limit numeric DEFAULT 5,
  kill_switch_active boolean DEFAULT false,
  kill_switch_until timestamptz,
  banned_symbols text[] DEFAULT '{}',
  preferred_symbols text[] DEFAULT '{}',
  protected_symbols text[] DEFAULT '{BTC,ETH,BNB,SOL,XRP}',
  preferred_symbols_only boolean DEFAULT false,
  signal_polarity jsonb DEFAULT '{}',
  symbol_multipliers jsonb DEFAULT '{}',
  deprioritize_scores jsonb DEFAULT '{}',
  symbol_cooldown_minutes numeric DEFAULT 30,
  reentry_cooldown_after_close_min numeric DEFAULT 30,
  forecast_max_age_minutes numeric DEFAULT 15,
  fee_rate_taker numeric DEFAULT 0.055,
  slippage_buffer_pct numeric DEFAULT 0.1,
  bybit_mirror_enabled boolean DEFAULT false,
  immunity_hours numeric DEFAULT 4,
  updated_at timestamptz DEFAULT now(),
  last_optimized_at timestamptz
);

CREATE TABLE IF NOT EXISTS crypto_prices_bybit (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol text NOT NULL,
  price numeric NOT NULL,
  mark_price numeric,
  volume_24h numeric,
  price_change_24h numeric,
  turnover_24h numeric,
  high_price_24h numeric,
  low_price_24h numeric,
  prev_price_24h numeric,
  prev_price_1h numeric,
  open_interest numeric,
  open_interest_value numeric,
  funding_rate numeric,
  bid1_price numeric,
  bid1_size numeric,
  ask1_price numeric,
  ask1_size numeric,
  ts timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prices_bybit_symbol_ts ON crypto_prices_bybit(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS crypto_features_hourly (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol text NOT NULL,
  ts timestamptz NOT NULL DEFAULT now(),
  price numeric,
  volume_24h numeric,
  r_1h numeric,
  r_24h numeric,
  rsi_14 numeric,
  macd numeric,
  macd_signal numeric,
  macd_histogram numeric,
  bollinger_upper numeric,
  bollinger_middle numeric,
  bollinger_lower numeric,
  bollinger_width numeric,
  atr numeric,
  fear_greed_index numeric,
  btc_dominance numeric,
  regime text DEFAULT 'neutral',
  risk_score numeric DEFAULT 50,
  candlestick_pattern text,
  candlestick_score numeric DEFAULT 0,
  support_1 numeric,
  resistance_1 numeric,
  sr_signal text,
  sr_strength numeric,
  btc_regime text,
  btc_structure_4h text,
  market_mode text,
  btc_momentum text,
  relative_strength numeric,
  volume_bucket text,
  volatility_bucket text,
  impulse_score numeric,
  reversal_score numeric,
  distance_to_support_pct numeric,
  distance_to_resistance_pct numeric,
  is_aggressive_bear boolean DEFAULT false,
  is_aggressive_bull boolean DEFAULT false,
  no_long_zone boolean DEFAULT false,
  no_short_zone boolean DEFAULT false,
  btc_move_strength numeric,
  candle_score_long numeric,
  candle_score_short numeric,
  in_bullish_fvg boolean DEFAULT false,
  in_bearish_fvg boolean DEFAULT false,
  nearest_fvg text,
  nearest_fvg_dist_pct numeric,
  in_bullish_ob boolean DEFAULT false,
  in_bearish_ob boolean DEFAULT false,
  ms_structure text,
  ms_bos_bullish boolean DEFAULT false,
  ms_bos_bearish boolean DEFAULT false,
  ms_choch_bullish boolean DEFAULT false,
  ms_choch_bearish boolean DEFAULT false,
  fib_level numeric,
  fib_zone text,
  fib_direction text,
  fib_dist_pct numeric,
  fib_score_long numeric,
  fib_score_short numeric,
  target_4h smallint
);
CREATE INDEX IF NOT EXISTS idx_features_symbol_ts ON crypto_features_hourly(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS crypto_scoring_weights (
  id text PRIMARY KEY DEFAULT 'current',
  weights jsonb NOT NULL DEFAULT '{}',
  entry_threshold integer NOT NULL DEFAULT 45,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_forecast_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol text NOT NULL,
  horizon text NOT NULL CHECK (horizon IN ('1h', '4h', '24h')),
  direction text,
  direction_probability numeric,
  confidence numeric,
  risk_score numeric,
  p10 numeric,
  p50 numeric,
  p90 numeric,
  regime text,
  features_snapshot jsonb DEFAULT '{}',
  model_version text DEFAULT 'python-v1.0',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_forecasts_symbol_horizon ON crypto_forecast_runs(symbol, horizon, created_at DESC);

CREATE TABLE IF NOT EXISTS crypto_forecast_scores (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  forecast_id uuid REFERENCES crypto_forecast_runs(id),
  symbol text NOT NULL,
  horizon text NOT NULL,
  direction_hit boolean,
  band_hit boolean,
  actual_price numeric,
  scored_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_factor_weights (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol text NOT NULL,
  horizon text NOT NULL,
  factor_name text NOT NULL,
  current_weight numeric NOT NULL DEFAULT 1.0,
  successful_predictions integer DEFAULT 0,
  failed_predictions integer DEFAULT 0,
  total_applications integer DEFAULT 0,
  wilson_lower numeric DEFAULT 0.5,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(symbol, horizon, factor_name)
);

CREATE TABLE IF NOT EXISTS crypto_fear_greed (
  id text PRIMARY KEY DEFAULT 'latest',
  value numeric NOT NULL,
  label text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_assets (
  symbol text PRIMARY KEY,
  name text,
  binance_symbol text,
  bybit_symbol text,
  rank integer,
  is_active boolean DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_futures_data (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol text NOT NULL,
  funding_rate numeric,
  open_interest numeric,
  ts timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_market_global (
  id text PRIMARY KEY DEFAULT 'latest',
  btc_dominance numeric,
  total_market_cap numeric,
  features_snapshot jsonb DEFAULT '{}',
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_trade_lessons (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trade_id uuid REFERENCES crypto_demo_trades(id),
  symbol text NOT NULL,
  trade_type text NOT NULL,
  pnl_usdt numeric,
  close_reason text,
  hold_duration_minutes numeric,
  lesson_type text DEFAULT 'pending',
  was_premature_close boolean DEFAULT false,
  analyzed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Triggers
CREATE OR REPLACE FUNCTION sync_balance_on_trade_close()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.status = 'closed' AND OLD.status = 'open' THEN
    UPDATE crypto_demo_accounts
    SET current_balance = initial_balance + COALESCE((
      SELECT SUM(pnl_usdt) FROM crypto_demo_trades
      WHERE account_id = NEW.account_id AND status = 'closed' AND pnl_usdt IS NOT NULL
    ), 0), updated_at = now()
    WHERE id = NEW.account_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_sync_balance_on_close
  AFTER UPDATE ON crypto_demo_trades
  FOR EACH ROW EXECUTE FUNCTION sync_balance_on_trade_close();

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_strategy_params_updated_at
  BEFORE UPDATE ON crypto_strategy_params
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Seed data
INSERT INTO crypto_strategy_params (id, version, strategy_mode,
  min_probability, min_confidence, max_risk_score,
  position_size_percent, max_total_exposure, max_symbol_exposure,
  base_stop_loss, tp1_percent, tp1_close_pct, tp2_percent, tp2_close_pct,
  runner_trail_mode, runner_trail_atr_mult, trail_start_percent, be_stop_after_tp1,
  daily_drawdown_limit, symbol_cooldown_minutes, forecast_max_age_minutes,
  fee_rate_taker, bybit_mirror_enabled, preferred_symbols, banned_symbols
) VALUES (
  'current', 1, 'range', 55, 50, 70, 5, 25, 7,
  2.5, 2.0, 40, 4.0, 30, 'atr', 1.2, 1.0, true,
  5, 30, 15, 0.055, false,
  ARRAY['BTC','ETH','XRP','ADA','BNB'],
  ARRAY['ETC','AVAX','DOT']
) ON CONFLICT (id) DO NOTHING;

INSERT INTO crypto_fear_greed (id, value, label) VALUES ('latest', 50, 'Neutral') ON CONFLICT (id) DO NOTHING;
INSERT INTO crypto_market_global (id, btc_dominance) VALUES ('latest', 55) ON CONFLICT (id) DO NOTHING;

INSERT INTO crypto_scoring_weights (id, weights, entry_threshold) VALUES (
  'current',
  '{"sr_signal":0.30,"candle_confirmation":0.25,"fvg_fibonacci":0.15,"rsi":0.12,"relative_strength":0.10,"momentum_1h":0.05,"volume":0.03,"ml_signal":0.00}'::jsonb,
  45
) ON CONFLICT (id) DO NOTHING;

INSERT INTO crypto_assets (symbol, name, bybit_symbol, rank, is_active) VALUES
('BTC','Bitcoin','BTCUSDT',1,true),('ETH','Ethereum','ETHUSDT',2,true),
('BNB','BNB','BNBUSDT',3,true),('SOL','Solana','SOLUSDT',4,true),
('XRP','XRP','XRPUSDT',5,true),('DOGE','Dogecoin','DOGEUSDT',6,true),
('ADA','Cardano','ADAUSDT',7,true),('AVAX','Avalanche','AVAXUSDT',8,true),
('DOT','Polkadot','DOTUSDT',9,true),('SHIB','Shiba Inu','1000SHIBUSDT',10,true),
('LINK','Chainlink','LINKUSDT',11,true),('TRX','TRON','TRXUSDT',12,true),
('LTC','Litecoin','LTCUSDT',13,true),('UNI','Uniswap','UNIUSDT',14,true),
('ATOM','Cosmos','ATOMUSDT',15,true),('ETC','Ethereum Classic','ETCUSDT',16,true),
('XLM','Stellar','XLMUSDT',17,true),('FIL','Filecoin','FILUSDT',18,true),
('NEAR','NEAR Protocol','NEARUSDT',19,true),('APT','Aptos','APTUSDT',20,true),
('ARB','Arbitrum','ARBUSDT',21,true),('OP','Optimism','OPUSDT',22,true),
('SUI','Sui','SUIUSDT',23,true),('PEPE','Pepe','1000PEPEUSDT',24,true),
('INJ','Injective','INJUSDT',25,true),('SEI','Sei','SEIUSDT',26,true),
('RUNE','THORChain','RUNEUSDT',27,true),('AAVE','Aave','AAVEUSDT',28,true),
('WIF','dogwifhat','WIFUSDT',29,true),('BONK','Bonk','1000BONKUSDT',30,true)
ON CONFLICT (symbol) DO NOTHING;
