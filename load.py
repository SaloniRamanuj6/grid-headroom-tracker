import sys
from pathlib import Path

import duckdb

RAW_DIR = Path("data/raw")
DB_PATH = "data/grid.duckdb"

# Which snapshot to load? Default = newest folder
day = sys.argv[1] if len(sys.argv) > 1 else sorted(p.name for p in RAW_DIR.iterdir())[-1]
feeder_file = (RAW_DIR / day / "layer_0.json").as_posix()
sub_file = (RAW_DIR / day / "layer_1.json").as_posix()
print(f"Loading snapshot {day}")

con = duckdb.connect(DB_PATH)

# ---------- Create tables once (IF NOT EXISTS = safe to rerun) ----------
con.execute("""
CREATE TABLE IF NOT EXISTS feeder_snapshot (
    snapshot_date          DATE,
    master_cdf             VARCHAR,
    substation             VARCHAR,
    voltage_kv             DOUBLE,
    available_mw           DOUBLE,
    available_after_pending_mw DOUBLE,
    connected_mw           DOUBLE,
    pending_mw             DOUBLE,
    feeder_limit_mw        DOUBLE,   -- we calculate: available + connected
    pct_full               DOUBLE,   -- we calculate: (connected + pending) / limit
    connected_solar_mw     DOUBLE,
    connected_storage_mw   DOUBLE,
    pending_solar_mw       DOUBLE,
    pending_storage_mw     DOUBLE,
    cip_fee                VARCHAR,
    ongoing_aso            VARCHAR,
    is_ferc_feeder         BOOLEAN,
    source_updated_date    DATE,     -- the date National Grid stamps on the data
    PRIMARY KEY (snapshot_date, master_cdf)
)
""")

con.execute("""
CREATE TABLE IF NOT EXISTS substation_bank_snapshot (
    snapshot_date          DATE,
    substation             VARCHAR,
    installed_dg_mw        DOUBLE,
    pending_dg_mw          DOUBLE,
    protection_3v0         VARCHAR,
    connected_solar_mw     DOUBLE,
    connected_storage_mw   DOUBLE,
    pending_solar_mw       DOUBLE,
    pending_storage_mw     DOUBLE,
    source_updated_date    DATE
)
""")

# ---------- Idempotent load: wipe this day first, then insert ----------
# Rerunning the same day gives the same result instead of duplicates.
con.execute("DELETE FROM feeder_snapshot WHERE snapshot_date = ?", [day])
con.execute("DELETE FROM substation_bank_snapshot WHERE snapshot_date = ?", [day])

con.execute(f"""
INSERT INTO feeder_snapshot
SELECT
    DATE '{day}'                                         AS snapshot_date,
    Master_CDF                                           AS master_cdf,
    Substation                                           AS substation,
    Operating_Voltage_kV                                 AS voltage_kv,
    HC_Available_MW                                      AS available_mw,
    HC_Available_MW_pending                              AS available_after_pending_mw,
    Total_Nameplate_Interconnected_DG_excluding_BESS_MW  AS connected_mw,
    Total_Nameplate_Pending_excluding_BESS_MW            AS pending_mw,
    HC_Available_MW + connected_mw                       AS feeder_limit_mw,
    CASE WHEN feeder_limit_mw > 0
         THEN (connected_mw + pending_mw) / feeder_limit_mw END AS pct_full,
    feeder_connected_solar, feeder_connected_storage,
    feeder_pending_solar,   feeder_pending_storage,
    NULLIF(CIP_FEE, 'Data Unavailable'),
    NULLIF(Ongoing_ASO, 'Data Unavailable'),
    FERC_FEEDER = 'Yes',
    CAST(epoch_ms(Updated_Date) AS DATE)      AS source_updated_date
FROM read_json_auto('{feeder_file}')
""")

con.execute(f"""
INSERT INTO substation_bank_snapshot
SELECT DISTINCT                                  -- DISTINCT removes the 551 repeats
    DATE '{day}', Substation,
    Sub_Bank_Installed_DG_MW, Sub_Bank_Pending_DG_MW,
    NULLIF(Protection_3V0, 'Data Unavailable'),
    bank_connected_solar, bank_connected_storage,
    bank_pending_solar,   bank_pending_storage,
    CAST(epoch_ms(Updated_Date) AS DATE)
FROM read_json_auto('{sub_file}')
""")


# ======================================================================
# NEW YORK (only if this snapshot folder has an "ny" subfolder)
# ======================================================================
ny_dir = RAW_DIR / day / "ny"

con.execute("""
CREATE TABLE IF NOT EXISTS ny_feeder_hosting_snapshot (
    snapshot_date              DATE,
    master_cdf                 VARCHAR,
    substation_bank            VARCHAR,
    nyiso_load_zone            VARCHAR,
    voltage_kv                 DOUBLE,
    max_hc_mw                  DOUBLE,   -- room for new solar at the BEST spot on the feeder
    min_hc_mw                  DOUBLE,   -- room at the WORST spot on the feeder
    installed_dg_mw            DOUBLE,   -- already connected
    queued_dg_mw               DOUBLE,   -- waiting in line
    connected_since_refresh_mw DOUBLE,   -- connected after the last analysis
    backfeed_protection        BOOLEAN,
    notes                      VARCHAR,
    hca_refresh_date           DATE,     -- when the capacity analysis was run
    dg_refresh_date            DATE,     -- when the project numbers were updated
    PRIMARY KEY (snapshot_date, master_cdf)
)
""")

