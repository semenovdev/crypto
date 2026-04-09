"""CLI utility for building a Solana market regime snapshot.

How this file is expected to work:
1. Fetch live market data for SOL, BTC, ETH, and the overall crypto market.
2. Fetch stablecoin market-cap history and compute liquidity metrics.
3. Fetch macro rates from FRED.
4. Fetch derivatives data for SOL perpetuals from Binance Futures.
5. Accept exactly one valuation input from the command line:
   - `--realized-price` to pass realized price directly; or
   - `--mvrv` to derive realized price from the current SOL price.
6. Build a V2 market snapshot with valuation, liquidity, macro, dominance,
   TOTAL3 proxy, and momentum data.
7. Compute a weighted market score, apply funding guardrails, and map the
   result to a target portfolio allocation.
8. Print input provenance, normalized inputs, final result fields, and a
   human-readable interpretation with ANSI colors.

Expected behavior and assumptions:
- The script is intended to run as a command-line tool.
- Internet access is required because all raw inputs are loaded from APIs.
- Stablecoin history must contain at least 31 observations.
- Historical TOTAL3 is approximated from current total market cap and a
  BTC+ETH+SOL market-cap basket because the free CoinGecko global endpoint
  does not expose historical total market cap.
- `realized_price` and `mvrv` must be positive numbers.

Example usage:
    python crypto.py --realized-price 120
    python crypto.py --mvrv 1.35
"""

import argparse
import csv
import io
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from requests import exceptions as requests_exceptions


COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
DEFI_LLAMA_STABLES_URL = "https://stablecoins.llama.fi/stablecoincharts/all"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
BINANCE_OPEN_INTEREST_URL = "https://fapi.binance.com/fapi/v1/openInterest"
BINANCE_OPEN_INTEREST_HIST_URL = "https://fapi.binance.com/futures/data/openInterestHist"


@dataclass
class MarketInputs:
    price: float
    realized_price: float
    mvrv: float
    fed_rate: float
    treasury_3m_yield: float
    stablecoin_market_cap_usd: float
    stablecoin_share: float
    stablecoin_30d_change: float
    total_market_cap_usd: float
    total3_market_cap_usd: float
    total3_30d_change: float
    btc_dominance: float
    eth_dominance: float
    sol_dominance: float
    btc_dominance_30d_change: float
    sol_dominance_30d_change: float
    funding_rate: float
    open_interest: float
    open_interest_change_7d: float


@dataclass
class MarketResult:
    discount_to_realized_pct: float
    mvrv_score: float
    stable_score: float
    macro_score: float
    dominance_score: float
    dominance_trend_score: float
    total3_score: float
    momentum_score: float
    final_score: float
    regime: str
    allocation_total_risk_pct: int
    allocation_stablecoins_pct: int
    allocation_majors_pct: int
    allocation_alts_pct: int


class AnsiColor:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"


_HTTP_CACHE: dict[tuple[str, tuple[tuple[str, str], ...]], Any] = {}


def _cache_key(url: str, params: dict[str, Any] | None = None) -> tuple[str, tuple[tuple[str, str], ...]]:
    normalized = tuple(sorted((params or {}).items()))
    return url, tuple((str(k), str(v)) for k, v in normalized)


def _request_with_retry(url: str, params: dict[str, Any] | None = None) -> requests.Response:
    last_error = None
    for attempt in range(4):
        response = requests.get(url, params=params, timeout=30)

        if response.status_code != 429:
            try:
                response.raise_for_status()
                return response
            except requests_exceptions.RequestException as exc:
                last_error = exc
                break

        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                delay = max(float(retry_after), 1.0)
            except ValueError:
                delay = float(2**attempt)
        else:
            delay = float(2**attempt)

        last_error = requests_exceptions.HTTPError(
            f"429 Too Many Requests for url: {response.url}",
            response=response,
        )

        if attempt == 3:
            break

        time.sleep(delay)

    raise RuntimeError(
        "Request failed after retries. The upstream API may be rate-limiting this script. "
        f"Original error: {last_error}"
    ) from last_error


