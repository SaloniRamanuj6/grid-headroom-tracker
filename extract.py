import json
from datetime import date, datetime, timezone
from pathlib import Path

import requests

BASE_URL = (
    "https://systemdataportal.nationalgrid.com/arcgis/rest/services/"
    "MASDP/MA_HostingCapacity_with_DGPending/MapServer"
)
NY_URL = "https://systemdataportal.nationalgrid.com/arcgis/rest/services/NYSDP"
NY_LAYERS = {
    # file name             : (service,                     layer)
    "hosting_feeders"       : ("Hosting_Capacity_Data",     0),  # room for new solar
    "hosting_substations"   : ("Hosting_Capacity_Data",     1),
    "load_capacity"         : ("EV_Load_Serving_Capacity",  0),  # room for new users (EV chargers etc.)
}
PAGE_SIZE = 1000  # the server allows max 2000 per request; we stay below it


def fetch_all_rows(layer_id, base_url=BASE_URL):
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
        resp = requests.get(f"{base_url}/{layer_id}/query", params=params, timeout=60)
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

# ---------- 1b. New York: same idea, saved in its own subfolder ----------
ny_dir = out_dir / "ny"
ny_dir.mkdir(exist_ok=True)
for file_name, (service, layer) in NY_LAYERS.items():
    rows = fetch_all_rows(layer, base_url=f"{NY_URL}/{service}/MapServer")
    (ny_dir / f"{file_name}.json").write_text(json.dumps(rows, indent=2))
    print(f"  NY {file_name}: {len(rows)} rows saved")
print(f"Saved New York snapshot to {ny_dir}\n")

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