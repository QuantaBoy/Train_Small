"""Level 3: `runs` -> `sections` (every hop of every run) + `section_stats` (per train, per hop).

A section is the stretch between two consecutive *reported* stations of one run, A -> B.
    gain = delay arriving at B - delay leaving A
gain > 0: the train lost time on that stretch. gain < 0: it recovered time.
The halt itself is measured too:
    dwell_gain = delay leaving B - delay arriving at B   (long or short stop at B)

    python etl/sections.py
"""
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, quantiles

DB = Path(__file__).parent.parent / "data" / "trackpulse.db"
# Delays outside this window are diversions or bad reports (157 rows in Level 2); they'd
# swamp the averages, so those stops are left out of the sections.
SANE = (-120, 600)
MIN_RUNS = 20  # fewer runs than this over a hop -> no stats, too thin to trust

SCHEMA = """
DROP TABLE IF EXISTS sections;
CREATE TABLE sections (
    train_no TEXT, date TEXT, from_stn TEXT, to_stn TEXT, from_seq INTEGER, to_seq INTEGER,
    km REAL,          -- length of the hop
    sch_min REAL,     -- timetabled running time A dep -> B arr
    act_min REAL,     -- actual running time
    delay_in REAL,    -- delay leaving A
    delay_out REAL,   -- delay arriving at B
    gain REAL,        -- delay_out - delay_in
    dwell_gain REAL   -- delay change during the halt at B (NULL if B has no departure)
);
DROP TABLE IF EXISTS section_stats;
CREATE TABLE section_stats (
    train_no TEXT, from_stn TEXT, to_stn TEXT, from_seq INTEGER, to_seq INTEGER,
    km REAL, sch_min REAL, runs INTEGER,
    gain_mean REAL, gain_median REAL, gain_p10 REAL, gain_p90 REAL,
    recover_share REAL,   -- share of runs that made up time on this hop
    dwell_mean REAL,
    PRIMARY KEY (train_no, from_seq, to_seq)
);
"""


def minutes(a: str, b: str) -> float:
    """Minutes from 'YYYY-MM-DD HH:MM' a to b, done in SQL-free Python for clarity."""
    from datetime import datetime
    f = "%Y-%m-%d %H:%M"
    return (datetime.strptime(b, f) - datetime.strptime(a, f)).total_seconds() / 60


def hops(stops: list[sqlite3.Row]):
    """Consecutive reported stops of one run -> section rows."""
    lo, hi = SANE
    ok = [s for s in stops if all(d is None or lo <= d <= hi for d in (s["delay_arr"], s["delay_dep"]))]
    for a, b in zip(ok, ok[1:]):
        leave = a["delay_dep"] if a["delay_dep"] is not None else a["delay_arr"]
        reach = b["delay_arr"] if b["delay_arr"] is not None else b["delay_dep"]
        if leave is None or reach is None:
            continue
        a_sch, a_act = a["sch_dep"] or a["sch_arr"], a["act_dep"] or a["act_arr"]
        b_sch, b_act = b["sch_arr"] or b["sch_dep"], b["act_arr"] or b["act_dep"]
        dwell = (b["delay_dep"] - b["delay_arr"]) if None not in (b["delay_dep"], b["delay_arr"]) else None
        yield (a["train_no"], a["date"], a["station"], b["station"], a["seq"], b["seq"],
               round(b["km"] - a["km"], 3), minutes(a_sch, b_sch), minutes(a_act, b_act),
               leave, reach, reach - leave, dwell)


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)

    run, key = [], None
    def flush():
        con.executemany("INSERT INTO sections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", hops(run))
    for s in con.execute("SELECT * FROM runs ORDER BY train_no, date, seq"):
        if (s["train_no"], s["date"]) != key:
            flush()
            run, key = [], (s["train_no"], s["date"])
        run.append(s)
    flush()

    groups = defaultdict(list)
    for r in con.execute("SELECT * FROM sections"):
        groups[(r["train_no"], r["from_seq"], r["to_seq"])].append(r)
    stats = []
    for (t, fs, ts), rs in groups.items():
        if len(rs) < MIN_RUNS:
            continue
        g = [r["gain"] for r in rs]
        p = quantiles(g, n=10)
        dw = [r["dwell_gain"] for r in rs if r["dwell_gain"] is not None]
        stats.append((t, rs[0]["from_stn"], rs[0]["to_stn"], fs, ts, rs[0]["km"], median(r["sch_min"] for r in rs),
                      len(rs), mean(g), median(g), p[0], p[-1], sum(x < 0 for x in g) / len(g),
                      mean(dw) if dw else None))
    con.executemany("INSERT INTO section_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", stats)
    con.commit()

    # Self-check: gain is exactly the delay change, and per-run gains add up to end-to-end change.
    assert con.execute("SELECT COUNT(*) FROM sections WHERE ABS(gain - (delay_out - delay_in)) > 1e-9").fetchone()[0] == 0
    assert minutes("2024-01-01 23:50", "2024-01-02 00:20") == 30

    print(con.execute("SELECT COUNT(*), COUNT(DISTINCT train_no||date) FROM sections").fetchone()[:], "hops / runs")
    print("hops with stats:", len(stats))
    print("\nWhere 16021 Kaveri loses and recovers most (median min per run):")
    for r in con.execute("""SELECT from_stn, to_stn, km, sch_min, runs, gain_median, gain_mean, recover_share
                            FROM section_stats WHERE train_no='16021' ORDER BY from_seq"""):
        print(f"  {r[0]:>5} -> {r[1]:<5} {r[2]:6.1f} km  sch {r[3]:5.0f} min  n={r[4]:4}  "
              f"median {r[5]:+5.1f}  mean {r[6]:+5.1f}  recovers in {r[7]:.0%} of runs")
    con.close()


if __name__ == "__main__":
    main()
