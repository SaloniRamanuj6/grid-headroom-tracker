import json
from pathlib import Path

import duckdb

con = duckdb.connect("data/grid.duckdb", read_only=True)


def rows(sql):
    """Run a query and return a list of dicts (easy to turn into JSON)."""
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def one(sql):
    return con.execute(sql).fetchone()[0]


def pct(a, b):
    return f"{round(a / b * 100)}% of feeders" if b else ""


# Column types tell the page how to show each value:
# text, int, mw (2 decimals), kv (1 decimal), pct (red dot above 100%), flag (red dot if not empty)
def col(header, key, kind="mw"):
    return {"h": header, "k": key, "t": kind}


# ======================================================================
# MASSACHUSETTS
# ======================================================================
ma_day = one("SELECT MAX(snapshot_date) FROM feeder_snapshot")
T = f"feeder_snapshot WHERE snapshot_date = '{ma_day}'"

k = rows(f"""
    SELECT COUNT(*)                                AS feeders,
           COUNT(*) FILTER (WHERE pct_full >= 0.9) AS nearly_full,
           COUNT(*) FILTER (WHERE pct_full > 1)    AS oversubscribed,
           COUNT(DISTINCT substation)              AS substations,
           MAX(source_updated_date)                AS source_date
    FROM {T}""")[0]
ma_zero_subs = one(f"""SELECT COUNT(*) FROM (SELECT substation FROM {T}
                       GROUP BY 1 HAVING SUM(available_after_pending_mw) <= 0)""")
ma_age = (ma_day - k["source_date"]).days

ma = {
    "name": "Massachusetts",
    "intro": "How much room is left for new solar, wind and storage on National Grid's Massachusetts distribution feeders.",
    "source_link": "https://systemdataportal.nationalgrid.com/MA/",
    "banner": {
        "level": "warning" if ma_age > 45 else "ok",
        "title": "Warning: source data may be stale" if ma_age > 45 else "Source data is recent",
        "text": f"National Grid's date stamp is {k['source_date']}, {ma_age} days before this snapshot ({ma_day}). "
                "Figures reflect that analysis, not necessarily today's grid.",
    },
    "kpis": [
        [k["feeders"], "feeders tracked", f"{k['substations']} substations"],
        [k["nearly_full"], "feeders 90%+ full", pct(k["nearly_full"], k["feeders"])],
        [k["oversubscribed"], "feeders oversubscribed", pct(k["oversubscribed"], k["feeders"])],
        [ma_zero_subs, "substations with 0 MW left", "after pending projects"],
    ],
    "charts": [{
        "title": "Feeders by how full they are",
        "note": "% full = (connected + pending MW) ÷ feeder limit, where limit = available + connected MW. "
                "Over 100% means more projects are waiting than the feeder has room for.",
        "bars": rows(f"""
            SELECT CASE WHEN pct_full > 1    THEN '4|Over 100% (oversubscribed)'
                        WHEN pct_full >= 0.9 THEN '3|90 to 100%'
                        WHEN pct_full >= 0.5 THEN '2|50 to 90%'
                        ELSE '1|Under 50%' END AS label,
                   COUNT(*) AS feeders
            FROM {T} GROUP BY 1 ORDER BY 1"""),
    }],
    "tables": [
        {
            "title": "Substations, least room first",
            "note": "MW left counts pending projects as already taking up room.",
            "search": ["substation"], "placeholder": "Search substation", "noun": "substations",
            "columns": [col("Substation", "substation", "text"), col("Feeders", "feeders", "int"),
                        col("90%+ full", "nearly_full", "int"), col("MW left", "mw_left"),
                        col("Flag", "flag", "flag")],
            "rows": rows(f"""
                SELECT substation, COUNT(*) AS feeders,
                       COUNT(*) FILTER (WHERE pct_full >= 0.9) AS nearly_full,
                       ROUND(GREATEST(SUM(available_after_pending_mw), 0), 2) AS mw_left,
                       CASE WHEN COUNT(*) FILTER (WHERE pct_full > 1) > 0
                            THEN COUNT(*) FILTER (WHERE pct_full > 1) || ' oversubscribed' END AS flag
                FROM {T} GROUP BY substation
                ORDER BY mw_left, nearly_full DESC, substation"""),
        },
        {
            "title": "All feeders, fullest first",
            "search": ["master_cdf", "substation"], "placeholder": "Search feeder ID or substation",
            "noun": "feeders",
            "columns": [col("Feeder", "master_cdf", "text"), col("Substation", "substation", "text"),
                        col("kV", "voltage_kv", "kv"), col("Limit MW", "limit_mw"),
                        col("Connected", "connected_mw"), col("Pending", "pending_mw"),
                        col("MW left", "left_mw"), col("% full", "pct_full", "pct")],
            "rows": rows(f"""
                SELECT master_cdf, substation, voltage_kv,
                       ROUND(feeder_limit_mw, 3) AS limit_mw, ROUND(connected_mw, 3) AS connected_mw,
                       ROUND(pending_mw, 3) AS pending_mw, ROUND(available_after_pending_mw, 3) AS left_mw,
                       ROUND(pct_full * 100, 1) AS pct_full
                FROM {T} ORDER BY pct_full DESC NULLS LAST, master_cdf"""),
        },
    ],
    "footnote": "Generation hosting capacity only (room for new solar, wind and storage), three-phase feeders, battery storage excluded.",
}