def fetch_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    key = _cache_key(url, params)
    if key in _HTTP_CACHE:
        return _HTTP_CACHE[key]

    data = _request_with_retry(url, params).json()
    _HTTP_CACHE[key] = data
    return data


def fetch_text(url: str, *, params: dict[str, Any] | None = None) -> str:
    key = _cache_key(url, params)
    if key in _HTTP_CACHE:
        cached = _HTTP_CACHE[key]
        if isinstance(cached, str):
            return cached

    text = _request_with_retry(url, params).text
    _HTTP_CACHE[key] = text
    return text


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def pct_change(current: float, previous: float) -> float:
    if previous == 0:
        raise ZeroDivisionError("previous value is zero")
    return (current - previous) / previous * 100.0


def format_ts(ts: int | float | None) -> str:
    if ts is None:
        return "n/a"
    try:
        value = float(ts)
        if value > 10_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except Exception:
        return str(ts)


def get_latest_fred_value(series_id: str) -> float:
    csv_text = fetch_text(FRED_CSV_URL, params={"id": series_id})
    rows = csv.DictReader(io.StringIO(csv_text))

    latest_value: float | None = None
    for row in rows:
        value = row.get(series_id)
        if value and value != ".":
            latest_value = float(value)

    if latest_value is None:
        raise RuntimeError(f"No valid FRED value found for {series_id}")

    return latest_value


def get_global_market_data() -> dict[str, float]:
    data = fetch_json(COINGECKO_GLOBAL_URL)["data"]
    return {
        "total_market_cap_usd": float(data["total_market_cap"]["usd"]),
        "btc_dominance": float(data["market_cap_percentage"]["btc"]),
        "eth_dominance": float(data["market_cap_percentage"]["eth"]),
        "updated_at": format_ts(data.get("updated_at")),
    }


def get_coin_markets(ids: list[str]) -> dict[str, dict[str, Any]]:
    data = fetch_json(
        COINGECKO_MARKETS_URL,
        params={"vs_currency": "usd", "ids": ",".join(ids)},
    )
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected CoinGecko markets response: {data}")
    return {row["id"]: row for row in data}


def get_stablecoin_history() -> list[dict[str, Any]]:
    data = fetch_json(DEFI_LLAMA_STABLES_URL)
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Unexpected DeFiLlama response: {data}")
    return data


def extract_stable_total(entry: dict[str, Any]) -> float:
    if "totalCirculatingUSD" in entry:
        value = entry["totalCirculatingUSD"]
        if isinstance(value, (int, float, str)):
            return float(value)
        if isinstance(value, dict):
            for key in ("peggedUSD", "usd", "value", "total"):
                if key in value and isinstance(value[key], (int, float, str)):
                    return float(value[key])

    for value in entry.values():
        if isinstance(value, (int, float, str)):
            try:
                return float(value)
            except ValueError:
                pass
        if isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, (int, float, str)):
                    try:
                        return float(nested)
                    except ValueError:
                        pass

    raise RuntimeError(f"Cannot parse stablecoin total from entry: {entry}")


def get_stablecoin_metrics() -> dict[str, float]:
    series = get_stablecoin_history()
    if len(series) < 31:
        raise RuntimeError("Not enough DeFiLlama history to compute 30d change")

    latest = extract_stable_total(series[-1])
    prev_30d = extract_stable_total(series[-31])

    return {
        "stablecoin_market_cap_usd": latest,
        "stablecoin_30d_change": pct_change(latest, prev_30d),
    }


def get_latest_funding_rate(symbol: str = "SOLUSDT") -> float:
    data = fetch_json(BINANCE_FUNDING_URL, params={"symbol": symbol, "limit": 5})
    if not data:
        raise RuntimeError("Empty funding history from Binance")
    return float(data[-1]["fundingRate"]) * 100.0


def get_open_interest(symbol: str = "SOLUSDT") -> float:
    data = fetch_json(BINANCE_OPEN_INTEREST_URL, params={"symbol": symbol})
    return float(data["openInterest"])


