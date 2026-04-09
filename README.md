# Описание `crypto.py`

## Что делает скрипт

`crypto.py` — это CLI-скрипт для оценки текущего рыночного режима Solana (`SOL`) на основе:

- рыночных данных по SOL и всему крипторынку;
- ликвидности через метрики стейблкоинов;
- макроэкономических ставок;
- пользовательского входа по `realized price` или `MVRV`.

На выходе скрипт строит набор входных метрик, рассчитывает промежуточные score-компоненты, затем собирает из них итоговый `final_score` и присваивает текстовый режим рынка.

Скрипт не предсказывает цену. Он агрегирует несколько эвристических сигналов и показывает, насколько текущий фон выглядит благоприятным или защитным для риска.

## Как запускать

Нужно передать ровно один из двух аргументов:

```bash
python crypto.py --realized-price 120
```

или

```bash
python crypto.py --mvrv 1.35
```

### Что означают аргументы

- `--realized-price` — пользователь напрямую задаёт realized price для SOL.
- `--mvrv` — пользователь задаёт MVRV, а скрипт сам восстанавливает realized price по формуле:

```text
realized_price = current_price / mvrv
```

Эти аргументы взаимоисключающие. Если не передать ни один или передать оба, `argparse` завершит программу с ошибкой.

## Общая схема работы

Скрипт выполняет следующие шаги:

1. Получает текущую цену SOL с CoinGecko.
2. Получает общую капитализацию крипторынка и доминации BTC/SOL с CoinGecko.
3. Получает исторический ряд по совокупной капитализации стейблкоинов с DeFiLlama.
4. Вычисляет долю стейблкоинов в крипторынке и изменение этой капитализации за последние 30 наблюдений.
5. Получает макроданные из FRED:
   - доходность 3-месячных T-bills;
   - верхнюю границу ставки ФРС.
6. Формирует объект `MarketInputs`.
7. На основе `MarketInputs` считает score-компоненты.
8. Собирает итоговый score и определяет рыночный режим.
9. Печатает лог источников данных, входные параметры, численный результат и текстовую интерпретацию.

## Откуда берутся данные

### 1. Цена SOL

Источник:

- CoinGecko `https://api.coingecko.com/api/v3/coins/markets`

Параметры запроса:

- `vs_currency=usd`
- `ids=solana`

Из ответа берутся поля:

- `name`
- `symbol`
- `current_price`
- `market_cap`
- `circulating_supply`

В расчётах дальше используется в первую очередь:

- `price_usd` как текущая цена SOL.

Функция:

- `get_sol_market_data()`

### 2. Данные по всему крипторынку

Источник:

- CoinGecko `https://api.coingecko.com/api/v3/global`

Из ответа используются:

- `data.total_market_cap.usd` — общая капитализация крипторынка в USD;
- `data.market_cap_percentage.btc` — доминация BTC;
- `data.market_cap_percentage.sol` — доминация SOL;
- `data.updated_at` — timestamp обновления.

Функция:

- `get_global_market_data()`

### 3. Данные по стейблкоинам

Источник:

- DeFiLlama `https://stablecoins.llama.fi/stablecoincharts/all`

Скрипт ожидает, что API вернёт список исторических точек. Из этого списка берутся:

- последняя запись `series[-1]`;
- запись 30 наблюдений назад `series[-31]`.

Важно: код считает именно разницу между последней точкой и точкой 30 наблюдений назад. Это называется `30d change`, но фактически это изменение за 30 элементов ряда, а не гарантированно календарно ровно за 30 дней.

Функции:

- `get_stablecoin_series()`
- `pick_latest_and_30d_ago()`
- `extract_total_stablecoin_market_cap()`

### 4. Макроэкономические данные

Источник:

- FRED CSV `https://fred.stlouisfed.org/graph/fredgraph.csv`

Используются две серии:

- `DTB3` — 3-Month Treasury Bill Secondary Market Rate;
- `DFEDTARU` — Federal Funds Target Range, Upper Limit.

Логика чтения:

- CSV загружается как текст;
- затем читается через `csv.DictReader`;
- скрипт проходит по строкам и запоминает последнее числовое значение;
- пустые значения и `"."` пропускаются.

Функции:

- `get_latest_fred_value()`
- `get_macro_data()`

## Какие структуры данных используются

### `SolMarketData`

Содержит:

- `name`
- `symbol`
- `price_usd`
- `market_cap_usd`
- `circulating_supply`

### `GlobalMarketData`

Содержит:

- `total_crypto_market_cap_usd`
- `btc_dominance_pct`
- `sol_dominance_pct`
- `updated_at`

### `StablecoinMetrics`

Содержит:

- `stablecoin_market_cap_usd`
- `stablecoin_market_cap_30d_ago_usd`
- `stablecoin_30d_change_pct`
- `stablecoin_share_pct`
- `latest_timestamp`
- `previous_timestamp`

