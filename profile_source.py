import json
import requests

# The "address" of National Grid's public map service
BASE_URL = (
    "https://systemdataportal.nationalgrid.com/arcgis/rest/services/"
    "MASDP/MA_HostingCapacity_with_DGPending/MapServer"
)

# The service has 3 layers (think: 3 tables)
LAYERS = {
    0: "Feeder Level Data 3 Phase",
    1: "Substation Level Data 3 Phase",
    2: "1 and 2 Phase",
}


def ask(layer_id, extra_params):
    """Send one question to a layer and return the JSON answer."""
    params = {"where": "1=1", "f": "json", **extra_params}  # 1=1 means "give me everything"
    response = requests.get(f"{BASE_URL}/{layer_id}/query", params=params, timeout=30) # query command is required by ArcGIS map server
    response.raise_for_status()
    return response.json()


for layer_id, layer_name in LAYERS.items():
    print("=" * 70)
    print(f"LAYER {layer_id}: {layer_name}")
    print("=" * 70)

    # Question 1: how many rows are in this table?
    count = ask(layer_id, {"returnCountOnly": "true"})
    if "error" in count:
        print("Server said:", count["error"])
        continue
    print(f"Total rows: {count.get('count')}")

    # Question 2: show me 3 rows, all columns, no map shapes
    sample = ask(layer_id, {
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": 3,
    })
    if "error" in sample:
        print("Server said:", sample["error"])
        continue

    print("\nColumns (name -> type):")
    for field in sample.get("fields", []):
        print(f"  {field['name']:<30} {field['type'].replace('esriFieldType', '')}") # esriFieldType is the type that NG's server sends back when asked to send data type !

    print("\nFirst 3 rows:")
    for row in sample.get("features", []): # features : in ArcGIS map language it is data rows
        print(json.dumps(row["attributes"], indent=2, default=str)) # attribute is a container that has all info like feeder id, voltage, capacity numbers.
    print()