def get_open_interest_change_7d(symbol: str = "SOLUSDT") -> float:
    data = fetch_json(
        BINANCE_OPEN_INTEREST_HIST_URL,
        params={"symbol": symbol, "period": "1d", "limit": 8},
    )
    if len(data) < 8:
        raise RuntimeError("Not enough open interest history from Binance")

    latest = float(data[-1]["sumOpenInterest"])
    prev_7d = float(data[0]["sumOpenInterest"])
    return pct_change(latest, prev_7d)


def get_current_market_snapshot() -> dict[str, float | str]:
    global_data = get_global_market_data()
    coins = get_coin_markets(["solana", "bitcoin", "ethereum"])

    total_market_cap = float(global_data["total_market_cap_usd"])
    btc_mc = float(coins["bitcoin"]["market_cap"])
    eth_mc = float(coins["ethereum"]["market_cap"])
    sol_mc = float(coins["solana"]["market_cap"])
    price = float(coins["solana"]["current_price"])

    total3_mc = total_market_cap - btc_mc - eth_mc
    sol_dom = sol_mc / total_market_cap * 100.0

    return {
        **global_data,
        "price": price,
        "btc_market_cap_usd": btc_mc,
        "eth_market_cap_usd": eth_mc,
        "sol_market_cap_usd": sol_mc,
        "sol_dominance": sol_dom,
        "total3_market_cap_usd": total3_mc,
    }


def get_historical_coin_market_cap(coin_id: str, days_back: int) -> float:
    target_date = datetime.now(timezone.utc) - timedelta(days=days_back)
    date_str = target_date.strftime("%d-%m-%Y")
    data = fetch_json(
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/history",
        params={"date": date_str, "localization": "false"},
    )

    market_cap = data.get("market_data", {}).get("market_cap", {}).get("usd")
    if market_cap is None:
        raise RuntimeError(f"Could not get historical market cap for {coin_id} at {date_str}")
    return float(market_cap)


def get_total3_30d_change(
    *,
    current_total_market_cap: float,
    current_btc_mc: float,
    current_eth_mc: float,
    current_sol_mc: float,
) -> float:
    btc_30 = get_historical_coin_market_cap("bitcoin", 30)
    eth_30 = get_historical_coin_market_cap("ethereum", 30)
    sol_30 = get_historical_coin_market_cap("solana", 30)

    basket_now = current_btc_mc + current_eth_mc + current_sol_mc
    basket_30 = btc_30 + eth_30 + sol_30
    if basket_30 <= 0:
        raise RuntimeError("Invalid historical basket for TOTAL3 proxy")

    basket_change = pct_change(basket_now, basket_30)
    current_total3 = current_total_market_cap - current_btc_mc - current_eth_mc
    historical_total3_proxy = current_total3 / (1 + basket_change / 100.0)
    return pct_change(current_total3, historical_total3_proxy)


def get_dominance_trends(
    *,
    current_total_market_cap: float,
    current_btc_mc: float,
    current_eth_mc: float,
    current_sol_mc: float,
) -> dict[str, float]:
    btc_mc_30 = get_historical_coin_market_cap("bitcoin", 30)
    eth_mc_30 = get_historical_coin_market_cap("ethereum", 30)
    sol_mc_30 = get_historical_coin_market_cap("solana", 30)

    basket_now = current_btc_mc + current_eth_mc + current_sol_mc
    basket_30 = btc_mc_30 + eth_mc_30 + sol_mc_30
    approx_total_30 = current_total_market_cap / (1 + pct_change(basket_now, basket_30) / 100.0)

    btc_dom_now = current_btc_mc / current_total_market_cap * 100.0
    sol_dom_now = current_sol_mc / current_total_market_cap * 100.0
    btc_dom_30 = btc_mc_30 / approx_total_30 * 100.0
    sol_dom_30 = sol_mc_30 / approx_total_30 * 100.0

    return {
        "btc_dominance_30d_change": btc_dom_now - btc_dom_30,
        "sol_dominance_30d_change": sol_dom_now - sol_dom_30,
    }


