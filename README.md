# `crypto.py`

## Что делает скрипт

`crypto.py` — это CLI-модель для оценки рыночного режима `SOL` и расчёта целевой аллокации портфеля.

Скрипт собирает:

- valuation через `MVRV` и `realized price`
- ликвидность через капитализацию стейблкоинов
- структуру рынка через доминации и proxy для `TOTAL3`
- макрофон через ставки, доллар и баланс ФРС
- тактический фон через `funding rate` и `open interest`

На выходе модель:

- строит `MarketInputs`
- считает набор sub-score
- собирает итоговый `final_score`
- применяет защитные funding-guardrails
- выдает режим рынка и целевую аллокацию:
  - `risk`
  - `stablecoins`
  - `majors`
  - `alts`

## Запуск

Нужно передать ровно один аргумент:

```bash
python crypto.py --realized-price 120
```

или

```bash
python crypto.py --mvrv 0.6575
```

Аргументы взаимоисключающие:

- `--realized-price` — realized price задаётся напрямую
- `--mvrv` — realized price восстанавливается по формуле:

```text
realized_price = price / mvrv
```

## Источники данных

### CoinGecko

Используется для:

- текущей цены `SOL`
- market cap `BTC`, `ETH`, `SOL`
- общей капитализации крипторынка
- доминации `BTC` и `ETH`

Эндпоинты:

- `https://api.coingecko.com/api/v3/global`
- `https://api.coingecko.com/api/v3/coins/markets`
- `https://api.coingecko.com/api/v3/coins/{id}/history`

### DeFiLlama

Используется для:

- совокупной капитализации стейблкоинов
- изменения капитализации стейблкоинов за 30 наблюдений

Эндпоинт:

- `https://stablecoins.llama.fi/stablecoincharts/all`

### FRED

Используется для:

- `DTB3` — 3M Treasury Bill Yield
- `DFEDTARU` — верхняя граница ставки ФРС
- `WALCL` — суммарный баланс ФРС

Эндпоинт:

- `https://fred.stlouisfed.org/graph/fredgraph.csv`

### Binance Futures

Используется для:

- `funding_rate`
- `open_interest`
- `open_interest_change_7d`

Эндпоинты:

- `https://fapi.binance.com/fapi/v1/fundingRate`
- `https://fapi.binance.com/fapi/v1/openInterest`
- `https://fapi.binance.com/futures/data/openInterestHist`

### DXY

Основной источник для `DXY`:

- Stooq `https://stooq.com/q/l/` для `dx.f`

Fallback-источник:

- FRED `DTWEXBGS`, если рыночный `DXY` временно недоступен

## Какие метрики строятся

### Valuation

- `price`
- `realized_price`
- `mvrv`
- `discount_to_realized_pct`

Формулы:

```text
mvrv = price / realized_price
discount_to_realized_pct = (price / realized_price - 1) * 100
```

### Stablecoin liquidity

- `stablecoin_market_cap_usd`
- `stablecoin_share`
- `stablecoin_30d_change`

Формулы:

```text
stablecoin_share = stablecoin_market_cap_usd / total_market_cap_usd * 100
stablecoin_30d_change = (latest - prev_30_obs) / prev_30_obs * 100
```

### Market structure

- `total_market_cap_usd`
- `total3_market_cap_usd`
- `total3_30d_change`
- `btc_dominance`
- `eth_dominance`
- `sol_dominance`
- `btc_dominance_30d_change`
- `sol_dominance_30d_change`

#### Что такое `TOTAL3`

В коде используется приближение:

```text
TOTAL3 = total_market_cap_usd - btc_market_cap_usd - eth_market_cap_usd
```

Исторический `TOTAL3` напрямую не берётся, потому что бесплатный `CoinGecko /global` не отдаёт историю.
Поэтому `total3_30d_change` оценивается через текущий `TOTAL3` и историческое изменение basket:

```text
BTC + ETH + SOL
```

Это proxy, а не каноническая метрика.

### Macro

- `treasury_3m_yield`
- `fed_rate`
- `dxy`
- `fed_balance_usd`
- `fed_balance_30d_change_pct`

#### Как считается `fed_balance_30d_change_pct`

Берётся текущее значение `WALCL` и ближайшее доступное значение не позже чем 30 дней назад:

```text
fed_balance_30d_change_pct = (fed_balance_now - fed_balance_30d_ago) / fed_balance_30d_ago * 100
```

### Derivatives / tactical

- `funding_rate`
- `open_interest`
- `open_interest_change_7d`

Важно:

- `funding_rate` хранится в raw-формате Binance
- например `0.0010` = `0.10%`
- в человекочитаемом выводе скрипт переводит его в проценты

## Как считаются sub-score

Все score ограничиваются функцией:

```text
clamp(value, 0, 100)
```

### 1. `mvrv_score`

Функция:

- `score_mvrv()`

Кусочно-линейная модель:

- `mvrv <= 0.60` → `95`
- `0.60..1.00` → снижение `95 -> 80`
- `1.00..1.50` → снижение `80 -> 50`
- `1.50..2.00` → снижение `50 -> 20`
- `2.00..2.50` → снижение `20 -> 5`
- `> 2.50` → `5`

Интуиция:

- низкий `MVRV` = valuation support
- высокий `MVRV` = перегретая оценка

### 2. `stable_score`

Функция:

- `score_stable()`

Формула:

```text
base  = 50 + (stablecoin_share - 10) * 8
trend = stablecoin_30d_change * 10
stable_score = clamp(base + trend)
```

