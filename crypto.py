"""CLI utility for building a Solana market regime snapshot.

How this file is expected to work:
1. Fetch live market data for SOL and the overall crypto market from CoinGecko.
2. Fetch stablecoin market-cap history from DeFiLlama and compute:
   - stablecoin share of total crypto market cap;
   - stablecoin market-cap change over the last 30 observations.
3. Fetch macro rates from FRED:
   - `DTB3` for the 3-month Treasury bill yield;
   - `DFEDTARU` for the upper bound of the Fed funds target range.
4. Accept exactly one valuation input from the command line:
   - `--realized-price` to pass realized price directly; or
   - `--mvrv` to derive realized price from the current SOL price.
5. Combine all inputs into a weighted score that classifies the market regime.
6. Print:
   - a log describing where each input came from;
   - the normalized input values used in scoring;
   - the computed result fields;
   - a human-readable interpretation with ANSI colors.

Expected behavior and assumptions:
- The script is intended to be run as a command-line tool, not imported as a library.
- Internet access is required because all raw inputs are loaded from external APIs.
- The DeFiLlama stablecoin series must contain at least 31 entries to compute the
  30-period change.
- `realized_price` and `mvrv` must be positive numbers.
- The scoring model is heuristic: it does not predict price, but summarizes market
  conditions into four components: valuation, stablecoin liquidity, dominance mix,
  and macro backdrop.

Example usage:
    python crypto.py --realized-price 120
    python crypto.py --mvrv 1.35
"""

import argparse
import csv
import io
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import requests
from requests import exceptions as requests_exceptions


DEFI_LLAMA_STABLES_URL = "https://stablecoins.llama.fi/stablecoincharts/all"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


@dataclass
class SolMarketData:
    name: str
    symbol: str
    price_usd: float
    market_cap_usd: float
    circulating_supply: float


@dataclass
class GlobalMarketData:
    total_crypto_market_cap_usd: float
    btc_dominance_pct: float
    sol_dominance_pct: float
    updated_at: str


@dataclass
class StablecoinMetrics:
    stablecoin_market_cap_usd: float
    stablecoin_market_cap_30d_ago_usd: float
    stablecoin_30d_change_pct: float
    stablecoin_share_pct: float
    latest_timestamp: str
    previous_timestamp: str


@dataclass
class MacroData:
    treasury_3m_yield: float
    fed_rate: float


@dataclass
class MarketInputs:
    price: float
    btc_dominance: float
    sol_dominance: float
    stablecoin_share: float
    stablecoin_30d_change: float
    treasury_3m_yield: float
    fed_rate: float
    realized_price: float | None = None
    mvrv: float | None = None


@dataclass
class MarketResult:
    mvrv: float
    realized_price: float
    discount_to_realized_pct: float
    stable_score: float
    dominance_score: float
    mvrv_score: float
    macro_score: float
    final_score: float
    regime: str


class AnsiColor:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"


def fetch_json(url: str, *, params: dict | None = None) -> dict | list:
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests_exceptions.RequestException as exc:
        raise RuntimeError(
            f"Failed to fetch {url}. Check internet access, DNS, VPN/proxy settings, or API availability. "
            f"Original error: {exc}"
        ) from exc


def fetch_text(url: str, *, params: dict | None = None) -> str:
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.text
    except requests_exceptions.RequestException as exc:
        raise RuntimeError(
            f"Failed to fetch {url}. Check internet access, DNS, VPN/proxy settings, or API availability. "
            f"Original error: {exc}"
        ) from exc


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def format_ts(ts: int | float | None) -> str:
    if ts is None:
        return "n/a"
    try:
        ts = float(ts)
        if ts > 10_000_000_000:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return str(ts)


def get_sol_market_data() -> SolMarketData:
    data = fetch_json(
        COINGECKO_MARKETS_URL,
        params={"vs_currency": "usd", "ids": "solana"},
    )

    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Unexpected CoinGecko markets response: {data}")

    row = data[0]
    return SolMarketData(
        name=row["name"],
        symbol=row["symbol"],
        price_usd=float(row["current_price"]),
        market_cap_usd=float(row["market_cap"]),
        circulating_supply=float(row["circulating_supply"]),
    )