def resolve_realized_price(*, price: float, realized_price: float | None, mvrv: float | None) -> float:
    if realized_price is not None:
        if realized_price <= 0:
            raise ValueError("realized_price must be > 0")
        return realized_price

    if mvrv is None:
        raise ValueError("Provide realized_price or mvrv")
    if mvrv <= 0:
        raise ValueError("mvrv must be > 0")
    return price / mvrv


def build_market_inputs(*, realized_price: float | None = None, mvrv: float | None = None) -> MarketInputs:
    snapshot = get_current_market_snapshot()
    stable = get_stablecoin_metrics()

    price = float(snapshot["price"])
    resolved_realized_price = resolve_realized_price(
        price=price,
        realized_price=realized_price,
        mvrv=mvrv,
    )
    resolved_mvrv = price / resolved_realized_price

    total_market_cap = float(snapshot["total_market_cap_usd"])
    btc_market_cap = float(snapshot["btc_market_cap_usd"])
    eth_market_cap = float(snapshot["eth_market_cap_usd"])
    sol_market_cap = float(snapshot["sol_market_cap_usd"])

    total3_30d_change = get_total3_30d_change(
        current_total_market_cap=total_market_cap,
        current_btc_mc=btc_market_cap,
        current_eth_mc=eth_market_cap,
        current_sol_mc=sol_market_cap,
    )
    dom_trends = get_dominance_trends(
        current_total_market_cap=total_market_cap,
        current_btc_mc=btc_market_cap,
        current_eth_mc=eth_market_cap,
        current_sol_mc=sol_market_cap,
    )

    stablecoin_market_cap = float(stable["stablecoin_market_cap_usd"])
    stable_share = stablecoin_market_cap / total_market_cap * 100.0

    return MarketInputs(
        price=round(price, 8),
        realized_price=round(resolved_realized_price, 8),
        mvrv=round(resolved_mvrv, 8),
        fed_rate=round(get_latest_fred_value("DFEDTARU"), 3),
        treasury_3m_yield=round(get_latest_fred_value("DTB3"), 3),
        stablecoin_market_cap_usd=round(stablecoin_market_cap, 2),
        stablecoin_share=round(stable_share, 3),
        stablecoin_30d_change=round(float(stable["stablecoin_30d_change"]), 3),
        total_market_cap_usd=round(total_market_cap, 2),
        total3_market_cap_usd=round(float(snapshot["total3_market_cap_usd"]), 2),
        total3_30d_change=round(total3_30d_change, 3),
        btc_dominance=round(float(snapshot["btc_dominance"]), 3),
        eth_dominance=round(float(snapshot["eth_dominance"]), 3),
        sol_dominance=round(float(snapshot["sol_dominance"]), 3),
        btc_dominance_30d_change=round(dom_trends["btc_dominance_30d_change"], 3),
        sol_dominance_30d_change=round(dom_trends["sol_dominance_30d_change"], 3),
        funding_rate=round(get_latest_funding_rate("SOLUSDT"), 6),
        open_interest=round(get_open_interest("SOLUSDT"), 2),
        open_interest_change_7d=round(get_open_interest_change_7d("SOLUSDT"), 3),
    )


def compute_discount_to_realized(price: float, realized_price: float) -> float:
    return (price / realized_price - 1.0) * 100.0


def score_mvrv(mvrv: float) -> float:
    if mvrv <= 0.60:
        return 95.0
    if mvrv <= 1.00:
        return 95.0 - (mvrv - 0.60) / 0.40 * 15.0
    if mvrv <= 1.50:
        return 80.0 - (mvrv - 1.00) / 0.50 * 30.0
    if mvrv <= 2.00:
        return 50.0 - (mvrv - 1.50) / 0.50 * 30.0
    if mvrv <= 2.50:
        return 20.0 - (mvrv - 2.00) / 0.50 * 15.0
    return 5.0


def score_macro(treasury_3m_yield: float, fed_rate: float) -> float:
    yield_score = 100.0 - ((treasury_3m_yield - 2.0) / (5.5 - 2.0)) * 100.0
    fed_score = 100.0 - ((fed_rate - 2.0) / (5.5 - 2.0)) * 100.0
    spread = fed_rate - treasury_3m_yield
    spread_bonus = clamp((spread / 0.25) * 5.0, 0.0, 8.0)
    return clamp(0.55 * clamp(yield_score) + 0.45 * clamp(fed_score) + spread_bonus)


