import sys
from datetime import datetime

import duckdb

con = duckdb.connect("data/grid.duckdb")

# Newest snapshot we have, and the one before it (if any)
days = [r[0] for r in con.execute(
    "SELECT DISTINCT snapshot_date FROM feeder_snapshot ORDER BY 1 DESC").fetchall()]
today, previous = days[0], (days[1] if len(days) > 1 else None)


def one_number(sql):
    """Run a query that returns a single number."""
    return con.execute(sql).fetchone()[0]


checks = []  # each item: (check name, severity, passed?, detail)
T = f"feeder_snapshot WHERE snapshot_date = '{today}'"

# 1. Every feeder ID shows up exactly once, and none are empty
dupes = one_number(f"SELECT COUNT(*) - COUNT(DISTINCT master_cdf) FROM {T}")
missing = one_number(f"SELECT COUNT(*) FROM {T} AND master_cdf IS NULL")
checks.append(("unique_feeder_id", "FAIL", dupes == 0 and missing == 0,
               f"{dupes} duplicates, {missing} missing"))

# 2. MW numbers can never be negative
negatives = one_number(f"""SELECT COUNT(*) FROM {T}
    AND (available_mw < 0 OR connected_mw < 0 OR pending_mw < 0)""")
checks.append(("no_negative_mw", "FAIL", negatives == 0, f"{negatives} rows with negative MW"))

# 3. Row count shouldn't suddenly drop (a half-broken download looks like this)
rows_now = one_number(f"SELECT COUNT(*) FROM {T}")
if previous:
    rows_before = one_number(
        f"SELECT COUNT(*) FROM feeder_snapshot WHERE snapshot_date = '{previous}'")
    change = (rows_now - rows_before) / rows_before
    checks.append(("row_count_stable", "FAIL", change > -0.10,
                   f"{rows_before} -> {rows_now} rows ({change:+.1%}) vs {previous}"))
else:
    checks.append(("row_count_stable", "INFO", True,
                   f"{rows_now} rows; first snapshot, nothing to compare yet"))

# 4. Freshness: how old is National Grid's own date stamp?
newest_source = one_number(f"SELECT MAX(source_updated_date) FROM {T}")
age_days = (today - newest_source).days
checks.append(("source_freshness", "WARN", age_days <= 45,
               f"source date {newest_source} is {age_days} days old (limit 45)"))

# 5. Oversubscribed feeders: more pending projects than room left
over = one_number(f"SELECT COUNT(*) FROM {T} AND pct_full > 1")
checks.append(("oversubscribed_feeders", "INFO", True,
               f"{over} feeders have more pending MW than available MW"))


# ======================================================================
# NEW YORK checks (same ideas, NY's own newest snapshot)
# ======================================================================
ny_days = [r[0] for r in con.execute(
    "SELECT DISTINCT snapshot_date FROM ny_feeder_hosting_snapshot ORDER BY 1 DESC").fetchall()]

if not ny_days:
    checks.append(("ny_data_present", "INFO", True, "no New York snapshot loaded yet"))
else:
    ny_today = ny_days[0]
    ny_prev = ny_days[1] if len(ny_days) > 1 else None
    NY = f"ny_feeder_hosting_snapshot WHERE snapshot_date = '{ny_today}'"
    NYL = f"ny_feeder_load_snapshot WHERE snapshot_date = '{ny_today}'"

    # 6. NY feeder IDs unique and present (hosting and load tables)
    d1 = one_number(f"SELECT COUNT(*) - COUNT(DISTINCT master_cdf) FROM {NY}")
    d2 = one_number(f"SELECT COUNT(*) - COUNT(DISTINCT master_cdf) FROM {NYL}")
    m1 = one_number(f"SELECT COUNT(*) FROM {NY} AND master_cdf IS NULL")
    checks.append(("ny_unique_feeder_id", "FAIL", d1 == 0 and d2 == 0 and m1 == 0,
                   f"hosting: {d1} duplicates, {m1} missing; load: {d2} duplicates"))

    # 7. Capacity and ratings can never be negative
    neg = one_number(f"SELECT COUNT(*) FROM {NY} AND (max_hc_mw < 0 OR min_hc_mw < 0 OR queued_dg_mw < 0)")
    neg += one_number(f"SELECT COUNT(*) FROM {NYL} AND (peak_load_mw < 0 OR feeder_rating_mw < 0 OR load_headroom_mw < 0)")
    checks.append(("ny_no_negative_mw", "FAIL", neg == 0, f"{neg} rows with negative MW"))

    # 8. Row count shouldn't suddenly drop
    ny_rows = one_number(f"SELECT COUNT(*) FROM {NY}")
    if ny_prev:
        before = one_number(f"SELECT COUNT(*) FROM ny_feeder_hosting_snapshot WHERE snapshot_date = '{ny_prev}'")
        ch = (ny_rows - before) / before
        checks.append(("ny_row_count_stable", "FAIL", ch > -0.10,
                       f"{before} -> {ny_rows} rows ({ch:+.1%}) vs {ny_prev}"))
    else:
        checks.append(("ny_row_count_stable", "INFO", True,
                       f"{ny_rows} rows; first NY snapshot, nothing to compare yet"))

    # 9. Freshness of the PROJECT numbers (NY gives us a separate date for these)
    dg_date = one_number(f"SELECT MAX(dg_refresh_date) FROM {NY}")
    dg_age = (ny_today - dg_date).days
    checks.append(("ny_dg_freshness", "WARN", dg_age <= 45,
                   f"project data date {dg_date} is {dg_age} days old (limit 45)"))

    # 10. "Connected since refresh" can't really be negative -> source data oddity
    odd = one_number(f"SELECT COUNT(*) FROM {NY} AND connected_since_refresh_mw < 0")
    checks.append(("ny_negative_since_refresh", "WARN", odd == 0,
                   f"{odd} feeders show negative MW connected since the last analysis"))

    # 11. Facts worth knowing
    peak = one_number(f"SELECT COUNT(*) FROM {NYL} AND pct_of_rating_used > 1")
    checks.append(("ny_peak_above_rating", "INFO", True,
                   f"{peak} feeders peak above their rating"))

# ---------- Print the report ----------
print(f"Data quality report for snapshot {today}\n")
failed = False
for name, severity, passed, detail in checks:
    status = "INFO" if severity == "INFO" else ("PASS" if passed else severity)
    if not passed and severity == "FAIL":
        failed = True
    print(f"  [{status:<4}] {name:<27} {detail}")

# ---------- Save results so we can see quality over time ----------
con.execute("""CREATE TABLE IF NOT EXISTS dq_results (
    snapshot_date DATE, check_name VARCHAR, severity VARCHAR,
    passed BOOLEAN, detail VARCHAR, checked_at TIMESTAMP)""")
con.execute("DELETE FROM dq_results WHERE snapshot_date = ?", [today])
for name, severity, passed, detail in checks:
    con.execute("INSERT INTO dq_results VALUES (?, ?, ?, ?, ?, ?)",
                [today, name, severity, passed, detail, datetime.now()])
con.close()

print("\nRESULT:", "FAILED - stop the pipeline" if failed else "OK to continue")
sys.exit(1 if failed else 0)