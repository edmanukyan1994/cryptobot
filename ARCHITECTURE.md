# CryptoBot v1.1 — Архитектура и документация

> Последнее обновление: 2026-04-16
> Автоматически поддерживается в актуальном состоянии при каждом изменении системы.

---

## Содержание

1. [Обзор системы](#обзор-системы)
2. [Инфраструктура](#инфраструктура)
3. [Модули и их функции](#модули-и-их-функции)
4. [Поток данных](#поток-данных)
5. [База данных](#база-данных)
6. [Стратегия торговли](#стратегия-торговли)
7. [Скоринг и веса](#скоринг-и-веса)
8. [Свечной анализ](#свечной-анализ)
9. [ML агент](#ml-агент)
10. [Настройки и параметры](#настройки-и-параметры)
11. [Известные ограничения](#известные-ограничения)

---

## Обзор системы

CryptoBot — автоматическая торговая система для крипто рынка (Bybit). Работает в демо-режиме на виртуальном балансе. Торгует 163 символами на основе SR уровней, свечного анализа, технических индикаторов и ML прогнозов.

**Текущий режим:** DEMO  
**Начальный баланс:** $10,000,000  
**Биржа:** Bybit (linear perpetuals)  
**Горизонт прогноза:** 4 часа  

---

## Инфраструктура

| Компонент | URL | Описание |
|-----------|-----|----------|
| CryptoBot | https://cryptobot-production-bad6.up.railway.app | Основной сервис |
| ML Agent | https://ml-agent-production-591a.up.railway.app | ML сервис |
| Database | switchback.proxy.rlwy.net:36971/railway | PostgreSQL |
| GitHub (bot) | github.com/edmanukyan1994/cryptobot | Исходный код |
| GitHub (ML) | github.com/edmanukyan1994/ml-agent | ML агент |
| Railway region | Singapore | Деплой регион |

**Внутренняя сеть Railway:**
- CryptoBot → ML Agent: `http://ml-agent.railway.internal:8000`

---

## Модули и их функции

### `main.py`
Точка входа. Запускает 9 модулей асинхронно:
- collector, ws_monitor, features, forecaster, trader, market_context, tg_commands, api

### `collector.py`
Сбор цен через REST API Bybit каждые 30 секунд.

### `ws_monitor.py`
WebSocket подключение к Bybit для real-time цен. Основной источник цен. Также проверяет открытые позиции каждые 10 секунд.

### `features.py`
**Главный модуль.** Вычисляет все признаки для каждого символа каждые ~10 минут:
- Технические индикаторы (RSI, MACD, Bollinger, ATR)
- SR уровни через `sr_engine.py`
- Свечные паттерны через `detect_candle()`
- FVG, Order Blocks, Market Structure через `candle_analysis.py`
- Относительная сила vs BTC
- Сохраняет в `crypto_features_hourly`
- Обновляет `target_4h` для ML обучения через `update_targets()`

### `candle_analysis.py`
Расширенный свечной анализ:
- `detect_fvg()` — Fair Value Gap (порог 0.05%)
- `detect_order_blocks()` — Order Blocks (импульс ≥0.8%)
- `detect_market_structure()` — BOS/CHoCH, uptrend/downtrend/ranging
- `score_candle_for_direction()` — итоговый скор -50..100

### `sr_engine.py`
Поиск Support/Resistance уровней. Определяет:
- `bounce_support` — цена у поддержки
- `bounce_resistance` — цена у сопротивления
- `breakout_up/down` — пробой уровня
- `neutral` — нет чёткого сигнала

### `forecaster.py`
Прогнозирует направление движения (up/down/neutral) на основе технических индикаторов. Точность ~51-56% — используется как вторичный фильтр.

### `scoring.py`
Вычисляет итоговый скор для входа (0-100). Функции:
- `calculate_score()` — основной расчёт
- `should_enter_long()` — решение для лонга
- `should_enter_short()` — решение для шорта
- `get_entry_threshold()` — порог из БД

### `trader.py`
Основная торговая логика:
- `check_entry()` — фильтры входа
- `detect_setup_type()` — тип сетапа
- `btc_move_allows_entry()` — фильтр по BTC
- `calculate_position_size()` — размер позиции
- `close_trade()` — закрытие позиции
- `manage_position()` — управление (TP, SL, trailing)

### `ml_client.py`
Клиент для ML агента. Отправляет 20 признаков, получает direction + probability.

### `market_context.py`
Определяет глобальный контекст рынка:
- BTC режим (bull/mild_bull/mild_bear/bear)
- Market mode (bull/bull_sideways/sideways/bear_sideways/bear)
- BTC momentum (strong_up/weak_up/flat/weak_down/strong_down)

### `api.py`
FastAPI эндпоинты для дашборда:
- `/api/status` — баланс, PnL, открытые позиции
- `/api/positions` — открытые позиции
- `/api/trades` — история сделок
- `/api/candles` — свечные данные
- `/api/signals` — активные SR сигналы
- `/api/market_context` — контекст рынка
- `/api/debug_features` — отладка признаков

### `telegram_bot.py`
Уведомления в Telegram:
- Открытие/закрытие сделок
- Перезапуск бота
- Команды: /status /positions /balance /stats /closeall

---

## Поток данных

```
Bybit WebSocket/REST
        ↓
   collector.py / ws_monitor.py
        ↓
   crypto_prices_bybit (БД)
        ↓
   features.py
   ├── sr_engine.py (SR уровни)
   ├── detect_candle() (паттерны)
   └── candle_analysis.py (FVG/OB/MS)
        ↓
   crypto_features_hourly (БД)
        ↓
   forecaster.py → crypto_forecast_runs (БД)
        ↓
   trader.py
   ├── check_entry() — фильтры
   ├── scoring.py — скор
   │   └── ml_client.py → ML Agent
   └── btc_move_allows_entry()
        ↓
   crypto_demo_trades (БД)
        ↓
   Telegram уведомления
```

---

## База данных

### Основные таблицы

**`crypto_prices_bybit`**
| Колонка | Тип | Описание |
|---------|-----|----------|
| symbol | text | Символ (BTC, ETH, ...) |
| price | numeric | Цена |
| volume_24h | numeric | Объём 24h |
| ts | timestamptz | Время записи |

**`crypto_features_hourly`**
Основная таблица признаков. Обновляется каждые ~10 минут.

| Колонка | Описание |
|---------|----------|
| symbol, ts, price | Идентификатор |
| rsi_14 | RSI 14 периодов |
| macd, macd_signal, macd_histogram | MACD |
| bollinger_upper/middle/lower/width | Bollinger Bands |
| atr | Average True Range |
| r_1h, r_24h | Изменение цены за 1h и 24h |
| sr_signal | SR сигнал (bounce_support/resistance/breakout) |
| sr_strength | Сила SR уровня |
| support_1, resistance_1 | Ближайшие уровни |
| market_mode | Режим рынка |
| btc_momentum | Моментум BTC |
| candlestick_pattern | Свечной паттерн |
| candlestick_score | Скор свечного паттерна |
| candle_score_long | Свечной скор для лонга (-50..100) |
| candle_score_short | Свечной скор для шорта (-50..100) |
| in_bullish_fvg | Цена в бычьем FVG |
| in_bearish_fvg | Цена в медвежьем FVG |
| nearest_fvg | Ближайший FVG (bullish/bearish) |
| nearest_fvg_dist_pct | Расстояние до ближайшего FVG |
| in_bullish_ob | Цена в бычьем Order Block |
| in_bearish_ob | Цена в медвежьем Order Block |
| ms_structure | Структура рынка (uptrend/downtrend/ranging) |
| ms_bos_bullish/bearish | Break of Structure |
| ms_choch_bullish/bearish | Change of Character |
| impulse_score | Скор импульса |
| reversal_score | Скор разворота |
| relative_strength | Сила монеты vs BTC |
| volume_bucket | Категория объёма (trash/low/medium/high/ultra) |
| volatility_bucket | Категория волатильности |
| target_4h | Метка для ML (1=рост>0.5%, 0=падение<-0.5%) |

**`crypto_demo_trades`**
История сделок.

| Колонка | Описание |
|---------|----------|
| symbol | Символ |
| trade_type | long/short |
| amount_usdt | Размер в USDT |
| entry_price, exit_price | Цены входа/выхода |
| pnl_usdt | PnL |
| status | open/closed |
| close_reason | Причина закрытия |
| features_snapshot | Снимок признаков на момент входа |
| setup_type | Тип сетапа |

**`crypto_demo_accounts`**
Баланс аккаунта.

**`crypto_scoring_weights`** (id='current')
Веса факторов скоринга и порог входа.

**`crypto_forecast_runs`**
Прогнозы forecaster'а.

---

## Стратегия торговли

### Правила входа по market_mode

| Market Mode | Лонги | Шорты |
|-------------|-------|-------|
| `bull` | Только при r_1h ≥ 0.3% (импульс) | Запрещены |
| `bull_sideways` | Только от `bounce_support` | **Запрещены** |
| `sideways` | От `bounce_support` | От `bounce_resistance` |
| `bear_sideways` | От `bounce_support` | Только от `bounce_resistance` |
| `bear` | Запрещены | Только при r_1h ≤ -0.3% (импульс) |

### Типы сетапов

| Setup Type | Условия | Размер позиции |
|------------|---------|----------------|
| `long_support` | dist_to_support ≤ 2%, rsi ≤ 55, volume ≥ 700K | trend_pct |
| `long_reversal` | reversal_score ≥ 2, rsi ≤ 40 или SR | trend_pct |
| `long_impulse` | impulse_score ≥ 3, r_1h ≥ 0.02%, rs ≥ 1.0 | impulse_pct |
| `long_trend` | bull/bull_sideways режим | trend_pct |
| `short_trend` | bear/bear_sideways режим | trend_pct |
| `short_impulse` | impulse_score ≥ 3, r_1h ≤ -0.02% | impulse_pct |

### Фильтр BTC (`btc_move_allows_entry`)

| Setup | BTC momentum | Результат |
|-------|-------------|-----------|
| long_trend/long_support | strong_down | Блокировка |
| long_trend/long_support | weak_down | Блокировка |
| short_trend | strong_down | Разрешено (буст) |
| short_trend | strong_up/weak_up | Блокировка |

### Управление позицией

- **TP1:** +2% от входа (закрывается 50%)
- **Trailing Stop:** активируется после TP1
- **Stop Loss:** 1.5-2% (зависит от setup_type)
- **Opposite Forecast Exit:** закрытие при смене прогноза с prob ≥ 80%
- **BE Stop:** безубыток после TP1

### Направление входа

Направление определяется из SR сигнала (приоритет над forecaster):
```
bounce_resistance / breakout_down → SHORT
bounce_support / breakout_up → LONG
neutral → fallback на forecaster
```

---

## Скоринг и веса

### Текущие веса (в БД, id='current')

| Фактор | Вес | Описание |
|--------|-----|----------|
| `sr_signal` | **0.35** | SR сигнал — главный фактор |
| `candle_confirmation` | **0.30** | Свечное подтверждение |
| `rsi` | 0.10 | Перекупленность/перепроданность |
| `volume` | 0.10 | Объём торгов |
| `relative_strength` | 0.10 | Сила монеты vs BTC |
| `momentum_1h` | 0.05 | Импульс за 1 час |
| `momentum_24h` | 0.00 | Отключён |
| `ml_signal` | 0.00 | ML (временно отключён) |
| `market_mode` | 0.00 | Мультипликатор, не фактор |
| `distance` | 0.00 | Заменён на candle_confirmation |

**Порог входа:** 40 очков из 100

### Мультипликатор market_mode

| Market Mode | Совпадение с направлением | Мультипликатор |
|-------------|--------------------------|----------------|
| bull/bear | Совпадает | ×1.2 |
| bull_sideways/bear_sideways | Совпадает | ×1.1 |
| sideways | Любое | ×1.0 |
| Любой | Не совпадает | ×0.9 |

### Логика `candle_confirmation`

Использует `candle_score_long` или `candle_score_short` из features которые вычисляются через `score_candle_for_direction()`:

**Для лонга:**
- `rejection_low/hammer + bounce_support` → +60
- `rejection_low/hammer` без SR → +35
- `bullish_engulfing/marubozu` без SR → +30
- `bullish_marubozu + bounce_resistance` (противоречие) → -20
- `shooting_star/rejection_high` → -30

**Для шорта (симметрично):**
- `rejection_high/shooting_star + bounce_resistance` → +60
- `rejection_high/shooting_star` без SR → +35
- `bearish_engulfing/marubozu` → +30
- Противоречия дают отрицательные очки

**FVG бонус:**
- `in_bullish_fvg=True` для лонга → +25
- Близко к FVG (dist < 1%) → +12

**Order Block бонус:**
- `in_bullish_ob=True` для лонга → +20
- Близко к OB (dist < 1%) → +10

**Market Structure бонус:**
- BOS в нужном направлении → +15
- CHoCH в нужном направлении → +8
- Структура совпадает с направлением → +10
- Структура против направления → -10

---

## Свечной анализ

### `detect_candle()` — паттерны

| Паттерн | Условие | Направление |
|---------|---------|-------------|
| `rejection_high` | Тень > 60% свечи сверху, тело < 30% | Медвежий |
| `rejection_low` | Тень > 60% свечи снизу, тело < 30% | Бычий |
| `shooting_star` | Верхняя тень > 2× тело | Медвежий |
| `hammer` | Нижняя тень > 2× тело | Бычий |
| `hanging_man` | Нижняя тень > 2× тело (у вершины) | Медвежий |
| `inverted_hammer` | Верхняя тень > 2× тело (у основания) | Бычий |
| `bullish_marubozu` | Бычья свеча без теней | Бычий |
| `bearish_marubozu` | Медвежья свеча без теней | Медвежий |
| `doji` | Тело < 5% диапазона | Нейтральный |

### `detect_fvg()` — Fair Value Gap

Ищет в последних 30 свечах:
- **Bullish FVG:** `low[i] > high[i-2]` — gap вверх (порог ≥ 0.05%)
- **Bearish FVG:** `high[i] < low[i-2]` — gap вниз (порог ≥ 0.05%)

Возвращает: top, bottom, mid, size_pct, dist_pct, in_fvg

### `detect_order_blocks()` — Order Blocks

Ищет в последних 50 свечах:
- **Bullish OB:** медвежья свеча + импульс вверх ≥ 0.8% после неё
- **Bearish OB:** бычья свеча + импульс вниз ≥ 0.8% после неё

### `detect_market_structure()` — Market Structure

Определяет swing highs/lows (lookback=5):
- **uptrend:** Higher High + Higher Low
- **downtrend:** Lower High + Lower Low
- **ranging:** всё остальное

**BOS (Break of Structure):** цена пробивает последний swing high/low
**CHoCH (Change of Character):** первый признак разворота тренда

---

## ML агент

### Архитектура

- **Модель:** XGBoost классификатор
- **Обучение:** автоматически при старте и каждые 24 часа
- **Данные:** до 200,000 записей из `crypto_features_hourly`

### Признаки (20 штук)

```python
FEATURE_NAMES = [
    # Технические индикаторы
    'rsi_14', 'macd', 'macd_signal', 'macd_histogram',
    'bollinger_width', 'atr', 'r_1h', 'r_24h',
    # Объём
    'volume_24h', 'impulse_score', 'reversal_score',
    # Сила
    'relative_strength',
    # SR
    'distance_to_support_pct', 'distance_to_resistance_pct',
    # Свечной анализ (добавлены 2026-04-16)
    'candle_score_long', 'candle_score_short',
    # FVG
    'in_bullish_fvg', 'in_bearish_fvg',
    # Order Blocks
    'in_bullish_ob', 'in_bearish_ob',
]
```

### Target метка (`target_4h`)

```
target_4h = 1 если цена через 4h выросла > 0.5%
target_4h = 0 если цена через 4h упала < -0.5%
target_4h = NULL если движение < 0.5% (нейтрально)
```

Обновляется автоматически через `update_targets()` в `features.py` после каждого цикла.

### API эндпоинты

- `POST /predict` — предсказание для признаков
- `POST /train` — ручной запуск переобучения
- `GET /health` — статус модели

### Текущий статус

ML сигнал временно отключён (`ml_signal` вес = 0.00) пока накапливаются данные с правильными метками. Планируется включить через 3-5 дней с весом 0.10.

---

## Настройки и параметры

### Параметры в БД (`crypto_bot_params`)

| Параметр | Значение | Описание |
|----------|----------|----------|
| `entry_threshold` | 40 | Минимальный скор для входа |
| `fee_rate_taker` | 0.055% | Комиссия биржи |
| `slippage_percent` | 0.15% | Проскальзывание |
| `min_prob_floor` | 55% | Мин. вероятность (только без SR) |
| `be_stop_after_tp1` | true | Безубыток после TP1 |

### Параметры в `crypto_scoring_weights`

Хранит веса факторов и `entry_threshold`. Обновляются через SQL без перезапуска бота.

---

## Известные ограничения

1. **Forecaster точность ~51-56%** — почти случайный. Используется только как вторичный фильтр, SR сигнал важнее.

2. **FVG на 1H таймфрейме** — в боковом рынке gaps маленькие (0.05-0.09%). Порог снижен до 0.05% для их обнаружения.

3. **ML агент** — обучен на данных с историей. Требует 2-3 недели накопления данных с новыми признаками для стабильной работы.

4. **`get_allowed_direction()`** — всегда возвращает "both". Fear & Greed не влияет на разрешённое направление (намеренно — F&G часто запаздывает).

5. **`set_cooldown()`** — пустая функция. Кулдаун после убыточных сделок не реализован.

6. **`target_4h`** — пересчитан 2026-04-16 на 238K строках. Предыдущие данные содержали неправильные метки.

---

## История изменений

### 2026-04-16
- Запрещены шорты в `bull_sideways` режиме
- Исправлен `candle_score` — теперь реально сохраняется в БД
- Исправлена конвертация `asyncpg Record` в dict в `scoring.py`
- Исправлена логика `score_candle_for_direction` — SR противоречие даёт отрицательный скор
- SR сигнал теперь определяет направление входа вместо forecaster
- ML агент подключён через Railway internal URL
- Пересчитан `target_4h` на 238K строках по реальным ценам
- ML агент переобучен с 20 признаками (было 14)
- Добавлен `update_targets()` в цикл features builder
- Добавлена обработка `long_support` setup type
- Убран дублирующийся endpoint `/api/market_context`
- Обновлены веса: sr_signal=0.35, candle=0.30, порог=40
- Баланс сброшен до $10M для чистого старта

### 2026-04-18
- Запрещены шорты в `bull` режиме (ранее только в `bull_sideways`)
- В `bull` режиме лонги блокируются при r1h > 0.05% — анализ показал что входы на разогнанных монетах дают WR 0% и avg −$5,506
- В `bull_sideways` минимальный стоп расширен до 3% — анализ показал что 21 из 24 стопов выжили бы при стопе 3%
- Исправлен `features_snapshot` — теперь содержит все поля включая candle_score, FVG, MS

### 2026-04-18
- Запрещены шорты в `bull` режиме (ранее только в `bull_sideways`)
- В `bull` лонги блокируются при r1h > 0.05% — анализ 24 сделок показал WR 0% и avg −$5,506 при входах на разогнанных монетах
- В `bull_sideways` минимальный стоп расширен до 3% — 21 из 24 стопов выжили бы при стопе 3%
- Исправлен `features_snapshot` — теперь содержит все поля включая candle_score, FVG, MS, OB
- Свечной анализ теперь на закрытых свечах (`klines[:-1]`) — устраняет нестабильность candle_score из-за незакрытой текущей свечи

- В `bull_sideways` запрещены лонги при `bearish_marubozu` и `shooting_star` — откатили (недостаточно данных, разница 19-30% не значима)
- Исправлен критический баг: `market_mode is not defined` в `open_trade` — функция не получала `market_mode` как параметр
- Исправлен порядок определения переменных в `check_entry` — `sr_signal` и `r_1h` теперь определяются до скоринга

### 2026-04-19
- Добавлен `detect_fibonacci()` в `candle_analysis.py` — определяет swing high/low за 50 свечей, считает уровни отката (0.236/0.382/0.5/0.618/0.786), зона ±1.5%
- Скоринг переработан симметрично для лонгов и шортов
- Новый фактор `fvg_fibonacci` (вес 0.15) объединяет FVG и Фибоначчи
- Убран фактор `distance` — заменён на `candle_confirmation` + `fvg_fibonacci`
- Новые веса: sr_signal=0.30, candle=0.25, fvg_fib=0.15, rsi=0.12, rs=0.10, mom1h=0.05, vol=0.03
- Добавлены колонки в БД: fib_level, fib_zone, fib_direction, fib_dist_pct, fib_score_long, fib_score_short
- Исправлен критический баг: минимальный стоп 3% в bull_sideways не применялся когда SR переопределял стоп

### 2026-04-19 (продолжение)
- Добавлена функция `detect_direction()` — направление определяется из совокупности сигналов (свечи, RSI, Фибоначчи, FVG, MS, RS, forecaster) вместо только SR
- Минимальный перевес для входа: 12 очков (bull_score - bear_score)
- Добавлен жёсткий фильтр: `candle_score < -15` блокирует вход даже при положительном edge
- Усилен штраф: `bearish_marubozu + bounce_support` при лонге → -25 очков
- SR отскоки (`bounce_support/resistance`) больше не дают очков в `detect_direction` — слишком нестабильны между обновлениями