def score_stable(stablecoin_share: float, stablecoin_30d_change: float) -> float:
    base = 50.0 + (stablecoin_share - 10.0) * 8.0
    trend = stablecoin_30d_change * 10.0
    return clamp(base + trend)


def score_dominance_level(sol_dominance: float, btc_dominance: float, stablecoin_share: float) -> float:
    base = 50.0
    sol_part = (sol_dominance - 1.5) * 20.0
    btc_part = -(btc_dominance - 55.0) * 3.0
    stable_part = -(stablecoin_share - 10.0) * 2.0
    return clamp(base + sol_part + btc_part + stable_part)


def score_dominance_trend(btc_d_change_30d: float, sol_d_change_30d: float) -> float:
    score = 50.0
    score += (-btc_d_change_30d) * 6.0
    score += sol_d_change_30d * 10.0
    return clamp(score)


def score_total3(total3_30d_change: float) -> float:
    return clamp(50.0 + total3_30d_change * 2.0)


def score_momentum(funding_rate_pct: float, oi_change_7d: float) -> float:
    score = 50.0
    if funding_rate_pct < 0:
        score += 10.0
    elif funding_rate_pct > 0.10:
        score -= 20.0
    elif funding_rate_pct > 0.03:
        score -= 8.0
    else:
        score += 5.0

    score += oi_change_7d * 1.2
    return clamp(score)


def apply_funding_guardrails(final_score: float, funding_rate_pct: float) -> tuple[float, str | None]:
    adjusted = final_score
    regime_override = None

    if funding_rate_pct > 0.10:
        funding_penalty = min(0.40, (funding_rate_pct - 0.10) * 0.50)
        adjusted *= 1.0 - funding_penalty

    if funding_rate_pct > 0.15 and adjusted > 65:
        adjusted *= 0.90
        regime_override = "Cautious Accumulation (High Funding)"

    return adjusted, regime_override


def classify(score: float) -> str:
    if score >= 80:
        return "Strong Risk-On"
    if score >= 65:
        return "Accumulate / Mild Risk-On"
    if score >= 50:
        return "Neutral"
    if score >= 35:
        return "Defensive"
    return "Risk-Off"


def allocation_from_score(score: float) -> tuple[int, int, int, int]:
    if score >= 80:
        total_risk, stablecoins = 90, 10
    elif score >= 65:
        total_risk, stablecoins = 70, 30
    elif score >= 50:
        total_risk, stablecoins = 50, 50
    elif score >= 35:
        total_risk, stablecoins = 30, 70
    else:
        total_risk, stablecoins = 15, 85

    if score > 75:
        majors_share, alts_share = 40, 60
    elif score > 60:
        majors_share, alts_share = 60, 40
    else:
        majors_share, alts_share = 80, 20

    majors = round(total_risk * majors_share / 100.0)
    alts = round(total_risk * alts_share / 100.0)
    return total_risk, stablecoins, majors, alts


def apply_risk_allocation_guardrails(
    total_risk: int,
    stablecoins: int,
    majors: int,
    alts: int,
    funding_rate_pct: float,
    final_score: float,
) -> tuple[int, int, int, int]:
    adjusted_risk = float(total_risk)

    if funding_rate_pct > 0.10:
        funding_penalty = min(0.40, (funding_rate_pct - 0.10) * 0.50)
        adjusted_risk *= 1.0 - funding_penalty * 0.70

    if funding_rate_pct > 0.15 and final_score > 65:
        adjusted_risk *= 0.70

    adjusted_risk = max(0.0, min(100.0, adjusted_risk))
    stablecoins = int(round(100.0 - adjusted_risk))
    total_risk = int(round(adjusted_risk))

    if total_risk == 0:
        return 0, 100, 0, 0

    majors_share = majors / max(1, majors + alts)
    majors = int(round(total_risk * majors_share))
    alts = total_risk - majors
    return total_risk, stablecoins, majors, alts


