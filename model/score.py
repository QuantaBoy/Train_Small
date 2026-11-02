"""Level 10: the model checks itself.

Every live ETA the API hands out is logged in `predictions`. Once the train has actually
reached that station, the feed has written the real time into `runs`, and the two can be
compared. This is the honest, ongoing version of the Level 5 test.

    python model/score.py [days]    # print the last N days' live scores (default 7)
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path(__file__).parent.parent / "data" / "trackpulse.db"
IST = timezone(timedelta(hours=5, minutes=30))


def live_scores(con: sqlite3.Connection, days: int = 7) -> dict:
    since = (datetime.now(IST) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = con.execute("""
        SELECT p.train_no, p.pred_delay, p.delay_now, p.lo, p.hi, COALESCE(r.delay_arr, r.delay_dep) AS actual
        FROM predictions p JOIN runs r ON r.train_no = p.train_no AND r.date = p.date AND r.station = p.station
        WHERE p.made_at >= ? AND COALESCE(r.delay_arr, r.delay_dep) IS NOT NULL""", (since,)).fetchall()
    pending = con.execute("SELECT COUNT(*) FROM predictions WHERE made_at >= ?", (since,)).fetchone()[0] - len(rows)
    if not rows:
        return {"days": days, "scored": 0, "waiting_for_arrival": pending}
    err = [abs(r[1] - r[5]) for r in rows]
    banded = [r for r in rows if r[3] is not None]
    trains: dict = {}
    for r, e in zip(rows, err):
        trains.setdefault(r[0], []).append(e)
    return {
        "days": days, "scored": len(rows), "waiting_for_arrival": pending,
        "mae": round(sum(err) / len(err), 2),
        "naive_mae": round(sum(abs(r[2] - r[5]) for r in rows) / len(rows), 2),  # "delay stays the same"
        "within_5_min": round(sum(e <= 5 for e in err) / len(err), 3),
        "inside_range": round(sum(r[3] <= r[5] <= r[4] for r in banded) / len(banded), 3) if banded else None,
        "by_train": {t: {"scored": len(es), "mae": round(sum(es) / len(es), 2)} for t, es in sorted(trains.items())},
    }


def rival_scores(con: sqlite3.Connection, days: int = 7) -> dict:
    """Track Pulse against RailRadar's forecast and against carrying the delay forward, on exactly the
    same (train, moment, station) triples, once the train has arrived there."""
    since = (datetime.now(IST) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = con.execute("""
        SELECT p.pred_delay, v.pred_delay, p.delay_now, COALESCE(r.delay_arr, r.delay_dep)
        FROM predictions p
        JOIN rivals v ON v.train_no = p.train_no AND v.date = p.date AND v.made_at = p.made_at AND v.station = p.station
        JOIN runs r ON r.train_no = p.train_no AND r.date = p.date AND r.station = p.station
        WHERE p.made_at >= ? AND COALESCE(r.delay_arr, r.delay_dep) IS NOT NULL""", (since,)).fetchall()
    if not rows:
        return {"days": days, "scored": 0}
    mae = lambda i: round(sum(abs(r[i] - r[3]) for r in rows) / len(rows), 2)  # noqa: E731
    return {"days": days, "scored": len(rows), "ours": mae(0), "railradar": mae(1), "carried": mae(2),
            "ours_closer": round(sum(abs(r[0] - r[3]) < abs(r[1] - r[3]) for r in rows) / len(rows), 3)}


def _check():
    con = sqlite3.connect(":memory:")
    con.executescript("""CREATE TABLE runs (train_no, date, station, delay_arr, delay_dep);
        CREATE TABLE predictions (train_no, date, made_at, at_station, delay_now, station, pred_delay, lo, hi, model);
        INSERT INTO runs VALUES ('1', 'd', 'B', 12, 14), ('1', 'd', 'C', NULL, 4);""")
    now = datetime.now(IST).isoformat()
    con.executemany("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("1", "d", now, "A", 20, "B", 10, 5, 15, "m"),   # off by 2, inside range; naive off by 8
        ("1", "d", now, "A", 20, "C", 10, 8, 12, "m"),   # off by 6, outside range; naive off by 16
        ("1", "d", now, "A", 20, "D", 10, 8, 12, "m")])  # not arrived yet
    s = live_scores(con)
    assert (s["scored"], s["waiting_for_arrival"], s["mae"], s["naive_mae"], s["within_5_min"], s["inside_range"]) == \
        (2, 1, 4.0, 12.0, 0.5, 0.5), s
    con.execute("CREATE TABLE rivals (train_no, date, made_at, station, pred_delay, source)")
    con.execute("INSERT INTO rivals VALUES ('1', 'd', ?, 'B', 20, 'railradar')", (now,))  # off by 8; ours off by 2
    r = rival_scores(con)
    assert (r["scored"], r["ours"], r["railradar"], r["carried"], r["ours_closer"]) == (1, 2.0, 8.0, 8.0, 1.0), r


if __name__ == "__main__":
    _check()
    print(live_scores(sqlite3.connect(DB), int(sys.argv[1]) if len(sys.argv) > 1 else 7))
