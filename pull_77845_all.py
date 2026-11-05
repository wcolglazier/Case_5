#!/usr/bin/env python3
"""Pull every public ZIP/ZCTA 77845 series that can be reached without an API key.

Writes extracts under data/77845/. National files are streamed and discarded
after the 77845 row is found.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "77845"
RAW = ROOT / "data" / "raw"
ZIP = "77845"
UA = "CollegeStationHousingCaseStudy/1.0 (educational project)"
ACS_CHAPTERS = {"01", "02", "08", "11", "17", "19", "20", "21", "22", "23", "24", "25", "27", "28"}
LOCK = threading.Lock()


def resolve(host: str) -> str:
    text = subprocess.check_output(
        ["dig", "+time=3", "+tries=1", "@1.1.1.1", host, "A", "+short"],
        text=True,
    )
    for line in text.splitlines():
        if line.strip()[:1].isdigit():
            return line.strip()
    raise RuntimeError(f"no address for {host}")


def curl_cmd(url: str, host: str, ip: str, timeout: int, extra: list[str] | None = None) -> list[str]:
    cmd = [
        "/usr/bin/curl",
        "-4",
        "-sS",
        "--http1.1",
        "--connect-timeout",
        "12",
        "--max-time",
        str(timeout),
        "--resolve",
        f"{host}:443:{ip}",
        "-A",
        UA,
        "-L",
        url,
    ]
    if extra:
        cmd[1:1] = extra
    return cmd


def http_status(url: str, host: str, ip: str) -> str:
    cmd = curl_cmd(url, host, ip, 20, ["-o", "/dev/null", "-w", "%{http_code}", "-I"])
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return "000"


def zillow_candidates() -> list[tuple[str, str]]:
    zhvi = [
        "uc_sfrcondo_tier_0.33_0.67_sm_sa_month",
        "uc_sfrcondo_tier_0.33_0.67_month",
        "uc_sfrcondo_tier_0.0_0.33_sm_sa_month",
        "uc_sfrcondo_tier_0.67_1.0_sm_sa_month",
        "uc_sfr_sm_sa_month",
        "uc_sfr_tier_0.33_0.67_sm_sa_month",
        "uc_condo_sm_sa_month",
        "uc_condo_tier_0.33_0.67_sm_sa_month",
        "uc_sfrcondo_sm_sa_month",
    ] + [f"bdrmcnt_{n}_uc_sfrcondo_tier_0.33_0.67_sm_sa_month" for n in range(1, 6)]
    weekly = ["uc_sfrcondo_sm_week", "uc_sfrcondo_week", "uc_sfrcondo_sm_month", "uc_sfrcondo_month"]
    specs = {
        "zhvi": zhvi,
        "zhvf_growth": ["uc_sfrcondo_tier_0.33_0.67_sm_sa_month"],
        "zori": [
            "uc_sfrcondomfr_sm_month",
            "uc_sfr_sm_month",
            "uc_mfr_sm_month",
            "uc_condo_sm_month",
        ],
        "zorf_growth": ["uc_sfrcondomfr_sm_month", "uc_sfrcondomfr_sm_sa_month"],
        "invt_fs": weekly,
        "new_listings": weekly,
        "new_pending": weekly,
        "new_con_sales_count_raw": ["uc_sfrcondo_month"],
        "median_list_price": weekly,
        "mean_list_price": weekly,
        "median_sale_price": weekly,
        "mean_sale_price": weekly,
        "median_sale_price_per_sqft": weekly,
        "sales_count_now": ["uc_sfrcondo_month", "uc_sfrcondo_sm_month"],
        "market_temp_index": ["uc_sfrcondo_month", "uc_sfrcondo_sm_month"],
        "perc_listings_price_cut": weekly,
        "mean_listings_price_cut_amt": weekly,
        "mean_listings_price_cut_pct": weekly,
        "med_listings_price_cut_amt": weekly,
        "med_listings_price_cut_pct": weekly,
        "mean_days_to_pending": weekly,
        "med_days_to_pending": weekly,
        "mean_days_to_close": ["uc_sfrcondo_sm_month", "uc_sfrcondo_month"],
        "med_days_to_close": ["uc_sfrcondo_sm_month", "uc_sfrcondo_month"],
        "mean_sale_to_list": weekly,
        "median_sale_to_list": weekly,
        "pct_sold_above_list": weekly,
        "pct_sold_below_list": weekly,
        "new_homeowner_income_needed_downpayment_0.20": [
            "uc_sfrcondo_tier_0.33_0.67_sm_sa_month"
        ],
        "mortgage_payment_downpayment_0.20": ["uc_sfrcondo_tier_0.33_0.67_sm_sa_month"],
        "total_monthly_payment_downpayment_0.20": ["uc_sfrcondo_tier_0.33_0.67_sm_sa_month"],
        "affordable_price_downpayment_0.20": ["uc_sfrcondo_tier_0.33_0.67_sm_sa_month"],
        "years_to_save_downpayment_0.20": ["uc_sfrcondo_tier_0.33_0.67_sm_sa_month"],
    }
    found = []
    for metric, suffixes in specs.items():
        for suffix in suffixes:
            name = f"{metric}_{suffix}"
            url = (
                "https://files.zillowstatic.com/research/public_csvs/"
                f"{metric}/Zip_{metric}_{suffix}.csv"
            )
            found.append((name, url))
    return found


def stream_region_row(url: str, host: str, ip: str, timeout: int = 240) -> tuple[list[str], list[str]] | None:
    proc = subprocess.Popen(
        curl_cmd(url, host, ip, timeout),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    header = None
    match = None
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if not line:
            continue
        if header is None:
            header = next(csv.reader([line]))
            continue
        row = next(csv.reader([line]))
        if len(row) > 2 and row[2] == ZIP:
            match = row
            proc.kill()
            break
    proc.wait()
    if header and match:
        return header, match
    return None


def melt_zillow(name: str, header: list[str], row: list[str]) -> list[dict[str, str]]:
    meta = {header[i]: row[i] for i in range(min(len(header), len(row))) if not header[i][:1].isdigit()}
    records = []
    for col, value in zip(header, row):
        if len(col) < 8 or not col[0].isdigit() or value.strip() in {"", "NA"}:
            continue
        records.append(
            {
                "series": name,
                "date": col,
                "value": value,
                "region_id": meta.get("RegionID", ""),
                "city": meta.get("City", ""),
                "state": meta.get("StateName", meta.get("State", "")),
                "metro": meta.get("Metro", ""),
                "county": meta.get("CountyName", ""),
            }
        )
    return records


def pull_zillow(ip: str) -> None:
    candidates = zillow_candidates()
    print(f"Zillow: checking {len(candidates)} ZIP files")
    live = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(http_status, url, "files.zillowstatic.com", ip): (name, url)
            for name, url in candidates
        }
        for future in as_completed(futures):
            name, url = futures[future]
            status = future.result()
            if status == "200":
                live.append((name, url))
    live.sort()
    print(f"Zillow: {len(live)} files exist")
    path = OUT / "zillow_77845.csv"
    fields = ["series", "date", "value", "region_id", "city", "state", "metro", "county"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for name, url in live:
            found = stream_region_row(url, "files.zillowstatic.com", ip)
            if not found:
                print(f"  MISSING ROW {name}")
                continue
            records = melt_zillow(name, *found)
            writer.writerows(records)
            handle.flush()
            dates = [record["date"] for record in records]
            latest = records[-1]["value"] if records else ""
            print(f"  {name}: {len(records)} obs, {dates[0] if dates else '-'} to {dates[-1] if dates else '-'}, latest {latest}")


def pull_realtor(ip: str) -> None:
    print("Realtor.com hotness history")
    url = "https://econdata.s3-us-west-2.amazonaws.com/Reports/Hotness/RDC_Inventory_Hotness_Metrics_Zip_History.csv"
    proc = subprocess.Popen(
        curl_cmd(url, "econdata.s3-us-west-2.amazonaws.com", ip, 400),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    reader = csv.DictReader(io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline=""))
    rows = [row for row in reader if (row.get("postal_code") or "").strip() == ZIP]
    proc.wait()
    dest = OUT / "realtor_hotness_77845.csv"
    if rows:
        with dest.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    print(f"  hotness rows {len(rows)}")

    print("Realtor.com current-month ZIP file")
    current = "https://econdata.s3-us-west-2.amazonaws.com/Reports/Core/RDC_Inventory_Core_Metrics_Zip.csv"
    text = subprocess.check_output(curl_cmd(current, "econdata.s3-us-west-2.amazonaws.com", ip, 60), text=True)
    current_rows = [
        row
        for row in csv.DictReader(io.StringIO(text))
        if (row.get("postal_code") or "").strip() == ZIP
    ]
    dest = OUT / "realtor_current_77845.csv"
    if current_rows:
        with dest.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(current_rows[0].keys()))
            writer.writeheader()
            writer.writerows(current_rows)
        print(f"  current month {current_rows[0].get('month_date_yyyymm')}")


def pull_irs(ip: str) -> None:
    print("IRS ZIP-code income")
    frames = []
    for year, name in ((2022, "22zpallagi.csv"), (2023, "23zpallagi.csv")):
        url = f"https://www.irs.gov/pub/irs-soi/{name}"
        status = http_status(url, "www.irs.gov", ip)
        print(f"  {name} status {status}")
        if status not in {"200", "301", "302"}:
            continue
        dest = RAW / name
        cmd = curl_cmd(url, "www.irs.gov", ip, 120, ["-o", str(dest)])
        subprocess.check_call(cmd)
        with dest.open(newline="", errors="replace") as handle:
            reader = csv.DictReader(handle)
            kept = []
            for row in reader:
                zip_value = (row.get("zipcode") or row.get("ZIPCODE") or row.get("zip") or "").strip()
                if zip_value == ZIP:
                    row["tax_year"] = str(year)
                    kept.append(row)
            print(f"  {name} rows {len(kept)}")
            frames.extend(kept)
    if not frames:
        return
    fields = ["tax_year"] + [key for key in frames[0].keys() if key != "tax_year"]
    with (OUT / "irs_77845.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(frames)


def acs_tables(ip: str) -> list[str]:
    url = "https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/"
    html = subprocess.check_output(curl_cmd(url, "www2.census.gov", ip, 40), text=True, errors="replace")
    files = re.findall(r'href="(acsdt5y2024-b\d+\.dat)"', html, re.I)
    selected = []
    for name in files:
        chapter = re.search(r"b(\d\d)", name).group(1)
        if chapter in ACS_CHAPTERS:
            selected.append(name)
    return sorted(set(selected))


def pull_one_acs(filename: str, ip: str) -> list[dict[str, str]]:
    url = (
        "https://www2.census.gov/programs-surveys/acs/summary_file/2024/"
        f"table-based-SF/data/5YRData/{filename}"
    )
    proc = subprocess.Popen(
        curl_cmd(url, "www2.census.gov", ip, 90),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    header = None
    values = None
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if header is None:
            header = line.split("|")
            continue
        geo = line.split("|", 1)[0]
        if geo.endswith(ZIP):
            values = line.split("|")
            proc.kill()
            break
    proc.wait()
    if not header or not values:
        return []
    table = filename.split("-", 1)[1].replace(".dat", "").upper()
    records = []
    buckets: dict[str, dict[str, str]] = {}
    for name in header[1:]:
        match = re.match(r"(.+)_([EM])(\d+)$", name)
        if not match:
            continue
        stem, kind, number = match.groups()
        buckets.setdefault(f"{stem}_{number}", {})["estimate" if kind == "E" else "moe"] = name
    index = {name: i for i, name in enumerate(header)}
    for _variable, cols in buckets.items():
        est_col = cols.get("estimate")
        if not est_col:
            continue
        moe_col = cols.get("moe")
        est_i = index[est_col]
        moe_i = index[moe_col] if moe_col else None
        records.append(
            {
                "table": table,
                "variable": est_col,
                "estimate": values[est_i] if est_i < len(values) else "",
                "margin_of_error": values[moe_i] if moe_i is not None and moe_i < len(values) else "",
            }
        )
    return records


def pull_acs(ip: str) -> None:
    tables = acs_tables(ip)
    print(f"Census ACS 2024 base tables: {len(tables)}")
    dest = OUT / "acs2024_77845.csv"
    with dest.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["table", "variable", "estimate", "margin_of_error"])
        writer.writeheader()
        done = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(pull_one_acs, name, ip): name for name in tables}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    records = future.result()
                except Exception as exc:
                    print(f"  FAIL {name} {exc}")
                    continue
                with LOCK:
                    writer.writerows(records)
                    handle.flush()
                done += 1
                if done % 25 == 0 or not records:
                    print(f"  {done}/{len(tables)} {name} vars {len(records)}")
        print(f"Census tables written: {done}")


def pull_redfin() -> None:
    print("Redfin ZIP market tracker")
    hosts = [
        "redfin-public-data.s3.us-west-2.amazonaws.com",
        "redfin-public-data.s3-us-west-2.amazonaws.com",
    ]
    url_path = "/redfin_market_tracker/zip_code_market_tracker.tsv000.gz"
    for host in hosts:
        try:
            ip = resolve(host)
        except RuntimeError as exc:
            print(f"  {exc}")
            continue
        url = f"https://{host}{url_path}"
        status = http_status(url, host, ip)
        print(f"  {host} status {status}")
        if status != "200":
            # Some buckets reject HEAD. Try a tiny GET.
            proc = subprocess.run(
                curl_cmd(url, host, ip, 25, ["-r", "0-200", "-D", "-"]),
                capture_output=True,
            )
            head = proc.stdout[:300].decode("utf-8", "replace").replace("\n", " | ")
            print(f"  GET probe {head[:220]}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    ips = {
        "zillow": resolve("files.zillowstatic.com"),
        "realtor": resolve("econdata.s3-us-west-2.amazonaws.com"),
        "irs": resolve("www.irs.gov"),
        "census": resolve("www2.census.gov"),
    }
    pull_zillow(ips["zillow"])
    pull_realtor(ips["realtor"])
    pull_irs(ips["irs"])
    pull_redfin()
    pull_acs(ips["census"])
    print("DONE", OUT)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "acs":
        OUT.mkdir(parents=True, exist_ok=True)
        pull_acs(resolve("www2.census.gov"))
    else:
        main()