def score_band(score: float) -> tuple[str, str]:
    if score >= 80:
        return "Strong", AnsiColor.GREEN
    if score >= 65:
        return "Constructive", AnsiColor.CYAN
    if score >= 50:
        return "Mixed", AnsiColor.YELLOW
    return "Weak", AnsiColor.RED


def score_bias(score: float) -> tuple[str, str]:
    if score >= 65:
        return "↑", "bullish"
    if score >= 50:
        return "→", "neutral"
    return "↓", "defensive"


def colorize(text: str, color: str, *, bold: bool = False) -> str:
    prefix = color
    if bold:
        prefix += AnsiColor.BOLD
    return f"{prefix}{text}{AnsiColor.RESET}"


def print_score_line(label: str, score: float, message: str) -> None:
    band, color = score_band(score)
    arrow, bias = score_bias(score)
    score_text = colorize(f"{score:>6.2f}", color, bold=True)
    band_text = colorize(f"{band:<12}", color)
    bias_text = colorize(f"{arrow} {bias:<9}", color)
    print(f"{label:<20} {score_text}  {band_text} {bias_text} {message}")


def interpret_mvrv(inputs: MarketInputs, result: MarketResult) -> str:
    if inputs.mvrv <= 0.7:
        return (
            f"SOL trades deep below realized value: MVRV is {inputs.mvrv:.3f} and "
            f"discount to realized is {result.discount_to_realized_pct:.2f}%."
        )
    if inputs.mvrv <= 1.0:
        return f"SOL still trades below realized value with MVRV at {inputs.mvrv:.3f}."
    if inputs.mvrv <= 1.5:
        return f"Valuation is fair-to-constructive, with MVRV at {inputs.mvrv:.3f}."
    return f"Valuation looks stretched, with MVRV at {inputs.mvrv:.3f}."


def interpret_stable_score(inputs: MarketInputs, result: MarketResult) -> str:
    if result.stable_score >= 75:
        return (
            f"Stablecoin liquidity is supportive: share is {inputs.stablecoin_share:.2f}% "
            f"and 30d growth is {inputs.stablecoin_30d_change:.2f}%."
        )
    if result.stable_score >= 50:
        return (
            f"Stablecoin liquidity is acceptable: share is {inputs.stablecoin_share:.2f}% "
            f"and 30d growth is {inputs.stablecoin_30d_change:.2f}%."
        )
    return (
        f"Stablecoin liquidity is soft: share is {inputs.stablecoin_share:.2f}% "
        f"and 30d growth is {inputs.stablecoin_30d_change:.2f}%."
    )


def interpret_macro_score(inputs: MarketInputs, result: MarketResult) -> str:
    if result.macro_score >= 65:
        return (
            f"Macro backdrop is supportive with 3M yield at {inputs.treasury_3m_yield:.2f}% "
            f"and Fed rate at {inputs.fed_rate:.2f}%."
        )
    if result.macro_score >= 50:
        return (
            f"Macro is mixed: 3M yield is {inputs.treasury_3m_yield:.2f}% "
            f"and Fed rate is {inputs.fed_rate:.2f}%."
        )
    return (
        f"Macro remains restrictive with 3M yield at {inputs.treasury_3m_yield:.2f}% "
        f"and Fed rate at {inputs.fed_rate:.2f}%."
    )


def interpret_dominance_level(inputs: MarketInputs, result: MarketResult) -> str:
    if result.dominance_score >= 60:
        return (
            f"Dominance mix favors alt risk: SOL dominance is {inputs.sol_dominance:.3f}% "
            f"while BTC dominance is {inputs.btc_dominance:.3f}%."
        )
    if result.dominance_score >= 50:
        return (
            f"Dominance mix is balanced: BTC dominance is {inputs.btc_dominance:.3f}% "
            f"and SOL dominance is {inputs.sol_dominance:.3f}%."
        )
    return (
        f"Dominance mix is defensive: BTC dominance at {inputs.btc_dominance:.3f}% "
        f"is still pressuring the alt setup."
    )