### `MacroData`

Содержит:

- `treasury_3m_yield`
- `fed_rate`

### `MarketInputs`

Это нормализованный набор входов для расчёта итогового score:

- `price`
- `btc_dominance`
- `sol_dominance`
- `stablecoin_share`
- `stablecoin_30d_change`
- `treasury_3m_yield`
- `fed_rate`
- `realized_price`
- `mvrv`

### `MarketResult`

Финальный результат расчёта:

- `mvrv`
- `realized_price`
- `discount_to_realized_pct`
- `stable_score`
- `dominance_score`
- `mvrv_score`
- `macro_score`
- `final_score`
- `regime`

## Как вычисляются входные метрики

### 1. `realized_price`

Если передан `--realized-price`, используется он.

Если передан `--mvrv`, то:

```text
realized_price = price / mvrv
```

Функция:

- `resolve_realized_price()`

Проверки:

- `realized_price > 0`
- `mvrv > 0`

### 2. `mvrv`

После того как известны `price` и `realized_price`, MVRV считается так:

```text
mvrv = price / realized_price
```

Функция:

- `compute_mvrv()`

### 3. `discount_to_realized_pct`

Отклонение текущей цены от realized price:

```text
discount_to_realized_pct = (price / realized_price - 1) * 100
```

Если значение отрицательное, значит цена ниже realized price. Если положительное — выше.

Функция:

- `compute_discount_to_realized()`

### 4. `stablecoin_share`

Доля капитализации стейблкоинов в общей капитализации крипторынка:

```text
stablecoin_share = stablecoin_market_cap / total_crypto_market_cap_usd * 100
```

Функция:

- `get_stablecoin_metrics()`

### 5. `stablecoin_30d_change`

Изменение совокупной капитализации стейблкоинов относительно точки 30 наблюдений назад:

```text
stablecoin_30d_change =
    (stablecoin_market_cap_now - stablecoin_market_cap_30d_ago)
    / stablecoin_market_cap_30d_ago
    * 100
```

Если значение положительное, это трактуется как приток ликвидности. Если отрицательное — как ухудшение ликвидности.

## Как вычисляются score-компоненты

Все score-компоненты в итоге приводятся к шкале примерно от `0` до `100`. Для ограничения значений используется функция:

```text
clamp(value, 0, 100)
```

### 1. `mvrv_score`

Функция:

- `score_mvrv()`

Это кусочно-линейная функция.

Логика:

- если `mvrv <= 0.60`, score = `95`
- если `0.60 < mvrv <= 1.00`, score плавно снижается с `95` до `80`
- если `1.00 < mvrv <= 1.50`, score плавно снижается с `80` до `50`
- если `1.50 < mvrv <= 2.00`, score плавно снижается с `50` до `20`
- если `2.00 < mvrv <= 2.50`, score плавно снижается с `20` до `5`
- если `mvrv > 2.50`, score = `5`

Интуиция:

- низкий MVRV трактуется как более привлекательная оценка;
- высокий MVRV трактуется как перегретая оценка и меньший запас прочности.

### 2. `macro_score`

Функция:

- `score_macro()`

Сначала отдельно считаются две оценки:

```text
yield_score = 100 - ((treasury_3m_yield - 2.0) / (5.5 - 2.0)) * 100
fed_score   = 100 - ((fed_rate - 2.0) / (5.5 - 2.0)) * 100
```

То есть:

- чем ниже доходность 3M T-bills, тем лучше;
- чем ниже ставка ФРС, тем лучше.

Далее считается спред:

```text
spread = fed_rate - treasury_3m_yield
spread_bonus = clamp((spread / 0.25) * 5.0, 0.0, 8.0)
```

Финальная формула:

```text
macro_score = clamp(0.55 * clamp(yield_score) + 0.45 * clamp(fed_score) + spread_bonus)
```

Интуиция:

- более мягкие ставки дают более высокий score;
- положительный спред добавляет небольшой бонус.

### 3. `stable_score`

Функция:

- `calc_stable_score()`

Формула:

```text
base  = 50 + (stablecoin_share - 10) * 8
trend = stablecoin_30d_change * 10
stable_score = clamp(base + trend)
```

Интуиция:

- если доля стейблкоинов выше 10%, это повышает score;
- если капитализация стейблкоинов растёт, это дополнительно повышает score;
- падение капитализации стейблкоинов уменьшает score.

### 4. `dominance_score`

Функция:

- `calc_dominance_score()`

Формула:

```text
base        = 50
sol_part    = (sol_dominance - 1.5) * 20
btc_part    = -(btc_dominance - 55.0) * 3
stable_part = -(stablecoin_share - 10.0) * 2

dominance_score = clamp(base + sol_part + btc_part + stable_part)
```

Интуиция:

