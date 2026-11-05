#!/usr/bin/env python3
"""Build a monthly modeling table for College Station ZIP 77845.

Sources, all public:
  Zillow Research CSVs (ZHVI all homes, single-family, 4-bedroom)
  Realtor.com monthly ZIP inventory history
  Freddie Mac PMMS weekly 30-year rate (the series FRED publishes as MORTGAGE30US)
  BLS LAUS via the BLS API (College Station-Bryan MSA unemployment, not seasonally adjusted;
    FRED series COLL748URN)
  Census ACS 5-year table-based summary files, ZCTA 77845

The forward-return columns are the assignment target and use only future ZHVI.
They are blank for months that do not yet have a realized outcome.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "77845_monthly.csv"
ZIP = "77845"
UA = "CollegeStationHousingCaseStudy/1.0 (educational project)"

ZILLOW = {
    "zhvi": "zhvi/Zip_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "zhvi_sfr": "zhvi/Zip_zhvi_uc_sfr_sm_sa_month.csv",
    "zhvi_4bd": "zhvi/Zip_zhvi_bdrmcnt_4_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
}
REALTOR_URL = (
    "https://econdata.s3-us-west-2.amazonaws.com/Reports/Core/"
    "RDC_Inventory_Core_Metrics_Zip_History.csv"
)
PMMS_URL = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
BLS_SERIES = "LAUMT481778000000003"
ACS_TABLES = {
    "b19013": "B19013_E001",
    "b01003": "B01003_E001",
    "b25077": "B25077_E001",
    "b25003": None,
}
ACS_YEARS = (2021, 2022, 2023, 2024)


def resolve(host: str) -> str:
    out = subprocess.check_output(
        ["dig", "+time=3", "+tries=1", "@1.1.1.1", host, "A", "+short"],
        text=True,
    )
    for line in out.splitlines():
        line = line.strip()
        if line[:1].isdigit():
            return line
    raise RuntimeError(f"no address for {host}")


def curl(url: str, host: str, ip: str, dest: Path | None = None, timeout: int = 180) -> subprocess.Popen:
    cmd = [
        "/usr/bin/curl",
        "-4",
        "-sS",
        "--http1.1",
        "--connect-timeout",
        "15",
        "--max-time",
        str(timeout),
        "--resolve",
        f"{host}:443:{ip}",
        "-A",
        UA,
        "-L",
        url,
    ]
    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd[1:1] = ["-o", str(dest)]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_curl(url: str, host: str, ip: str, dest: Path, timeout: int = 120) -> None:
    proc = curl(url, host, ip, dest=dest, timeout=timeout)
    _, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed for {url}: {err.decode()[:300]}")


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}-01"


def num(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", ".", "NA", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def stream_zillow(name: str, path: str, ip: str) -> dict[str, float]:
    url = f"https://files.zillowstatic.com/research/public_csvs/{path}"
    print(f"Zillow {name}: streaming {path}")
    proc = curl(url, "files.zillowstatic.com", ip, timeout=300)
    assert proc.stdout is not None
    header = None
    found = None
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if header is None:
            header = next(csv.reader([line]))
            continue
        row = next(csv.reader([line]))
        if len(row) > 2 and row[2] == ZIP:
            found = row
            proc.kill()
            break
    proc.wait()
    if not found or not header:
        raise RuntimeError(f"ZIP {ZIP} not found in {path}")
    series = {}
    for col, value in zip(header, found):
        if len(col) >= 10 and col[0].isdigit():
            parsed = num(value)
            if parsed is not None:
                year, month, _day = col.split("-")
                series[month_key(int(year), int(month))] = parsed
    print(f"  {len(series)} months, {min(series)} to {max(series)}")
    return series


def stream_realtor(ip: str) -> dict[str, dict[str, float | None]]:
    print("Realtor.com: streaming ZIP history")
    proc = curl(REALTOR_URL, "econdata.s3-us-west-2.amazonaws.com", ip, timeout=600)
    assert proc.stdout is not None
    keep = [
        "median_listing_price",
        "active_listing_count",
        "median_days_on_market",
        "new_listing_count",
        "median_listing_price_per_square_foot",
        "price_reduced_count",
        "median_square_feet",
    ]
    rows: dict[str, dict[str, float | None]] = {}
    reader = csv.DictReader(io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline=""))
    for row in reader:
        if (row.get("postal_code") or "").strip() != ZIP:
            continue
        stamp = (row.get("month_date_yyyymm") or "").strip()
        if len(stamp) != 6 or not stamp.isdigit():
            continue
        key = month_key(int(stamp[:4]), int(stamp[4:]))
        rows[key] = {col: num(row.get(col)) for col in keep}
    proc.wait()
    if not rows:
        raise RuntimeError("no Realtor.com rows for 77845")
    print(f"  {len(rows)} months, {min(rows)} to {max(rows)}")
    dest = RAW / "realtor_77845.csv"
    with dest.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", *keep])
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow({"date": key, **rows[key]})
    return rows


def download_pmms(ip: str) -> dict[str, float]:
    print("Freddie Mac PMMS: 30-year mortgage rate")
    dest = RAW / "pmms_history.csv"
    run_curl(PMMS_URL, "www.freddiemac.com", ip, dest, timeout=60)
    monthly: dict[str, list[float]] = defaultdict(list)
    with dest.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rate = num(row.get("pmms30"))
            if rate is None or not row.get("date"):
                continue
            observed = datetime.strptime(row["date"].strip(), "%m/%d/%Y")
            monthly[month_key(observed.year, observed.month)].append(rate)
    series = {key: sum(vals) / len(vals) for key, vals in monthly.items()}
    print(f"  {len(series)} months, {min(series)} to {max(series)}")
    return series


def download_unemployment(ip: str) -> dict[str, float]:
    print("BLS: College Station-Bryan unemployment rate")
    series: dict[str, float] = {}
    dest = RAW / "bls_unemployment_77845_msa.json"
    chunks = []
    for start, end in ((2000, 2009), (2010, 2019), (2020, 2026)):
        payload = json.dumps(
            {"seriesid": [BLS_SERIES], "startyear": str(start), "endyear": str(end)}
        )
        cmd = [
            "/usr/bin/curl",
            "-4",
            "-sS",
            "--http1.1",
            "--connect-timeout",
            "15",
            "--max-time",
            "40",
            "--resolve",
            f"api.bls.gov:443:{ip}",
            "-A",
            UA,
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
            "https://api.bls.gov/publicAPI/v1/timeseries/data/",
        ]
        raw = subprocess.check_output(cmd)
        parsed = json.loads(raw)
        if parsed.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(f"BLS error {start}-{end}: {parsed.get('message')}")
        chunks.append(parsed)
        for point in parsed["Results"]["series"][0]["data"]:
            period = point["period"]
            if not period.startswith("M") or period == "M13":
                continue
            value = num(point.get("value"))
            if value is None:
                continue
            series[month_key(int(point["year"]), int(period[1:]))] = value
    dest.write_text(json.dumps(chunks))
    print(f"  {len(series)} months, {min(series)} to {max(series)}")
    return series


def download_acs(ip: str) -> dict[int, dict[str, float | None]]:
    print("Census ACS 5-year ZCTA 77845")
    by_year: dict[int, dict[str, float | None]] = {}
    for year in ACS_YEARS:
        record: dict[str, float | None] = {}
        for table, column in ACS_TABLES.items():
            url = (
                "https://www2.census.gov/programs-surveys/acs/summary_file/"
                f"{year}/table-based-SF/data/5YRData/acsdt5y{year}-{table}.dat"
            )
            proc = curl(url, "www2.census.gov", ip, timeout=120)
            assert proc.stdout is not None
            header = None
            match = None
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if header is None:
                    header = line.split("|")
                    continue
                geo = line.split("|", 1)[0]
                if geo.endswith(ZIP):
                    match = line.split("|")
                    proc.kill()
                    break
            proc.wait()
            if not header or not match:
                raise RuntimeError(f"ZCTA {ZIP} missing from {year} {table}")
            if table == "b25003":
                idx = {name: i for i, name in enumerate(header)}
                record["occupied_units"] = num(match[idx["B25003_E001"]])
                record["owner_occupied_units"] = num(match[idx["B25003_E002"]])
            else:
                record[column] = num(match[header.index(column)])
            print(f"  {year} {table} geo={geo}")
        by_year[year] = {
            "median_household_income": record.get("B19013_E001"),
            "population": record.get("B01003_E001"),
            "median_owner_occupied_value": record.get("B25077_E001"),
            "occupied_units": record.get("occupied_units"),
            "owner_occupied_units": record.get("owner_occupied_units"),
        }
    dest = RAW / "acs_77845.csv"
    fields = [
        "acs_vintage_year",
        "median_household_income",
        "population",
        "population_growth",
        "median_owner_occupied_value",
        "occupied_units",
        "owner_occupied_units",
    ]
    years = sorted(by_year)
    with dest.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        previous_pop = None
        for year in years:
            pop = by_year[year]["population"]
            growth = None
            if previous_pop not in (None, 0) and pop is not None:
                growth = (pop - previous_pop) / previous_pop
            by_year[year]["population_growth"] = growth
            writer.writerow({"acs_vintage_year": year, "population_growth": growth, **by_year[year]})
            previous_pop = pop
    return by_year


def payment(price: float | None, annual_rate_pct: float | None) -> float | None:
    if price is None or annual_rate_pct is None or price <= 0 or annual_rate_pct <= 0:
        return None
    monthly_rate = annual_rate_pct / 100 / 12
    loan = price * 0.80
    factor = (1 + monthly_rate) ** 360
    return loan * monthly_rate * factor / (factor - 1)


def income_needed(monthly_payment: float | None) -> float | None:
    if monthly_payment is None:
        return None
    return monthly_payment * 12 / 0.30


def add_months(key: str, months: int) -> str:
    year, month, _day = key.split("-")
    absolute = int(year) * 12 + (int(month) - 1) + months
    return month_key(absolute // 12, absolute % 12 + 1)


def forward_return(series: dict[str, float], key: str, months: int) -> float | None:
    today = series.get(key)
    future = series.get(add_months(key, months))
    if today in (None, 0) or future is None:
        return None
    return (future - today) / today


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    ips = {
        "zillow": resolve("files.zillowstatic.com"),
        "realtor": resolve("econdata.s3-us-west-2.amazonaws.com"),
        "freddie": resolve("www.freddiemac.com"),
        "bls": resolve("api.bls.gov"),
        "census": resolve("www2.census.gov"),
    }
    zhvi = {name: stream_zillow(name, path, ips["zillow"]) for name, path in ZILLOW.items()}
    realtor = stream_realtor(ips["realtor"])
    mortgage = download_pmms(ips["freddie"])
    unemployment = download_unemployment(ips["bls"])
    acs = download_acs(ips["census"])
    latest_acs_year = max(acs)

    months = sorted(set(zhvi["zhvi"]) | set(realtor) | set(unemployment))
    months = [key for key in months if key >= min(zhvi["zhvi"])]
    fields = [
        "date",
        "zip",
        "zhvi",
        "zhvi_sfr",
        "zhvi_4bd",
        "zhvi_change_past_12m",
        "zhvi_change_next_12m",
        "zhvi_change_next_24m",
        "zhvi_change_next_36m",
        "mortgage_rate_30y",
        "unemployment_rate",
        "active_listing_count",
        "median_listing_price",
        "median_days_on_market",
        "new_listing_count",
        "median_listing_price_per_sqft",
        "price_reduced_count",
        "median_square_feet",
        "est_list_price_2500sqft",
        "est_monthly_pi_zhvi",
        "est_income_needed_zhvi",
        "est_monthly_pi_2500sqft",
        "est_income_needed_2500sqft",
        "median_household_income",
        "population",
        "population_growth",
        "median_owner_occupied_value",
        "owner_occupied_units",
        "occupied_units",
        "acs_vintage_year",
    ]
    out_rows = []
    for key in months:
        year = int(key[:4])
        vintage = year if year in acs else (latest_acs_year if year > latest_acs_year else None)
        census = acs.get(vintage, {})
        growth = census.get("population_growth") if year in acs else None
        listing = realtor.get(key, {})
        price_sqft = listing.get("median_listing_price_per_square_foot")
        list_2500 = price_sqft * 2500 if price_sqft is not None else None
        rate = mortgage.get(key)
        value = zhvi["zhvi"].get(key)
        pi_zhvi = payment(value, rate)
        pi_2500 = payment(list_2500, rate)
        past = forward_return(zhvi["zhvi"], add_months(key, -12), 12)
        out_rows.append(
            {
                "date": key,
                "zip": ZIP,
                "zhvi": value,
                "zhvi_sfr": zhvi["zhvi_sfr"].get(key),
                "zhvi_4bd": zhvi["zhvi_4bd"].get(key),
                "zhvi_change_past_12m": past,
                "zhvi_change_next_12m": forward_return(zhvi["zhvi"], key, 12),
                "zhvi_change_next_24m": forward_return(zhvi["zhvi"], key, 24),
                "zhvi_change_next_36m": forward_return(zhvi["zhvi"], key, 36),
                "mortgage_rate_30y": rate,
                "unemployment_rate": unemployment.get(key),
                "active_listing_count": listing.get("active_listing_count"),
                "median_listing_price": listing.get("median_listing_price"),
                "median_days_on_market": listing.get("median_days_on_market"),
                "new_listing_count": listing.get("new_listing_count"),
                "median_listing_price_per_sqft": price_sqft,
                "price_reduced_count": listing.get("price_reduced_count"),
                "median_square_feet": listing.get("median_square_feet"),
                "est_list_price_2500sqft": list_2500,
                "est_monthly_pi_zhvi": pi_zhvi,
                "est_income_needed_zhvi": income_needed(pi_zhvi),
                "est_monthly_pi_2500sqft": pi_2500,
                "est_income_needed_2500sqft": income_needed(pi_2500),
                "median_household_income": census.get("median_household_income"),
                "population": census.get("population"),
                "population_growth": growth,
                "median_owner_occupied_value": census.get("median_owner_occupied_value"),
                "owner_occupied_units": census.get("owner_occupied_units"),
                "occupied_units": census.get("occupied_units"),
                "acs_vintage_year": vintage,
            }
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in out_rows:
            writer.writerow(
                {
                    key: ("" if row[key] is None else row[key])
                    for key in fields
                }
            )

    def span(predicate):
        hit = [row["date"] for row in out_rows if predicate(row)]
        return (hit[0], hit[-1], len(hit)) if hit else ("none", "none", 0)

    core = span(
        lambda row: all(
            row[name] is not None
            for name in ("zhvi", "mortgage_rate_30y", "active_listing_count", "unemployment_rate")
        )
    )
    print("\nWROTE", OUT)
    print("rows", len(out_rows), "from", out_rows[0]["date"], "to", out_rows[-1]["date"])
    for label, pred in (
        ("zhvi", lambda r: r["zhvi"] is not None),
        ("zhvi_sfr", lambda r: r["zhvi_sfr"] is not None),
        ("zhvi_4bd", lambda r: r["zhvi_4bd"] is not None),
        ("realtor", lambda r: r["active_listing_count"] is not None),
        ("mortgage", lambda r: r["mortgage_rate_30y"] is not None),
        ("unemployment", lambda r: r["unemployment_rate"] is not None),
        ("next_12m_target", lambda r: r["zhvi_change_next_12m"] is not None),
        ("core_overlap", lambda r: all(r[n] is not None for n in ("zhvi", "mortgage_rate_30y", "active_listing_count", "unemployment_rate"))),
    ):
        start, end, count = span(pred)
        print(f"{label}: {count} months, {start} to {end}")
    latest = next(row for row in reversed(out_rows) if row["zhvi"] is not None)
    print("latest zhvi month", latest["date"], "zhvi", round(latest["zhvi"]), "sfr", round(latest["zhvi_sfr"] or 0), "4bd", round(latest["zhvi_4bd"] or 0))
    priced = next(row for row in reversed(out_rows) if row["est_list_price_2500sqft"] is not None and row["mortgage_rate_30y"] is not None)
    print(
        "2500 sqft asking estimate",
        round(priced["est_list_price_2500sqft"]),
        "on",
        priced["date"],
        "income needed",
        round(priced["est_income_needed_2500sqft"]),
        "vs ACS income",
        priced["median_household_income"],
        "vintage",
        priced["acs_vintage_year"],
    )


if __name__ == "__main__":
    main()