def interpret_dominance_trend(inputs: MarketInputs, result: MarketResult) -> str:
    if result.dominance_trend_score >= 60:
        return (
            f"Trend favors rotation into SOL and away from BTC: BTC dominance changed "
            f"{inputs.btc_dominance_30d_change:.2f} pp and SOL dominance changed "
            f"{inputs.sol_dominance_30d_change:.2f} pp over 30d."
        )
    if result.dominance_trend_score >= 50:
        return (
            f"Dominance trend is mixed: BTC.D changed {inputs.btc_dominance_30d_change:.2f} pp "
            f"and SOL.D changed {inputs.sol_dominance_30d_change:.2f} pp over 30d."
        )
    return (
        f"Dominance trend is not yet alt-friendly: BTC.D changed {inputs.btc_dominance_30d_change:.2f} pp "
        f"while SOL.D changed {inputs.sol_dominance_30d_change:.2f} pp over 30d."
    )


def interpret_total3(inputs: MarketInputs, result: MarketResult) -> str:
    if result.total3_score >= 60:
        return f"TOTAL3 proxy is expanding, with 30d change at {inputs.total3_30d_change:.2f}%."
    if result.total3_score >= 50:
        return f"TOTAL3 proxy is stable, with 30d change at {inputs.total3_30d_change:.2f}%."
    return f"TOTAL3 proxy is contracting, with 30d change at {inputs.total3_30d_change:.2f}%."


def interpret_momentum(inputs: MarketInputs, result: MarketResult) -> str:
    if result.momentum_score >= 60:
        return (
            f"Perp positioning is constructive: funding is {inputs.funding_rate:.4f}% "
            f"and open interest changed {inputs.open_interest_change_7d:.2f}% over 7d."
        )
    if result.momentum_score >= 50:
        return (
            f"Perp positioning is mixed: funding is {inputs.funding_rate:.4f}% "
            f"and open interest changed {inputs.open_interest_change_7d:.2f}% over 7d."
        )
    return (
        f"Perp positioning is overheated or fading: funding is {inputs.funding_rate:.4f}% "
        f"and open interest changed {inputs.open_interest_change_7d:.2f}% over 7d."
    )


def interpret_allocation(result: MarketResult) -> str:
    return (
        f"Target allocation is {result.allocation_total_risk_pct}% risk and "
        f"{result.allocation_stablecoins_pct}% stablecoins, with {result.allocation_majors_pct}% "
        f"in majors and {result.allocation_alts_pct}% in alts."
    )


def print_interpretation(inputs: MarketInputs, result: MarketResult) -> None:
    regime_band, regime_color = score_band(result.final_score)
    regime_arrow, regime_bias = score_bias(result.final_score)

    print("\n=== INTERPRETATION ===")
    print(
        f"Final regime: {colorize(result.regime, regime_color, bold=True)} "
        f"({colorize(regime_band, regime_color)}, {colorize(f'{regime_arrow} {regime_bias}', regime_color)})"
    )
    print(
        f"Current price is {inputs.price:.2f} USD versus realized price {inputs.realized_price:.2f} USD, "
        f"which leaves discount to realized at {result.discount_to_realized_pct:.2f}%."
    )
    print(interpret_allocation(result))

    print_score_line("Final Score", result.final_score, "Weighted aggregate market regime signal.")
    print_score_line("MVRV Score", result.mvrv_score, interpret_mvrv(inputs, result))
    print_score_line("Stable Score", result.stable_score, interpret_stable_score(inputs, result))
    print_score_line("Macro Score", result.macro_score, interpret_macro_score(inputs, result))
    print_score_line("Dominance", result.dominance_score, interpret_dominance_level(inputs, result))
    print_score_line("Dom Trend", result.dominance_trend_score, interpret_dominance_trend(inputs, result))
    print_score_line("TOTAL3 Score", result.total3_score, interpret_total3(inputs, result))
    print_score_line("Momentum", result.momentum_score, interpret_momentum(inputs, result))

    print("\n=== REBALANCE RULES ===")
    print("- Rebalance if target allocation differs by >10 percentage points")
    print("- Increase risk only if final_score > 70 and dominance_trend_score > 50")
    print("- Reduce risk if final_score < 60 or funding_rate > 0.10%")
    print("- If funding_rate > 0.15% and final_score stays bullish, apply cautious accumulation haircut")
    print("- Aggressive derisk if final_score < 40")