def get_global_market_data() -> GlobalMarketData:
    data = fetch_json(COINGECKO_GLOBAL_URL)

    if "data" not in data:
        raise RuntimeError(f"Unexpected CoinGecko global response: {data}")

    payload = data["data"]
    total_market_cap = payload.get("total_market_cap", {})
    market_cap_pct = payload.get("market_cap_percentage", {})

    if "usd" not in total_market_cap or "btc" not in market_cap_pct or "sol" not in market_cap_pct:
        raise RuntimeError(f"CoinGecko global response is missing required fields: {payload}")

    return GlobalMarketData(
        total_crypto_market_cap_usd=float(total_market_cap["usd"]),
        btc_dominance_pct=float(market_cap_pct["btc"]),
        sol_dominance_pct=float(market_cap_pct["sol"]),
        updated_at=format_ts(payload.get("updated_at")),
    )


def get_stablecoin_series() -> list[dict]:
    data = fetch_json(DEFI_LLAMA_STABLES_URL)

    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Unexpected DeFiLlama response: {data}")

    return data


def pick_latest_and_30d_ago(series: list[dict]) -> tuple[dict, dict]:
    if len(series) < 31:
        raise RuntimeError("Not enough history to compute 30d change")
    return series[-1], series[-31]


def extract_numeric_value(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    if isinstance(value, dict):
        for key in [
            "peggedUSD",
            "usd",
            "value",
            "total",
            "circulating",
            "totalCirculatingUSD",
        ]:
            if key in value and isinstance(value[key], (int, float, str)):
                return float(value[key])
        for nested in value.values():
            if isinstance(nested, (int, float, str)):
                try:
                    return float(nested)
                except ValueError:
                    pass
    raise RuntimeError(f"Cannot extract numeric value from: {value}")


def extract_total_stablecoin_market_cap(entry: dict) -> float:
    for key in [
        "totalCirculatingUSD",
        "totalCirculating",
        "circulatingUSD",
        "circulating",
        "total",
        "value",
    ]:
        if key in entry:
            return extract_numeric_value(entry[key])
    raise RuntimeError(f"Unexpected stablecoin entry format: {entry}")


def get_stablecoin_metrics(total_crypto_market_cap_usd: float) -> StablecoinMetrics:
    stable_series = get_stablecoin_series()
    latest, prev_30d = pick_latest_and_30d_ago(stable_series)

    total_stablecoin_market_cap = extract_total_stablecoin_market_cap(latest)
    total_stablecoin_market_cap_30d_ago = extract_total_stablecoin_market_cap(prev_30d)

    if total_stablecoin_market_cap_30d_ago == 0:
        raise ZeroDivisionError("Stablecoin market cap 30d ago is zero")

    stablecoin_30d_change = (
        (total_stablecoin_market_cap - total_stablecoin_market_cap_30d_ago)
        / total_stablecoin_market_cap_30d_ago
        * 100.0
    )
    stablecoin_share = total_stablecoin_market_cap / total_crypto_market_cap_usd * 100.0

    return StablecoinMetrics(
        stablecoin_market_cap_usd=round(total_stablecoin_market_cap, 2),
        stablecoin_market_cap_30d_ago_usd=round(total_stablecoin_market_cap_30d_ago, 2),
        stablecoin_30d_change_pct=round(stablecoin_30d_change, 2),
        stablecoin_share_pct=round(stablecoin_share, 2),
        latest_timestamp=format_ts(latest.get("date")),
        previous_timestamp=format_ts(prev_30d.get("date")),
    )


def get_latest_fred_value(series_id: str) -> float:
    csv_text = fetch_text(FRED_CSV_URL, params={"id": series_id})
    rows = csv.DictReader(io.StringIO(csv_text))

    latest_value: float | None = None
    for row in rows:
        for key, raw_value in row.items():
            if key == "DATE":
                continue

            value = (raw_value or "").strip()
            if not value or value == ".":
                continue

            try:
                latest_value = float(value)
                break
            except ValueError:
                continue

    if latest_value is None:
        raise RuntimeError(f"No numeric FRED values found for series {series_id}")

    return latest_value


def get_macro_data() -> MacroData:
    return MacroData(
        treasury_3m_yield=round(get_latest_fred_value("DTB3"), 3),
        fed_rate=round(get_latest_fred_value("DFEDTARU"), 3),
    )


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
    sol_market = get_sol_market_data()
    global_market = get_global_market_data()
    stablecoin_metrics = get_stablecoin_metrics(global_market.total_crypto_market_cap_usd)
    macro = get_macro_data()

    resolved_realized_price = resolve_realized_price(
        price=sol_market.price_usd,
        realized_price=realized_price,
        mvrv=mvrv,
    )

    return MarketInputs(
        price=round(sol_market.price_usd, 8),
        btc_dominance=round(global_market.btc_dominance_pct, 3),
        sol_dominance=round(global_market.sol_dominance_pct, 3),
        stablecoin_share=stablecoin_metrics.stablecoin_share_pct,
        stablecoin_30d_change=stablecoin_metrics.stablecoin_30d_change_pct,
        treasury_3m_yield=macro.treasury_3m_yield,
        fed_rate=macro.fed_rate,
        realized_price=round(resolved_realized_price, 8),
        mvrv=round(mvrv, 8) if mvrv is not None else None,
    )


def compute_mvrv(price: float, realized_price: float) -> float:
    if realized_price <= 0:
        raise ValueError("realized_price must be > 0")
    return price / realized_price


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


def calc_stable_score(stablecoin_share: float, stablecoin_30d_change: float) -> float:
    base = 50.0 + (stablecoin_share - 10.0) * 8.0
    trend = stablecoin_30d_change * 10.0
    return clamp(base + trend)


def calc_dominance_score(sol_dominance: float, btc_dominance: float, stablecoin_share: float) -> float:
    base = 50.0
    sol_part = (sol_dominance - 1.5) * 20.0
    btc_part = -(btc_dominance - 55.0) * 3.0
    stable_part = -(stablecoin_share - 10.0) * 2.0
    return clamp(base + sol_part + btc_part + stable_part)


def classify(score: float) -> str:
    if score >= 80:
        return "Aggressive Buy / Strong Risk-On"
    if score >= 60:
        return "Accumulate / Mild Risk-On"
    if score >= 40:
        return "Neutral / Wait"
    return "Defensive / Risk-Off"


def score_band(score: float) -> tuple[str, str]:
    if score >= 80:
        return "Strong", AnsiColor.GREEN
    if score >= 60:
        return "Constructive", AnsiColor.CYAN
    if score >= 40:
        return "Mixed", AnsiColor.YELLOW
    return "Weak", AnsiColor.RED


def score_bias(score: float) -> tuple[str, str]:
    if score >= 60:
        return "↑", "bullish"
    if score >= 40:
        return "→", "neutral"
    return "↓", "bearish"


def colorize(text: str, color: str, *, bold: bool = False) -> str:
    prefix = color
    if bold:
        prefix += AnsiColor.BOLD
    return f"{prefix}{text}{AnsiColor.RESET}"


def interpret_mvrv(result: MarketResult) -> str:
    if result.mvrv <= 0.7:
        return "SOL trades well below realized value, which usually supports a favorable valuation setup."
    if result.mvrv <= 1.0:
        return "SOL still trades below realized value, so valuation remains supportive."
    if result.mvrv <= 1.5:
        return "Valuation is no longer cheap, but it is not yet stretched."
    return "Valuation looks extended versus realized value, which reduces margin of safety."


def interpret_stable_score(inputs: MarketInputs, result: MarketResult) -> str:
    if result.stable_score >= 75:
        return (
            f"Stablecoin liquidity looks supportive: share is {inputs.stablecoin_share:.2f}% "
            f"and 30d growth is {inputs.stablecoin_30d_change:.2f}%."
        )
    if result.stable_score >= 50:
        return (
            f"Liquidity backdrop is acceptable, but not especially strong: share is {inputs.stablecoin_share:.2f}% "
            f"and 30d growth is {inputs.stablecoin_30d_change:.2f}%."
        )
    return (
        f"Stablecoin liquidity is a headwind: share is {inputs.stablecoin_share:.2f}% "
        f"and 30d growth is {inputs.stablecoin_30d_change:.2f}%."
    )


def interpret_dominance_score(inputs: MarketInputs, result: MarketResult) -> str:
    if result.dominance_score >= 60:
        return (
            f"Dominance mix favors alt risk: SOL dominance is {inputs.sol_dominance:.3f}% "
            f"while BTC dominance is {inputs.btc_dominance:.3f}%."
        )
    if result.dominance_score >= 40:
        return (
            f"Dominance signals are mixed: SOL dominance is {inputs.sol_dominance:.3f}% "
            f"and BTC dominance is still elevated at {inputs.btc_dominance:.3f}%."
        )
    return (
        f"Dominance is defensive: BTC dominance at {inputs.btc_dominance:.3f}% is pressuring the alt setup."
    )


def interpret_macro_score(inputs: MarketInputs, result: MarketResult) -> str:
    if result.macro_score >= 65:
        return (
            f"Macro backdrop is supportive with 3M yield at {inputs.treasury_3m_yield:.2f}% "
            f"and Fed rate at {inputs.fed_rate:.2f}%."
        )
    if result.macro_score >= 45:
        return (
            f"Macro is only mildly supportive: 3M yield is {inputs.treasury_3m_yield:.2f}% "
            f"and Fed rate is {inputs.fed_rate:.2f}%."
        )
    return (
        f"Macro remains restrictive: 3M yield is {inputs.treasury_3m_yield:.2f}% "
        f"and Fed rate is {inputs.fed_rate:.2f}%."
    )


def print_score_line(label: str, score: float, message: str) -> None:
    band, color = score_band(score)
    arrow, bias = score_bias(score)
    score_text = colorize(f"{score:>6.2f}", color, bold=True)
    band_text = colorize(f"{band:<12}", color)
    bias_text = colorize(f"{arrow} {bias:<7}", color)
    print(f"{label:<18} {score_text}  {band_text} {bias_text} {message}")


def print_interpretation(inputs: MarketInputs, result: MarketResult) -> None:
    regime_band, regime_color = score_band(result.final_score)
    regime_arrow, regime_bias = score_bias(result.final_score)

    print("\n=== INTERPRETATION ===")
    print(
        f"Final regime: {colorize(result.regime, regime_color, bold=True)} "
        f"({colorize(regime_band, regime_color)}, {colorize(f'{regime_arrow} {regime_bias}', regime_color)})"
    )
    print(
        f"Current price is {inputs.price:.2f} USD versus realized price {result.realized_price:.2f} USD, "
        f"which leaves discount to realized at {result.discount_to_realized_pct:.2f}%."
    )

    print_score_line("Final Score", result.final_score, "Weighted aggregate market signal.")
    print_score_line("MVRV Score", result.mvrv_score, interpret_mvrv(result))
    print_score_line("Stable Score", result.stable_score, interpret_stable_score(inputs, result))
    print_score_line("Dominance", result.dominance_score, interpret_dominance_score(inputs, result))
    print_score_line("Macro Score", result.macro_score, interpret_macro_score(inputs, result))


def compute_market_score(inputs: MarketInputs) -> MarketResult:
    if inputs.realized_price is None:
        raise ValueError("MarketInputs.realized_price must be set before scoring")

    mvrv = compute_mvrv(inputs.price, inputs.realized_price)
    discount_pct = compute_discount_to_realized(inputs.price, inputs.realized_price)
    stable_score = calc_stable_score(inputs.stablecoin_share, inputs.stablecoin_30d_change)
    dominance_score = calc_dominance_score(
        inputs.sol_dominance,
        inputs.btc_dominance,
        inputs.stablecoin_share,
    )
    mvrv_score = score_mvrv(mvrv)
    macro_score = score_macro(inputs.treasury_3m_yield, inputs.fed_rate)

    final_score = (
        0.35 * mvrv_score
        + 0.25 * stable_score
        + 0.20 * dominance_score
        + 0.20 * macro_score
    )

    return MarketResult(
        mvrv=round(mvrv, 6),
        realized_price=round(inputs.realized_price, 8),
        discount_to_realized_pct=round(discount_pct, 2),
        stable_score=round(stable_score, 2),
        dominance_score=round(dominance_score, 2),
        mvrv_score=round(mvrv_score, 2),
        macro_score=round(macro_score, 2),
        final_score=round(final_score, 2),
        regime=classify(final_score),
    )


def print_inputs_log() -> None:
    print("=== MARKET INPUTS LOG ===")
    print("price <- SOL spot price from CoinGecko /coins/markets")
    print("btc_dominance <- CoinGecko /global market_cap_percentage.btc")
    print("sol_dominance <- CoinGecko /global market_cap_percentage.sol")
    print("stablecoin_share <- DeFiLlama stablecoins total / CoinGecko total crypto market cap")
    print("stablecoin_30d_change <- 30d change of DeFiLlama stablecoins total")
    print("treasury_3m_yield <- FRED DTB3")
    print("fed_rate <- FRED DFEDTARU (Federal Funds Target Range - Upper Limit)")
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