con.execute("""
CREATE TABLE IF NOT EXISTS ny_feeder_load_snapshot (
    snapshot_date              DATE,
    master_cdf                 VARCHAR,
    substation_bank            VARCHAR,
    voltage_kv                 DOUBLE,
    peak_load_mw               DOUBLE,   -- busiest moment of demand
    feeder_rating_mw           DOUBLE,   -- what the feeder can safely carry
    bank_rating_mw             DOUBLE,   -- what the substation bank can carry
    load_headroom_mw           DOUBLE,   -- room for new users (EV chargers, heat pumps...)
    pct_of_rating_used         DOUBLE,   -- we calculate: peak / smaller of the two ratings
    PRIMARY KEY (snapshot_date, master_cdf)
)
""")

con.execute("DELETE FROM ny_feeder_hosting_snapshot WHERE snapshot_date = ?", [day])
con.execute("DELETE FROM ny_feeder_load_snapshot WHERE snapshot_date = ?", [day])

if ny_dir.exists():
    ny_hosting_file = (ny_dir / "hosting_feeders.json").as_posix()
    ny_load_file = (ny_dir / "load_capacity.json").as_posix()

    con.execute(f"""
    INSERT INTO ny_feeder_hosting_snapshot
    SELECT
        DATE '{day}', Master_CDF, substation_bank_name, nyiso_load_zone, feeder_voltage,
        feeder_max_hc, feeder_min_hc, feeder_installed_dg, feeder_queued_dg,
        feeder_dg_connected_since_refresh,
        substation_backfeed_protection = 'Yes',
        NULLIF(TRIM(notes), ''),                        -- empty text -> real NULL
        CAST(epoch_ms(hca_refresh_date) AS DATE),
        CAST(epoch_ms(feeder_dg_refresh_date) AS DATE)
    FROM read_json_auto('{ny_hosting_file}')
    """)

    con.execute(f"""
    INSERT INTO ny_feeder_load_snapshot
    SELECT
        DATE '{day}', Master_CDF, substation_bank_name, feeder_voltage,
        feeder_peak_load, feeder_rating, substation_bank_rating, load_capacity_headroom,
        feeder_peak_load / NULLIF(LEAST(feeder_rating, substation_bank_rating), 0)
    FROM read_json_auto('{ny_load_file}')
    """)
else:
    print("(No New York data in this snapshot, skipping NY)")

# ---------- Quick look at what we built ----------
print("\nRows loaded:")
print(con.sql(f"""
    SELECT 'feeder_snapshot' AS tbl, COUNT(*) AS n FROM feeder_snapshot WHERE snapshot_date = '{day}'
    UNION ALL
    SELECT 'substation_bank_snapshot', COUNT(*) FROM substation_bank_snapshot WHERE snapshot_date = '{day}'
"""))

print("Feeders by how full they are:")
print(con.sql(f"""
    SELECT CASE WHEN pct_full >= 0.9 THEN '1) 90%+ full'
                WHEN pct_full >= 0.5 THEN '2) 50-90% full'
                ELSE '3) under 50% full' END AS bucket,
           COUNT(*) AS feeders
    FROM feeder_snapshot WHERE snapshot_date = '{day}'
    GROUP BY 1 ORDER BY 1
"""))

print("Top 10 substations with the most 90%+ full feeders:")
print(con.sql(f"""
    SELECT substation,
           COUNT(*)                                   AS feeders,
           COUNT(*) FILTER (WHERE pct_full >= 0.9)    AS nearly_full,
           ROUND(SUM(available_after_pending_mw), 2)  AS mw_left
    FROM feeder_snapshot WHERE snapshot_date = '{day}'
    GROUP BY substation
    ORDER BY nearly_full DESC, mw_left, substation   -- substation = tie-breaker
    LIMIT 10
"""))

if ny_dir.exists():
    print("NEW YORK rows loaded:")
    print(con.sql(f"""
        SELECT 'ny_feeder_hosting_snapshot' AS tbl, COUNT(*) AS n FROM ny_feeder_hosting_snapshot WHERE snapshot_date = '{day}'
        UNION ALL
        SELECT 'ny_feeder_load_snapshot', COUNT(*) FROM ny_feeder_load_snapshot WHERE snapshot_date = '{day}'
    """))

    print("NEW YORK headline numbers:")
    print(con.sql(f"""
        SELECT
            (SELECT COUNT(*) FILTER (WHERE max_hc_mw <= 0)           FROM ny_feeder_hosting_snapshot WHERE snapshot_date = '{day}') AS no_room_for_solar,
            (SELECT COUNT(*) FILTER (WHERE queued_dg_mw > max_hc_mw) FROM ny_feeder_hosting_snapshot WHERE snapshot_date = '{day}') AS line_bigger_than_room,
            (SELECT COUNT(*) FILTER (WHERE pct_of_rating_used > 1)   FROM ny_feeder_load_snapshot    WHERE snapshot_date = '{day}') AS peak_above_rating,
            (SELECT COUNT(*) FILTER (WHERE load_headroom_mw < 0.5)   FROM ny_feeder_load_snapshot    WHERE snapshot_date = '{day}') AS under_half_mw_load_room
    """))

con.close()