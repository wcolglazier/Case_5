#!/usr/bin/env python3
"""Build price features for ZIP 77845 from the extracts already on disk.

Forward ZHVI changes are stored as y_* columns. Every other column is known
in the same month or earlier.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ZILLOW = ROOT / "data" / "77845" / "zillow_77845.csv"
MONTHLY = ROOT / "data" / "77845_monthly.csv"
OUT = ROOT / "data" / "77845" / "price_features.csv"
LADDER = ROOT / "data" / "77845" / "price_ladder.csv"
DIST = ROOT / "data" / "77845" / "owner_value_distribution.csv"

# Census B25075 owner-occupied value buckets, excluding the total.
VALUE_BUCKETS = [
    ("Less than $10,000", 0, 9999),
    ("$10,000 to $14,999", 10000, 14999),
    ("$15,000 to $19,999", 15000, 19999),
    ("$20,000 to $24,999", 20000, 24999),
    ("$25,000 to $29,999", 25000, 29999),
    ("$30,000 to $34,999", 30000, 34999),
    ("$35,000 to $39,999", 35000, 39999),
    ("$40,000 to $49,999", 40000, 49999),
    ("$50,000 to $59,999", 50000, 59999),
    ("$60,000 to $69,999", 60000, 69999),
    ("$70,000 to $79,999", 70000, 79999),
    ("$80,000 to $89,999", 80000, 89999),
    ("$90,000 to $99,999", 90000, 99999),
    ("$100,000 to $124,999", 100000, 124999),
    ("$125,000 to $149,999", 125000, 149999),
    ("$150,000 to $174,999", 150000, 174999),
    ("$175,000 to $199,999", 175000, 199999),
    ("$200,000 to $249,999", 200000, 249999),
    ("$250,000 to $299,999", 250000, 299999),
    ("$300,000 to $399,999", 300000, 399999),
    ("$400,000 to $499,999", 400000, 499999),
    ("$500,000 to $749,999", 500000, 749999),
    ("$750,000 to $999,999", 750000, 999999),
    ("$1,000,000 to $1,499,999", 1000000, 1499999),
    ("$1,500,000 to $1,999,999", 1500000, 1999999),
    ("$2,000,000 or more", 2000000, None),
]

SERIES = {
    "zhvi": "zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month",
    "zhvi_raw": "zhvi_uc_sfrcondo_tier_0.33_0.67_month",
    "zhvi_sfr": "zhvi_uc_sfr_sm_sa_month",
    "zhvi_condo": "zhvi_uc_condo_sm_sa_month",
    "zhvi_bottom": "zhvi_uc_sfrcondo_tier_0.0_0.33_sm_sa_month",
    "zhvi_top": "zhvi_uc_sfrcondo_tier_0.67_1.0_sm_sa_month",
    "zhvi_2bd": "zhvi_bdrmcnt_2_uc_sfrcondo_tier_0.33_0.67_sm_sa_month",
    "zhvi_3bd": "zhvi_bdrmcnt_3_uc_sfrcondo_tier_0.33_0.67_sm_sa_month",
    "zhvi_4bd": "zhvi_bdrmcnt_4_uc_sfrcondo_tier_0.33_0.67_sm_sa_month",
    "zhvi_5bd": "zhvi_bdrmcnt_5_uc_sfrcondo_tier_0.33_0.67_sm_sa_month",
    "median_sale_price": "median_sale_price_uc_sfrcondo_month",
    "mean_sale_price": "mean_sale_price_uc_sfrcondo_month",
    "sale_to_list": "median_sale_to_list_uc_sfrcondo_sm_month",
    "pct_sold_below_list": "pct_sold_below_list_uc_sfrcondo_sm_month",
    "inventory": "invt_fs_uc_sfrcondo_month",
    "new_pending": "new_pending_uc_sfrcondo_month",
    "price_cut_share": "perc_listings_price_cut_uc_sfrcondo_month",
    "median_price_cut": "med_listings_price_cut_amt_uc_sfrcondo_month",
    "market_temp": "market_temp_index_uc_sfrcondo_month",
    "days_to_close": "mean_days_to_close_uc_sfrcondo_sm_month",
    "zori": "zori_uc_sfrcondomfr_sm_month",
    "forecast_1m": "zhvf_growth_uc_sfrcondo_tier_0.33_0.67_sm_sa_month",
}


def num(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return float(text)


def month_key(stamp: str) -> str:
    year, month, _day = stamp.split("-")
    return f"{year}-{month}-01"


def add_months(key: str, months: int) -> str:
    year, month, _day = key.split("-")
    absolute = int(year) * 12 + (int(month) - 1) + months
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}-01"


def load_zillow() -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, float]] = {name: {} for name in SERIES}
    wanted = {filename: name for name, filename in SERIES.items()}
    # The forecast file has only three future dates. Keep those on their own calendar.
    with ZILLOW.open(newline="") as handle:
        for row in csv.DictReader(handle):
            name = wanted.get(row["series"])
            if name is None:
                continue
            grouped[name][month_key(row["date"])] = float(row["value"])
    return grouped


def load_monthly() -> dict[str, dict[str, float | None]]:
    keep = [
        "mortgage_rate_30y",
        "unemployment_rate",
        "median_listing_price",
        "median_listing_price_per_sqft",
        "median_square_feet",
        "median_days_on_market",
        "active_listing_count",
        "price_reduced_count",
        "median_household_income",
        "acs_vintage_year",
    ]
    rows = {}
    with MONTHLY.open(newline="") as handle:
        for row in csv.DictReader(handle):
            rows[row["date"]] = {name: num(row[name]) for name in keep}
    return rows


def change(series: dict[str, float], key: str, months: int) -> float | None:
    now = series.get(key)
    then = series.get(add_months(key, -months))
    if now is None or then in (None, 0):
        return None
    return now / then - 1


def ratio(left: float | None, right: float | None) -> float | None:
    if left is None or right in (None, 0):
        return None
    return left / right - 1


def payment(price: float | None, annual_rate: float | None) -> float | None:
    if price is None or annual_rate is None or price <= 0 or annual_rate <= 0:
        return None
    monthly = annual_rate / 100 / 12
    loan = price * 0.80
    factor = (1 + monthly) ** 360
    return loan * monthly * factor / (factor - 1)


def income_needed(monthly_payment: float | None) -> float | None:
    if monthly_payment is None:
        return None
    return monthly_payment * 12 / 0.30


def log_value(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    return math.log(value)


def blank(value: float | None) -> str | float:
    return "" if value is None else value


def build() -> list[dict[str, float | str | None]]:
    zillow = load_zillow()
    monthly = load_monthly()
    zhvi = zillow["zhvi"]
    months = sorted(zhvi)
    rows = []
    for key in months:
        base = monthly.get(key, {})
        price = {name: series.get(key) for name, series in zillow.items()}
        list_price = base.get("median_listing_price")
        ppsf = base.get("median_listing_price_per_sqft")
        sqft = base.get("median_square_feet")
        rate = base.get("mortgage_rate_30y")
        income = base.get("median_household_income")
        ask_2500 = ppsf * 2500 if ppsf is not None else None
        value_ppsf = price["zhvi"] / sqft if price["zhvi"] is not None and sqft not in (None, 0) else None
        inventory = price["inventory"]
        pending = price["new_pending"]
        pi_zhvi = payment(price["zhvi"], rate)
        pi_4bd = payment(price["zhvi_4bd"], rate)
        pi_2500 = payment(ask_2500, rate)
        row: dict[str, float | str | None] = {
            "date": key,
            "zhvi": price["zhvi"],
            "zhvi_raw": price["zhvi_raw"],
            "zhvi_sfr": price["zhvi_sfr"],
            "zhvi_condo": price["zhvi_condo"],
            "zhvi_bottom_tier": price["zhvi_bottom"],
            "zhvi_top_tier": price["zhvi_top"],
            "zhvi_2bd": price["zhvi_2bd"],
            "zhvi_3bd": price["zhvi_3bd"],
            "zhvi_4bd": price["zhvi_4bd"],
            "zhvi_5bd": price["zhvi_5bd"],
            "median_sale_price": price["median_sale_price"],
            "mean_sale_price": price["mean_sale_price"],
            "median_list_price": list_price,
            "list_price_per_sqft": ppsf,
            "median_listing_sqft": sqft,
            "ask_price_2500sqft": ask_2500,
            "value_per_sqft_using_listing_size": value_ppsf,
            "typical_rent": price["zori"],
            "log_zhvi": log_value(price["zhvi"]),
            "log_median_sale_price": log_value(price["median_sale_price"]),
            "log_ask_2500sqft": log_value(ask_2500),
            "sfr_premium_over_typical": ratio(price["zhvi_sfr"], price["zhvi"]),
            "condo_gap_vs_sfr": ratio(price["zhvi_condo"], price["zhvi_sfr"]),
            "top_vs_bottom_tier": ratio(price["zhvi_top"], price["zhvi_bottom"]),
            "bed_step_3_over_2": ratio(price["zhvi_3bd"], price["zhvi_2bd"]),
            "bed_step_4_over_3": ratio(price["zhvi_4bd"], price["zhvi_3bd"]),
            "bed_step_5_over_4": ratio(price["zhvi_5bd"], price["zhvi_4bd"]),
            "four_bed_premium_over_typical": ratio(price["zhvi_4bd"], price["zhvi"]),
            "list_premium_over_zhvi": ratio(list_price, price["zhvi"]),
            "sale_premium_over_zhvi": ratio(price["median_sale_price"], price["zhvi"]),
            "mean_sale_skew": ratio(price["mean_sale_price"], price["median_sale_price"]),
            "sale_to_list": price["sale_to_list"],
            "share_sold_below_list": price["pct_sold_below_list"],
            "list_ppsf_premium_over_value_ppsf": ratio(ppsf, value_ppsf),
            "ask_2500_premium_over_4bd": ratio(ask_2500, price["zhvi_4bd"]),
            "ask_2500_premium_over_typical": ratio(ask_2500, price["zhvi"]),
            "size_gap_2500_vs_median_listing": ratio(2500, sqft),
            "zhvi_change_1m": change(zhvi, key, 1),
            "zhvi_change_3m": change(zhvi, key, 3),
            "zhvi_change_12m": change(zhvi, key, 12),
            "zhvi_change_36m": change(zhvi, key, 36),
            "sale_price_change_12m": change(zillow["median_sale_price"], key, 12),
            "list_price_change_12m": change(
                {k: v["median_listing_price"] for k, v in monthly.items() if v["median_listing_price"] is not None},
                key,
                12,
            ),
            "mortgage_rate": rate,
            "mortgage_rate_change_12m": change(
                {k: v["mortgage_rate_30y"] for k, v in monthly.items() if v["mortgage_rate_30y"] is not None},
                key,
                12,
            ),
            "monthly_pi_typical": pi_zhvi,
            "monthly_pi_4bd": pi_4bd,
            "monthly_pi_2500sqft": pi_2500,
            "income_needed_typical": income_needed(pi_zhvi),
            "income_needed_4bd": income_needed(pi_4bd),
            "income_needed_2500sqft": income_needed(pi_2500),
            "price_to_income_typical": price["zhvi"] / income if price["zhvi"] is not None and income not in (None, 0) else None,
            "price_to_income_2500sqft": ask_2500 / income if ask_2500 is not None and income not in (None, 0) else None,
            "pi_share_of_median_income_2500sqft": pi_2500 / (income / 12) if pi_2500 is not None and income not in (None, 0) else None,
            "inventory": inventory,
            "new_pending": pending,
            "months_of_supply": inventory / pending if inventory is not None and pending not in (None, 0) else None,
            "price_cut_share": price["price_cut_share"],
            "median_price_cut_dollars": price["median_price_cut"],
            "market_temp": price["market_temp"],
            "days_on_market": base.get("median_days_on_market"),
            "days_to_close": price["days_to_close"],
            "unemployment_rate": base.get("unemployment_rate"),
            "median_household_income": income,
            "acs_vintage_year": base.get("acs_vintage_year"),
            "y_zhvi_next_12m": change(zhvi, add_months(key, 12), 12),
            "y_zhvi_next_24m": change(zhvi, add_months(key, 24), 24),
            "y_zhvi_next_36m": change(zhvi, add_months(key, 36), 36),
        }
        # change() looks backward from the future date, which is the forward return.
        rows.append(row)
    return rows


def write_rows(rows: list[dict[str, float | str | None]]) -> None:
    fields = list(rows[0].keys())
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: blank(row[key]) if not isinstance(row[key], str) else row[key] for key in fields})


def write_ladder(latest: dict[str, float | str | None]) -> None:
    names = [
        ("Bottom-tier home", "zhvi_bottom_tier"),
        ("2-bedroom", "zhvi_2bd"),
        ("Condo", "zhvi_condo"),
        ("3-bedroom", "zhvi_3bd"),
        ("Typical home (ZHVI)", "zhvi"),
        ("Median sale", "median_sale_price"),
        ("Median listing", "median_list_price"),
        ("Single-family", "zhvi_sfr"),
        ("4-bedroom", "zhvi_4bd"),
        ("2,500 sq ft ask", "ask_price_2500sqft"),
        ("5-bedroom", "zhvi_5bd"),
        ("Top-tier home", "zhvi_top_tier"),
    ]
    rate = latest.get("mortgage_rate")
    with LADDER.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["as_of", "house", "price", "monthly_principal_and_interest", "income_needed_if_payment_is_30pct"],
        )
        writer.writeheader()
        for label, price_key in names:
            price = latest.get(price_key)
            price_value = price if isinstance(price, float) else None
            monthly = payment(price_value, rate if isinstance(rate, float) else None)
            writer.writerow(
                {
                    "as_of": latest["date"],
                    "house": label,
                    "price": blank(price_value),
                    "monthly_principal_and_interest": blank(monthly),
                    "income_needed_if_payment_is_30pct": blank(income_needed(monthly)),
                }
            )


def write_distribution() -> None:
    acs = list(csv.DictReader(open(ROOT / "data" / "77845" / "acs2024_77845.csv")))
    counts = []
    for index, (label, low, high) in enumerate(VALUE_BUCKETS, start=2):
        code = f"B25075_E{index:03d}"
        match = next(row for row in acs if row["variable"] == code)
        counts.append((label, low, high, float(match["estimate"])))
    total = sum(count for _label, _low, _high, count in counts)
    running = 0.0
    with DIST.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["bucket", "low", "high", "owner_homes", "share", "cumulative_share"],
        )
        writer.writeheader()
        for label, low, high, count in counts:
            running += count
            writer.writerow(
                {
                    "bucket": label,
                    "low": low,
                    "high": "" if high is None else high,
                    "owner_homes": int(count),
                    "share": count / total,
                    "cumulative_share": running / total,
                }
            )


def corr(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 24:
        return None
    xs = [a for a, _b in pairs]
    ys = [b for _a, b in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


def summarize(rows: list[dict[str, float | str | None]]) -> None:
    latest = next(row for row in reversed(rows) if row["zhvi"] is not None)
    print("latest", latest["date"])
    for key in [
        "zhvi",
        "zhvi_4bd",
        "ask_price_2500sqft",
        "median_sale_price",
        "list_premium_over_zhvi",
        "sale_to_list",
        "ask_2500_premium_over_4bd",
        "top_vs_bottom_tier",
        "months_of_supply",
        "income_needed_typical",
        "income_needed_4bd",
        "income_needed_2500sqft",
        "price_to_income_2500sqft",
        "pi_share_of_median_income_2500sqft",
    ]:
        value = latest[key]
        print(f"  {key}={value}")
    print("signal vs next-12m ZHVI change, Jul 2016-Sep 2025")
    usable = [
        row
        for row in rows
        if isinstance(row["y_zhvi_next_12m"], float) and row["date"] >= "2016-07-01" and row["date"] <= "2025-09-01"
    ]
    features = [
        "zhvi_change_12m",
        "zhvi_change_3m",
        "mortgage_rate",
        "mortgage_rate_change_12m",
        "list_premium_over_zhvi",
        "sale_to_list",
        "sale_premium_over_zhvi",
        "months_of_supply",
        "price_cut_share",
        "share_sold_below_list",
        "days_on_market",
        "unemployment_rate",
        "market_temp",
        "top_vs_bottom_tier",
        "four_bed_premium_over_typical",
    ]
    for name in features:
        pairs = [
            (row[name], row["y_zhvi_next_12m"])
            for row in usable
            if isinstance(row[name], float) and isinstance(row["y_zhvi_next_12m"], float)
        ]
        value = corr(pairs)
        if value is not None:
            print(f"  {name:32} r={value:+.2f}  n={len(pairs)}")


def main() -> None:
    rows = build()
    write_rows(rows)
    latest = next(row for row in reversed(rows) if row["ask_price_2500sqft"] is not None)
    write_ladder(latest)
    write_distribution()
    print("WROTE", OUT)
    print("rows", len(rows))
    summarize(rows)


if __name__ == "__main__":
    main()