- рост доминации SOL повышает score;
- слишком высокая доминация BTC снижает score, потому что фон становится более защитным для альткоинов;
- более высокая доля стейблкоинов немного давит на этот конкретный score, так как здесь она трактуется как защитная ликвидность, а не риск-аппетит.

## Как собирается итоговый результат

Функция:

- `compute_market_score()`

Итоговый score:

```text
final_score =
    0.35 * mvrv_score +
    0.25 * stable_score +
    0.20 * dominance_score +
    0.20 * macro_score
```

Весовые коэффициенты:

- `35%` — valuation через `mvrv_score`
- `25%` — ликвидность через `stable_score`
- `20%` — структура рынка через `dominance_score`
- `20%` — макрофон через `macro_score`

## Как определяется рыночный режим

Функция:

- `classify()`

Пороговые значения:

- `final_score >= 80` → `Aggressive Buy / Strong Risk-On`
- `final_score >= 60` → `Accumulate / Mild Risk-On`
- `final_score >= 40` → `Neutral / Wait`
- `final_score < 40` → `Defensive / Risk-Off`

## Что выводится в консоль

Скрипт печатает четыре блока.

### 1. `=== MARKET INPUTS LOG ===`

Здесь показывается, откуда берётся каждый показатель:

- цена SOL — из CoinGecko markets;
- доминации BTC и SOL — из CoinGecko global;
- метрики стейблкоинов — из DeFiLlama;
- макроставки — из FRED;
- `realized_price` — либо напрямую от пользователя, либо из `--mvrv`.

### 2. `=== INPUTS ===`

Печатается содержимое `MarketInputs`, уже нормализованное и округлённое.

### 3. `=== RESULT ===`

Печатается содержимое `MarketResult`:

- `mvrv`
- `realized_price`
- `discount_to_realized_pct`
- `stable_score`
- `dominance_score`
- `mvrv_score`
- `macro_score`
- `final_score`
- `regime`

### 4. `=== INTERPRETATION ===`

Человекочитаемая интерпретация:

- итоговый режим;
- текущая цена против realized price;
- краткая расшифровка каждого sub-score;
- цветовая маркировка через ANSI-коды.

## Обработка ошибок и ограничения

### Сетевые ошибки

Функции `fetch_json()` и `fetch_text()` оборачивают ошибки `requests` в `RuntimeError` с сообщением о возможных причинах:

- нет интернета;
- проблемы DNS;
- VPN/proxy;
- API временно недоступен.

### Ошибки структуры данных

Скрипт явно проверяет, что ответы API содержат ожидаемые поля. Если формат ответа отличается от ожидаемого, выбрасывается `RuntimeError`.

### Недостаточно истории по стейблкоинам

Если в серии меньше 31 точки, выбрасывается ошибка:

```text
Not enough history to compute 30d change
```

### Деление на ноль и невалидные входы

- если `stablecoin_market_cap_30d_ago == 0`, выбрасывается `ZeroDivisionError`;
- если `realized_price <= 0`, выбрасывается `ValueError`;
- если `mvrv <= 0`, выбрасывается `ValueError`.

## Важные замечания по логике

### 1. Это эвристическая модель

Формулы и пороги в коде заданы вручную. Это не статистически обученная модель и не инвестиционная рекомендация.

### 2. `30d change` зависит от структуры ряда

Название метрики говорит о 30 днях, но код фактически сравнивает последнюю точку с точкой `-31`. Если API публикует данные не строго раз в день, интервал может отличаться от календарных 30 дней.

### 3. Доля стейблкоинов участвует в двух местах

`stablecoin_share`:

- повышает `stable_score`;
- одновременно слегка снижает `dominance_score`.

Это не ошибка реализации, а особенность модели: один и тот же показатель трактуется с двух разных сторон.

### 4. MVRV можно задать только косвенно или через realized price

В итоговом расчёте MVRV всегда используется как отношение текущей цены к realized price. Если пользователь передал `--mvrv`, то это значение сначала нужно, чтобы восстановить realized price, а затем MVRV будет вычислен обратно уже из финальных входов.

## Ключевые функции по порядку исполнения

Если смотреть на код как на pipeline, то основной путь такой:

1. `main()`
2. `parse_args()`
3. `build_market_inputs()`
4. `get_sol_market_data()`
5. `get_global_market_data()`
6. `get_stablecoin_metrics()`
7. `get_macro_data()`
8. `resolve_realized_price()`
9. `compute_market_score()`
10. `print_inputs_log()`
11. вывод `INPUTS`
12. вывод `RESULT`
13. `print_interpretation()`

## Краткое резюме

`crypto.py` собирает данные из CoinGecko, DeFiLlama и FRED, нормализует их в `MarketInputs`, затем оценивает четыре блока сигналов:

- valuation через MVRV;
- ликвидность через стейблкоины;
- market structure через доминации;
- макрофон через процентные ставки.

После этого скрипт рассчитывает итоговый score и относит рынок к одному из четырёх режимов: от защитного до выраженно risk-on.