def compute_market_score(inputs: MarketInputs) -> MarketResult:
    discount_pct = compute_discount_to_realized(inputs.price, inputs.realized_price)
    mvrv_score = score_mvrv(inputs.mvrv)
    stable_score = score_stable(inputs.stablecoin_share, inputs.stablecoin_30d_change)
    macro_score = score_macro(inputs.treasury_3m_yield, inputs.fed_rate)
    dominance_score = score_dominance_level(
        inputs.sol_dominance,
        inputs.btc_dominance,
        inputs.stablecoin_share,
    )
    dominance_trend_score = score_dominance_trend(
        inputs.btc_dominance_30d_change,
        inputs.sol_dominance_30d_change,
    )
    total3_score = score_total3(inputs.total3_30d_change)
    momentum_score = score_momentum(inputs.funding_rate, inputs.open_interest_change_7d)

    final_score = (
        0.25 * mvrv_score
        + 0.20 * stable_score
        + 0.15 * macro_score
        + 0.15 * dominance_score
        + 0.10 * dominance_trend_score
        + 0.10 * total3_score
        + 0.05 * momentum_score
    )

    final_score, regime_override = apply_funding_guardrails(final_score, inputs.funding_rate)
    regime = regime_override or classify(final_score)
    total_risk, stablecoins, majors, alts = allocation_from_score(final_score)
    total_risk, stablecoins, majors, alts = apply_risk_allocation_guardrails(
        total_risk,
        stablecoins,
        majors,
        alts,
        inputs.funding_rate,
        final_score,
    )

    return MarketResult(
        discount_to_realized_pct=round(discount_pct, 2),
        mvrv_score=round(mvrv_score, 2),
        stable_score=round(stable_score, 2),
        macro_score=round(macro_score, 2),
        dominance_score=round(dominance_score, 2),
        dominance_trend_score=round(dominance_trend_score, 2),
        total3_score=round(total3_score, 2),
        momentum_score=round(momentum_score, 2),
        final_score=round(final_score, 2),
        regime=regime,
        allocation_total_risk_pct=total_risk,
        allocation_stablecoins_pct=stablecoins,
        allocation_majors_pct=majors,
        allocation_alts_pct=alts,
    )


def print_inputs_log() -> None:
    print("=== MARKET INPUTS LOG ===")
    print("price / BTC / ETH / SOL market caps <- CoinGecko /coins/markets")
    print("total_market_cap_usd / btc_dominance / eth_dominance <- CoinGecko /global")
    print("stablecoin_market_cap_usd / stablecoin_30d_change <- DeFiLlama stablecoin history")
    print("treasury_3m_yield <- FRED DTB3")
    print("fed_rate <- FRED DFEDTARU (Federal Funds Target Range - Upper Limit)")
    print("funding_rate / open_interest / open_interest_change_7d <- Binance Futures SOLUSDT")
    print("total3_30d_change <- proxy from current TOTAL3 and 30d BTC+ETH+SOL historical basket")
    print("btc_dominance_30d_change / sol_dominance_30d_change <- proxy from 30d historical market caps")
    print("realized_price <- direct input or derived from --mvrv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build MarketInputs and compute crypto market score.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--realized-price", type=float, help="Directly provide realized price.")
    group.add_argument("--mvrv", type=float, help="Provide MVRV to derive realized price from current SOL price.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    inputs = build_market_inputs(
        realized_price=args.realized_price,
        mvrv=args.mvrv,
    )
    result = compute_market_score(inputs)

    print_inputs_log()

    print("\n=== INPUTS ===")
    for key, value in asdict(inputs).items():
        print(f"{key}: {value}")

    print("\n=== RESULT ===")
    for key, value in asdict(result).items():
        print(f"{key}: {value}")

    print_interpretation(inputs, result)


if __name__ == "__main__":
    main()