### 3. `dominance_score`

Функция:

- `score_dominance_level()`

Формула:

```text
base        = 50
sol_part    = (sol_dominance - 1.5) * 20
btc_part    = -(btc_dominance - 55.0) * 3
stable_part = -(stablecoin_share - 10.0) * 2
dominance_score = clamp(base + sol_part + btc_part + stable_part)
```

### 4. `dominance_trend_score`

Функция:

- `score_dominance_trend()`

Формула:

```text
score = 50
score += (-btc_dominance_30d_change) * 6
score += sol_dominance_30d_change * 10
```

Интуиция:

- падающий `BTC.D` = лучше для risk-on
- растущий `SOL.D` = лучше для `SOL`

### 5. `total3_score`

Функция:

- `score_total3()`

Формула:

```text
total3_score = clamp(50 + total3_30d_change * 2)
```

### 6. `momentum_score`

Функция:

- `score_momentum()`

Логика:

- отрицательный funding поддерживает score
- funding выше `0.03%` штрафует
- funding выше `0.10%` штрафует сильнее
- изменение `open interest` усиливает или ослабляет score

Формально:

```text
if funding_rate < 0:
    score += 10
elif funding_rate > 0.0010:
    score -= 20
elif funding_rate > 0.0003:
    score -= 8
else:
    score += 5

score += open_interest_change_7d * 1.2
```

### 7. `macro_score`

Базовая функция:

- `score_macro()`

Сначала оцениваются ставки:

```text
yield_score = 100 - ((treasury_3m_yield - 2.0) / (5.5 - 2.0)) * 100
fed_score   = 100 - ((fed_rate - 2.0) / (5.5 - 2.0)) * 100
spread_bonus = clamp((fed_rate - treasury_3m_yield) / 0.25 * 5, 0, 8)

macro_base = clamp(0.55 * clamp(yield_score) + 0.45 * clamp(fed_score) + spread_bonus)
```

Потом применяются overlay-фильтры:

- `DXY` выше `103` снижает `macro_score`
- `WALCL` меньше чем `-1%` за 30 дней трактуется как QT и режет `macro_score`

Функция:

- `apply_macro_liquidity_overlays()`

Логика:

```text
if dxy > 103:
    macro_score *= 1 - min(0.40, (dxy - 103) * 0.02)

if fed_balance_30d_change_pct < -1.0:
    macro_score *= 0.85
```

## Как собирается итоговый score

Функция:

- `compute_market_score()`

Формула:

```text
final_score =
    0.25 * mvrv_score +
    0.20 * stable_score +
    0.15 * macro_score +
    0.15 * dominance_score +
    0.10 * dominance_trend_score +
    0.10 * total3_score +
    0.05 * momentum_score
```

## Funding guardrails

После расчёта `final_score` применяются дополнительные защитные правила.

### 1. Haircut по score

Функция:

- `apply_funding_guardrails()`

Логика:

```text
if funding_rate > 0.0010:
    final_score *= 0.90

if funding_rate > 0.0015 and final_score > 65:
    final_score *= 0.90
    regime = "Cautious Accumulation (High Funding)"
```

### 2. Снижение risk allocation

Функция:

- `apply_risk_allocation_guardrails()`

Логика:

```text
if funding_rate > 0.0015:
    target_risk *= 0.60
elif funding_rate > 0.0010:
    target_risk *= 0.70
```

Это ключевое правило, которое не даёт системе держать слишком большой риск при перегретом perpetual market.

## Аллокации

Базовая аллокация определяется через `allocation_from_score()`.

### Общий риск

- `score >= 80` → `90% risk / 10% stablecoins`
- `score >= 65` → `70% risk / 30% stablecoins`
- `score >= 50` → `50% risk / 50% stablecoins`
- `score >= 35` → `30% risk / 70% stablecoins`
- `< 35` → `15% risk / 85% stablecoins`

### Внутри risk bucket

- `score > 75` → `40% majors / 60% alts`
- `score > 60` → `60% majors / 40% alts`
- иначе → `80% majors / 20% alts`

После этого правила funding могут дополнительно снизить общий риск.

## Режимы рынка

Функция:

- `classify()`

Пороги:

- `>= 80` → `Strong Risk-On`
- `>= 65` → `Accumulate / Mild Risk-On`
- `>= 50` → `Neutral`
- `>= 35` → `Defensive`
- `< 35` → `Risk-Off`

## Что выводится в консоль

Скрипт печатает:

### `=== MARKET INPUTS LOG ===`

Показывает, откуда берётся каждый показатель.

### `=== INPUTS ===`

Печатает полный `MarketInputs`.

### `=== RESULT ===`

Печатает полный `MarketResult`.

### `=== INTERPRETATION ===`

Печатает:

- итоговый режим
- discount к realized price
- целевую аллокацию
- интерпретацию по каждому score-компоненту
- правила ребаланса

## Ограничения модели

- это эвристическая модель, а не статистически обученный алгоритм
- `TOTAL3` считается через proxy
- `DXY` в первую очередь берётся как рыночное значение индекса доллара; `DTWEXBGS` используется только как fallback
- `funding_rate` зависит от Binance Futures по `SOLUSDT`, а не от всего рынка
- `stablecoin_30d_change` считается по 30 наблюдениям ряда, а не гарантированно по календарным 30 дням

## Проверка корректности

Для быстрой проверки синтаксиса:

```bash
python -m py_compile crypto.py
```