# ======================================================================
# NEW YORK
# ======================================================================
states = {"MA": ma}
ny_day = one("SELECT MAX(snapshot_date) FROM ny_feeder_hosting_snapshot")

if ny_day:
    H = f"ny_feeder_hosting_snapshot WHERE snapshot_date = '{ny_day}'"
    L = f"ny_feeder_load_snapshot WHERE snapshot_date = '{ny_day}'"

    nk = rows(f"""
        SELECT COUNT(*)                                       AS feeders,
               COUNT(DISTINCT substation_bank)                AS banks,
               COUNT(*) FILTER (WHERE max_hc_mw <= 0)         AS no_room,
               COUNT(*) FILTER (WHERE queued_dg_mw > max_hc_mw) AS line_bigger,
               MODE(hca_refresh_date)                         AS hca_date,   -- the date most feeders share
               MAX(dg_refresh_date)                           AS dg_date
        FROM {H}""")[0]
    nk["hca_rerun"] = one(f"SELECT COUNT(*) FROM {H} AND hca_refresh_date > '{nk['hca_date']}'")
    ny_peak = one(f"SELECT COUNT(*) FROM {L} AND pct_of_rating_used > 1")
    ny_load_feeders = one(f"SELECT COUNT(*) FROM {L}")
    dg_age = (ny_day - nk["dg_date"]).days

    # One row per feeder: solar room + load room side by side (joined on feeder ID)
    feeder_sql = f"""
        SELECT h.master_cdf, h.substation_bank, h.nyiso_load_zone AS zone, h.voltage_kv,
               h.max_hc_mw, h.queued_dg_mw, l.load_headroom_mw,
               ROUND(l.pct_of_rating_used * 100, 1) AS pct_rating,
               CASE WHEN h.notes ILIKE '%Capital Investment Project%' THEN 'Closed to >50 kW until upgrade'
                    WHEN h.notes ILIKE '%Upstream Constraints%'        THEN 'Possible upstream constraints'
                    WHEN h.notes IS NOT NULL                           THEN 'See utility note' END AS flag
        FROM ny_feeder_hosting_snapshot h
        LEFT JOIN ny_feeder_load_snapshot l
               ON l.master_cdf = h.master_cdf AND l.snapshot_date = h.snapshot_date
        WHERE h.snapshot_date = '{ny_day}'"""

    states["NY"] = {
        "name": "New York",
        "intro": "Room for new solar (hosting capacity) and for new demand like EV chargers and heat pumps "
                 "(load capacity) on National Grid's New York distribution feeders.",
        "source_link": "https://systemdataportal.nationalgrid.com/NY/",
        "banner": {
            "level": "warning" if dg_age > 45 else "ok",
            "title": "Warning: project data may be stale" if dg_age > 45 else "Project data is recent",
            "text": f"Capacity analysis run {nk['hca_date']} for most feeders"
                    + (f" ({nk['hca_rerun']} re-run since)" if nk["hca_rerun"] else "")
                    + f". Connected and queued project data updated "
                    f"{nk['dg_date']}, {dg_age} days before this snapshot ({ny_day}).",
        },
        "kpis": [
            [nk["feeders"], "feeders tracked", f"{nk['banks']} substation banks"],
            [nk["no_room"], "feeders with no room for new solar", pct(nk["no_room"], nk["feeders"])],
            [nk["line_bigger"], "feeders where queued solar exceeds room", pct(nk["line_bigger"], nk["feeders"])],
            [ny_peak, "feeders peaking above their rating", pct(ny_peak, ny_load_feeders)],
        ],
        "charts": [
            {
                "title": "Room for new solar (best spot on each feeder)",
                "note": "Max hosting capacity from National Grid's latest analysis.",
                "bars": rows(f"""
                    SELECT CASE WHEN max_hc_mw <= 0 THEN '1|0 MW (no room)'
                                WHEN max_hc_mw < 1  THEN '2|Under 1 MW'
                                WHEN max_hc_mw < 5  THEN '3|1 to 5 MW'
                                ELSE '4|5 MW or more' END AS label,
                           COUNT(*) AS feeders
                    FROM {H} GROUP BY 1 ORDER BY 1"""),
            },
            {
                "title": "How much of its rating each feeder uses at peak demand",
                "note": "Peak load ÷ the smaller of the feeder rating and the substation bank rating.",
                "bars": rows(f"""
                    SELECT CASE WHEN pct_of_rating_used > 1    THEN '4|Over 100%'
                                WHEN pct_of_rating_used >= 0.8 THEN '3|80 to 100%'
                                WHEN pct_of_rating_used >= 0.5 THEN '2|50 to 80%'
                                ELSE '1|Under 50%' END AS label,
                           COUNT(*) AS feeders
                    FROM {L} GROUP BY 1 ORDER BY 1"""),
            },
        ],
        "tables": [
            {
                "title": "Substation banks, most constrained first",
                "note": "Sorted by feeders with no room for new solar, then by solar waiting in line.",
                "search": ["substation_bank"], "placeholder": "Search substation bank", "noun": "banks",
                "columns": [col("Substation bank", "substation_bank", "text"), col("Feeders", "feeders", "int"),
                            col("No solar room", "no_room", "int"), col("Queued MW", "queued_mw"),
                            col("Lowest load room MW", "min_load_mw"), col("Flag", "flag", "flag")],
                "rows": rows(f"""
                    SELECT substation_bank, COUNT(*) AS feeders,
                           COUNT(*) FILTER (WHERE max_hc_mw <= 0) AS no_room,
                           ROUND(SUM(queued_dg_mw), 2) AS queued_mw,
                           ROUND(MIN(load_headroom_mw), 2) AS min_load_mw,
                           CASE WHEN COUNT(*) FILTER (WHERE flag = 'Closed to >50 kW until upgrade') > 0
                                THEN 'Closed to >50 kW until upgrade' END AS flag
                    FROM ({feeder_sql})
                    GROUP BY substation_bank
                    ORDER BY no_room DESC, queued_mw DESC, substation_bank"""),
            },
            {
                "title": "All feeders, least solar room first",
                "search": ["master_cdf", "substation_bank", "zone"],
                "placeholder": "Search feeder, bank or NYISO zone", "noun": "feeders",
                "columns": [col("Feeder", "master_cdf", "text"), col("Substation bank", "substation_bank", "text"),
                            col("Zone", "zone", "text"), col("kV", "voltage_kv", "kv"),
                            col("Solar room MW", "max_hc_mw"), col("Queued MW", "queued_dg_mw"),
                            col("Load room MW", "load_headroom_mw"), col("% rating at peak", "pct_rating", "pct"),
                            col("Note", "flag", "flag")],
                "rows": rows(feeder_sql + " ORDER BY h.max_hc_mw, h.queued_dg_mw DESC, h.master_cdf"),
            },
        ],
        "footnote": "Solar room = max hosting capacity at the best location on the feeder. "
                    "Load room = load capacity headroom from the EV Load Serving Capacity layer.",
    }

con.close()

# ---------- Pour the data into the HTML template ----------
template = Path("dashboard_template.html").read_text(encoding="utf-8")
html = template.replace("__DATA__", json.dumps(states, default=str))
Path("docs").mkdir(exist_ok=True)
Path("docs/index.html").write_text(html, encoding="utf-8")
print(f"Dashboard built: {', '.join(states)} (MA snapshot {ma_day}, NY snapshot {ny_day})")