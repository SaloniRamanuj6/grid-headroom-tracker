import json
from pathlib import Path

import duckdb

con = duckdb.connect("data/grid.duckdb", read_only=True)
day = con.execute("SELECT MAX(snapshot_date) FROM feeder_snapshot").fetchone()[0]
T = f"feeder_snapshot WHERE snapshot_date = '{day}'"


def rows(sql):
    """Run a query and return a list of dicts (easy to turn into JSON)."""
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------- Headline numbers ----------
k = rows(f"""
    SELECT COUNT(*)                                        AS feeders,
           COUNT(*) FILTER (WHERE pct_full >= 0.9)         AS nearly_full,
           COUNT(*) FILTER (WHERE pct_full > 1)            AS oversubscribed,
           MAX(source_updated_date)                        AS source_date
    FROM {T}""")[0]

zero_subs = con.execute(f"""
    SELECT COUNT(*) FROM (
        SELECT substation FROM {T}
        GROUP BY substation HAVING SUM(available_after_pending_mw) <= 0)""").fetchone()[0]

# ---------- Feeders by how full they are ----------
buckets = rows(f"""
    SELECT CASE WHEN pct_full > 1    THEN 4
                WHEN pct_full >= 0.9 THEN 3
                WHEN pct_full >= 0.5 THEN 2
                ELSE 1 END AS b,
           COUNT(*) AS feeders
    FROM {T} GROUP BY 1 ORDER BY 1""")
labels = {1: "Under 50%", 2: "50 to 90%", 3: "90 to 100%", 4: "Over 100% (oversubscribed)"}
buckets = [{"label": labels[b["b"]], "feeders": b["feeders"]} for b in buckets]

# ---------- Substations, tightest first ----------
substations = rows(f"""
    SELECT substation,
           COUNT(*)                                   AS feeders,
           COUNT(*) FILTER (WHERE pct_full >= 0.9)    AS nearly_full,
           COUNT(*) FILTER (WHERE pct_full > 1)       AS oversubscribed,
           ROUND(GREATEST(SUM(available_after_pending_mw), 0), 2) AS mw_left
    FROM {T}
    GROUP BY substation
    ORDER BY mw_left, nearly_full DESC, substation""")

# ---------- Every feeder, fullest first ----------
feeders = rows(f"""
    SELECT master_cdf, substation, voltage_kv,
           ROUND(feeder_limit_mw, 3)                  AS limit_mw,
           ROUND(connected_mw, 3)                     AS connected_mw,
           ROUND(pending_mw, 3)                       AS pending_mw,
           ROUND(available_after_pending_mw, 3)       AS left_mw,
           ROUND(pct_full * 100, 1)                   AS pct_full
    FROM {T}
    ORDER BY pct_full DESC NULLS LAST, master_cdf""")
con.close()

data = {
    "snapshot_date": str(day),
    "source_date": str(k["source_date"]),
    "source_age_days": (day - k["source_date"]).days,
    "kpis": {**{key: k[key] for key in ("feeders", "nearly_full", "oversubscribed")},
             "zero_subs": zero_subs, "substations": len(substations)},
    "buckets": buckets,
    "substations": substations,
    "feeders": feeders,
}

# ---------- Pour the data into the HTML template ----------
template = Path("dashboard_template.html").read_text(encoding="utf-8")
html = template.replace("__DATA__", json.dumps(data, default=str))
Path("docs").mkdir(exist_ok=True)
Path("docs/index.html").write_text(html, encoding="utf-8")
print(f"Dashboard built for snapshot {day}: docs/index.html")
