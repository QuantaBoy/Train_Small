"""Level 2: raw Kaggle CSVs (data/raw/*.csv) -> one clean `runs` table in data/trackpulse.db.

One row = one train, one run (date it left origin), one station it actually reported at.
Times are stored as full "YYYY-MM-DD HH:MM" so runs past midnight need no special care.

    python etl/clean.py
"""
import csv
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "trackpulse.db"
# Passenger trains stopped 2020-03-22 and restarted 2021-11-18. The few weeks before the
# stop ran a pre-COVID timetable, so only the post-restart era is kept.
START = "2021-11-18"

# Not dropped on re-run: since Level 10 the live feed also writes runs here (after the Kaggle snapshot).
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    train_no   TEXT NOT NULL,
    date       TEXT NOT NULL,      -- day the run left its origin
    seq        INTEGER NOT NULL,   -- order along the route
    station    TEXT NOT NULL,
    halts      INTEGER NOT NULL,   -- 1 scheduled halt, 0 pass-through
    km         REAL,               -- distance from origin
    lat        REAL,
    lng        REAL,
    sch_arr    TEXT, sch_dep TEXT, -- scheduled, "YYYY-MM-DD HH:MM"
    act_arr    TEXT, act_dep TEXT, -- actual
    delay_arr  REAL, delay_dep REAL, -- minutes late; negative = early
    PRIMARY KEY (train_no, date, seq)
);
CREATE INDEX IF NOT EXISTS runs_by_station ON runs(train_no, station, date);
"""


def stamp(hhmm: str, day: str) -> str | None:
    """'21:15', '01 Jan 2020' -> '2020-01-01 21:15'."""
    if not hhmm or not day:
        return None
    return datetime.strptime(f"{day} {hhmm}", "%d %b %Y %H:%M").strftime("%Y-%m-%d %H:%M")


def late(act: str | None, sch: str | None) -> float | None:
    """Signed minutes act - sch. The CSV's own delay column floors early running at 0;
    the timestamps don't, so early arrivals (recovered time) stay visible."""
    if not act or not sch:
        return None
    fmt = "%Y-%m-%d %H:%M"
    return (datetime.strptime(act, fmt) - datetime.strptime(sch, fmt)).total_seconds() / 60


def rows(path: Path):
    for r in csv.DictReader(path.open(newline="")):
        # A blank seq is the source's "last known position" marker, one per run, not a stop.
        if r["reported"] != "1" or r["date"] < START or not r["seq"]:
            continue
        sa, sd = stamp(r["sch_arrival"], r["sch_arrival_date"]), stamp(r["sch_departure"], r["sch_departure_date"])
        aa, ad = stamp(r["act_arrival"], r["act_arrival_date"]), stamp(r["act_departure"], r["act_departure_date"])
        yield (r["train_no"], r["date"], int(r["seq"]), r["station_code"], int(r["halts_here"]),
               float(r["distance_km"] or 0), float(r["lat"] or 0) or None, float(r["lng"] or 0) or None,
               sa, sd, aa, ad, late(aa, sa), late(ad, sd))


def main():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    for f in sorted(RAW.glob("*.csv")):
        con.executemany("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows(f))
    con.commit()

    # Self-check: every train loaded, nothing before START, origin rows have no arrival.
    trains = {p.stem for p in RAW.glob("*.csv")}
    assert {t for (t,) in con.execute("SELECT DISTINCT train_no FROM runs")} == trains
    assert con.execute("SELECT MIN(date) FROM runs").fetchone()[0] >= START
    assert stamp("21:15", "01 Jan 2020") == "2020-01-01 21:15" and late("2020-01-02 00:10", "2020-01-01 23:50") == 20
    for t, runs, n, early, avg in con.execute(
            "SELECT train_no, COUNT(DISTINCT date), COUNT(*), AVG(delay_arr < 0), AVG(delay_arr) "
            "FROM runs GROUP BY train_no"):
        print(f"{t}: {runs} runs, {n} station rows, early at {early:.0%} of stops, avg arrival delay {avg:+.1f} min")
    con.close()


if __name__ == "__main__":
    main()
