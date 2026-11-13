"""
Step 2 - Pull ALL rows, save a dated raw snapshot, and test what we noticed in Step 1.

Run:  python step2_extract.py
Output: data/raw/<today>/layer_0.json and layer_1.json  +  a short report
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import requests

BASE_URL = (
    "https://systemdataportal.nationalgrid.com/arcgis/rest/services/"
    "MASDP/MA_HostingCapacity_with_DGPending/MapServer"
)
PAGE_SIZE = 1000  # the server allows max 2000 per request; we stay below it


def fetch_all_rows(layer_id):
    """Ask for rows page by page until the server says 'no more'."""
    all_rows, offset = [], 0
    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",   # same order every time, so pages don't overlap
            "resultOffset": offset,         # "start reading from row number X"
            "resultRecordCount": PAGE_SIZE, # "give me this many"
            "f": "json",
        }
        resp = requests.get(f"{BASE_URL}/{layer_id}/query", params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"])

        page = [f["attributes"] for f in data.get("features", [])]
        all_rows.extend(page)
        print(f"  layer {layer_id}: got {len(page)} rows (offset {offset})")

        if not data.get("exceededTransferLimit") or not page:
            return all_rows
        offset += PAGE_SIZE


def to_date(ms):
    """ArcGIS dates are milliseconds since 1970 -> turn into a normal date."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date() if ms else None


# ---------- 1. Extract + save raw snapshot ----------
snapshot_day = date.today().isoformat()
out_dir = Path("data/raw") / snapshot_day
out_dir.mkdir(parents=True, exist_ok=True)

feeders = fetch_all_rows(0)
substations = fetch_all_rows(1)

for name, rows in [("layer_0", feeders), ("layer_1", substations)]:
    (out_dir / f"{name}.json").write_text(json.dumps(rows, indent=2))
print(f"\nSaved raw snapshot to {out_dir}\n")

# ---------- 2. Test our Step 1 guesses ----------
print("=== CHECK A: How fresh is the data? ===")
dates = [to_date(r["Updated_Date"]) for r in feeders if r.get("Updated_Date")]
print(f"  oldest: {min(dates)}   newest: {max(dates)}   days since newest: {(date.today() - max(dates)).days}")

print("\n=== CHECK B: Does available + connected always equal 1 MW? ===")
totals = {}
for r in feeders:
    total = round((r["HC_Available_MW"] or 0) + (r["Total_Nameplate_Interconnected_DG_excluding_BESS_MW"] or 0), 3)
    totals[total] = totals.get(total, 0) + 1
for total, n in sorted(totals.items(), key=lambda x: -x[1])[:8]:
    print(f"  total = {total} MW  ->  {n} feeders")

print("\n=== CHECK C: Are feeder IDs unique? ===")
ids = [r["Master_CDF"] for r in feeders]
print(f"  rows: {len(ids)}   unique IDs: {len(set(ids))}   missing IDs: {sum(1 for i in ids if not i)}")

print("\n=== CHECK D: How many substation rows are exact repeats? ===")
ignore = {"OBJECTID", "DG_Installed_Color"}
unique_subs = {json.dumps({k: v for k, v in r.items() if k not in ignore}, sort_keys=True) for r in substations}
print(f"  rows: {len(substations)}   truly different rows: {len(unique_subs)}")

print("\n=== CHECK E: How many feeders are nearly full (< 0.1 MW left)? ===")
full = [r for r in feeders if (r["HC_Available_MW_pending"] or 0) < 0.1]
print(f"  {len(full)} of {len(feeders)} feeders")