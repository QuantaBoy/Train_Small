"""Levels 8 + 10: the live feed. Finds where each train is, POSTs it to the API (Level 7),
saves every observed stop into `runs` so the model keeps learning, and once a day relearns.

    python live/feed.py live              # real trains, now, every 5 min (whereismytrain.in public cache)
    python live/feed.py live --once       # one pass and exit
    python live/feed.py backfill 2026-09-02 2026-10-25   # fill runs for past days
    python live/feed.py backfill 2025-03-01 --trains 12951,12952   # a newly added train's past
    python live/feed.py replay 2026-08-30 # replay a past day from our own data, 60x speed (demo / offline)

Start the website first (python app.py); the feed talks to it over HTTP like any outside feed would.
"""
import argparse
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from api import DB, TRAINS  # noqa: E402
from eta import SANE, route  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
LIVE_URL = "https://whereismytrain.in/cache/live_status?date={d:%d-%m-%Y}&train_no={no}"
# ponytail: third-party public cache, not an official feed. Fine for a prototype at ~50 trains
# every 5 min; for real deployment swap in NTES / IR's own feed behind the same POST.
LIVE_EVERY_S = 300
RELEARN_HOUR = 3  # IST: quiet hours; yesterday's runs have all reported by then
ROOT = Path(__file__).parent.parent
POLITE_GAP_S = 1
GONE_QUIET_H = 3  # no report for this long -> treat the run as over


def post(base: str, no: str, station: str, delay: float, date: str, reported_at: str) -> int:
    body = json.dumps({"station": station, "delay": round(delay), "date": date, "reported_at": reported_at}).encode()
    req = urllib.request.Request(f"{base}/api/trains/{no}/position", body, {"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError as e:  # site down or restarting: skip this post, the next pass sends a fresh one
        return f"site unreachable ({e})"


def last_position(no: str, stops: list[dict]) -> tuple[str, float, str] | None:
    """Latest reported stop that is one of our route halts and not the last one.
    stops: [{station, delay, at}] in route order, actual reports only."""
    halts = [s for _, s, *_ in route(no)]
    usable = [s for s in stops if s["station"] in halts[:-1]]
    reached_end = any(s["station"] == halts[-1] for s in stops)
    if not usable or reached_end:
        return None
    s = usable[-1]
    return s["station"], s["delay"], s["at"]


# ---- live: whereismytrain.in --------------------------------------------------------------

def fetch(no: str, day) -> list[dict]:
    """The source's stops for one run, each with only the times it actually observed:
    {station, arr, dep} as unix seconds or None, plus sch_arr / sch_dep."""
    req = urllib.request.Request(LIVE_URL.format(d=day, no=no), headers={"User-Agent": "TrackPulse-SIH-prototype"})
    data = json.load(urllib.request.urlopen(req, timeout=20))
    out = []
    for s in data.get("days_schedule", []):
        if s.get("source") == "eta":  # a forecast, not a report
            continue
        # Whole minutes, like the timetable and the Kaggle history, so delays line up exactly.
        minute = lambda t: t - t % 60 if t else None  # noqa: E731
        arr, dep = minute(s.get("actual_arrival_tm")), minute(s.get("actual_departure_tm"))
        # Before the train leaves, the source fills departure with the timetable value, which can sit
        # before the real arrival (SHU arr 19:36, "dep" 19:25). Only trust a departure after the arrival.
        if arr and dep and dep < arr:
            dep = None
        if not (arr or dep):
            continue
        out.append({"station": s["station_code"], "arr": arr, "dep": dep,
                    "sch_arr": s.get("sch_arrival_tm"), "sch_dep": s.get("sch_departure_tm"),
                    "seq": s.get("sno"), "halts": int(bool(s.get("stops"))), "km": s.get("distance"),
                    "lat": s.get("lat"), "lng": s.get("lng")})
    return out


BACKUP_URL = "https://railradar.in/api/v1/trains/{no}/live"


def railradar(no: str) -> dict:
    req = urllib.request.Request(BACKUP_URL.format(no=no), headers={"User-Agent": "TrackPulse-SIH-prototype"})
    return json.load(urllib.request.urlopen(req, timeout=20))["data"]


def log_rival(con, no: str, day, made_at: str, data: dict | None = None) -> int:
    """Save RailRadar's own forecast for the halts ahead, tagged with the position our forecast was made
    from, so model/score.py can compare the two on the same moments. Whereismytrain's public cache only
    repeats the timetable for stations ahead, so it has no forecast to compare."""
    data = data or railradar(no)
    if data.get("startDate") != day.isoformat():
        return 0
    rows = [(no, day.isoformat(), made_at, s["stationCode"], s.get("delayArrival", s.get("delayDeparture")), "railradar")
            for s in data.get("route", []) if s.get("status") == "upcoming" and s.get("isHalt")
            and s.get("delayArrival", s.get("delayDeparture")) is not None]
    con.executemany("INSERT OR REPLACE INTO rivals VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def fetch_backup(con, no: str, day, data: dict | None = None) -> list[dict]:
    """railradar.in reports, in fetch()'s format. It knows only the current run, and numbers stops
    differently, so stops are matched to our numbering by station code."""
    data = data or railradar(no)
    if data.get("startDate") != day.isoformat():
        return []
    ours = {s: (seq, lat, lng) for s, seq, lat, lng in con.execute(
        "SELECT station, MAX(seq), MAX(lat), MAX(lng) FROM runs WHERE train_no=? GROUP BY station", (no,))}
    epoch = lambda t: t and int(datetime.fromisoformat(t).timestamp()) // 60 * 60  # noqa: E731
    out = []
    for s in data.get("route", []):
        # Reports: stations it has "departed", and the one it is "at-station" (arrival real, departure
        # still their forecast). "upcoming" ones are forecasts, not reports.
        if s.get("status") not in ("departed", "at-station") or s["stationCode"] not in ours:
            continue
        seq, lat, lng = ours[s["stationCode"]]
        dep = s.get("actualDeparture") if s["status"] == "departed" else None
        out.append({"station": s["stationCode"], "arr": epoch(s.get("actualArrival")), "dep": epoch(dep),
                    "sch_arr": epoch(s.get("scheduledArrival")), "sch_dep": epoch(s.get("scheduledDeparture")),
                    "seq": seq, "halts": int(s["isHalt"]), "km": s.get("distance"), "lat": lat, "lng": lng})
    return out


def merge(main: list[dict], other: list[dict]) -> list[dict]:
    """Two sources' reports for one run, per station keeping whichever saw the train later. The
    whereismytrain cache can lag its own app by an hour; RailRadar often has the newer station."""
    latest = lambda s: max(s["arr"] or 0, s["dep"] or 0)  # noqa: E731
    by = {s["station"]: s for s in main}
    for s in other:
        if s["station"] not in by or latest(s) > latest(by[s["station"]]):
            by[s["station"]] = s
    return sorted(by.values(), key=lambda s: s["seq"] if s["seq"] is not None else float("inf"))


def reports(stops: list[dict]) -> list[dict]:
    """fetch() output -> [{station, delay, at}]: the latest observed time at each stop and how late it was."""
    out = []
    for s in stops:
        if s["dep"] and s["sch_dep"]:
            delay, at = (s["dep"] - s["sch_dep"]) / 60, s["dep"]
        elif s["arr"] and s["sch_arr"]:
            delay, at = (s["arr"] - s["sch_arr"]) / 60, s["arr"]
        else:
            continue
        out.append({"station": s["station"], "delay": delay, "at": datetime.fromtimestamp(at, IST).isoformat(timespec="seconds")})
    return out


def store(con, no: str, day: str, stops: list[dict]) -> int:
    """Write one run's observed stops into `runs`, the table everything learns from (Level 2 format).
    Re-fetching the same run overwrites its rows, never duplicates them."""
    stamp = lambda t: t and datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    late = lambda a, s: (a - s) / 60 if a and s else None  # noqa: E731
    rows = []
    for s in stops:
        if s["seq"] is None:
            continue
        # The source's stop numbering is the one the Kaggle history uses, so old and new runs line up.
        seq, halts, km, lat, lng = s["seq"], s["halts"], s["km"], s["lat"], s["lng"]
        dl_arr, dl_dep = late(s["arr"], s["sch_arr"]), late(s["dep"], s["sch_dep"])
        if any(d is not None and not SANE[0] <= d <= SANE[1] for d in (dl_arr, dl_dep)):
            continue
        rows.append((no, day, seq, s["station"], halts, km, lat, lng, stamp(s["sch_arr"]), stamp(s["sch_dep"]),
                     stamp(s["arr"]), stamp(s["dep"]), dl_arr, dl_dep))
    con.executemany("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


THREADS = 4  # trains fetched at once; each thread still waits POLITE_GAP_S between its own requests


def followed() -> list[str]:
    """TRAINS plus every train adopted from the website: an adopted train has runs from the last few days.
    ponytail: once opened, a train is followed for good; prune by last view if the list gets too long."""
    con = sqlite3.connect(DB, timeout=60)
    since = (datetime.now(IST).date() - timedelta(days=3)).isoformat()
    extra = {n for (n,) in con.execute("SELECT DISTINCT train_no FROM runs WHERE date >= ?", (since,))}
    con.close()
    return sorted(set(TRAINS) | extra)


def live_pass(base: str):
    with ThreadPoolExecutor(THREADS) as pool:
        list(pool.map(lambda no: live_train(base, no), followed()))


ADOPT_DAYS = 90  # enough for a weekly train to reach route()'s 10 runs


def source_name(no: str) -> str | None:
    """The train's name as the live source has it: for specials and new trains missing from our list."""
    for k in range(3):
        day = datetime.now(IST).date() - timedelta(days=k)
        req = urllib.request.Request(LIVE_URL.format(d=day, no=no), headers={"User-Agent": "TrackPulse-SIH-prototype"})
        try:
            name = json.load(urllib.request.urlopen(req, timeout=20)).get("train_name")
        except Exception:
            continue
        if name:
            return name.title()
    return None


def adopt(no: str) -> int:
    """The first time someone opens a train we don't follow yet: fetch its last ADOPT_DAYS of runs so the
    model has history to work from (~15 s). From then on live_pass follows it like the rest."""
    today = datetime.now(IST).date()

    def one(day) -> int:
        con = sqlite3.connect(DB, timeout=60)
        try:
            return store(con, no, day.isoformat(), fetch(no, day))
        except Exception:  # a missing day is fine; the rest still give history
            return 0
        finally:
            con.close()

    with ThreadPoolExecutor(8) as pool:
        return sum(pool.map(one, (today - timedelta(days=k) for k in range(ADOPT_DAYS))))


def live_train(base: str, no: str):
    today = datetime.now(IST).date()
    con = sqlite3.connect(DB, timeout=60)
    posted = False
    try:
        rr = railradar(no)  # one call per pass: it only knows the current run
    except Exception:
        rr = None
    for day in (today - timedelta(days=k) for k in range(3)):  # multi-day trains are still on an older run
        backup = fetch_backup(con, no, day, rr) if rr else []
        try:
            stops = merge(fetch(no, day), backup)
        except Exception as e:  # one bad train must not stop the rest
            if not backup:
                print(f"  {no} {day}: fetch failed ({e})")
                continue
            stops = backup
        finally:
            time.sleep(POLITE_GAP_S)
        saved = store(con, no, day.isoformat(), stops)
        pos = None if posted else last_position(no, reports(stops))
        if pos and datetime.now(IST) - datetime.fromisoformat(pos[2]) > timedelta(hours=GONE_QUIET_H):
            pos = None  # the run stopped reporting hours ago (e.g. never reported its last stops): not live
        if pos:
            station, delay, at = pos
            posted = True
            print(f"  {no} run {day}: at {station} {delay:+.0f} min (reported {at[11:16]}), {saved} stops saved -> "
                  f"{post(base, no, station, delay, day.isoformat(), at)}")
            try:
                log_rival(con, no, day, at, rr)
            except Exception as e:  # the comparison is a bonus; never let it stop the feed
                print(f"  {no}: no RailRadar forecast ({e})")
        elif saved:
            print(f"  {no} run {day}: {saved} stops saved")
    if not posted:
        print(f"  {no}: not running right now")
    con.close()


def backfill(first: str, last: str, trains: list[str] | None = None):
    """Fill `runs` with every day from first to last (YYYY-MM-DD) for `trains` (default all), e.g.
    the weeks between the Kaggle snapshot and today, or a newly added train's past. Safe to re-run;
    it overwrites, never duplicates."""
    start, end = datetime.fromisoformat(first).date(), datetime.fromisoformat(last).date()

    def one(no: str):
        con, day, total = sqlite3.connect(DB, timeout=60), start, 0
        while day <= end:
            try:
                total += store(con, no, day.isoformat(), fetch(no, day))
            except Exception as e:
                print(f"  {no} {day}: fetch failed ({e})", flush=True)
            time.sleep(POLITE_GAP_S)
            day += timedelta(days=1)
        con.close()
        print(f"  {no}: {total} stops saved, {first} to {last}", flush=True)

    with ThreadPoolExecutor(THREADS) as pool:
        list(pool.map(one, trains or list(TRAINS)))


# ---- replay: a past day from data/trackpulse.db ---------------------------------------------

def replay(base: str, day: str, speed: float, tick_s: float):
    con = sqlite3.connect(DB, timeout=30)
    start = datetime.fromisoformat(f"{day}T00:00").replace(tzinfo=IST)
    runs = {}  # no -> (run date, [{station, delay, at}])
    for no in TRAINS:
        for d in ((start - timedelta(days=1)).date().isoformat(), day):
            rows = con.execute("""SELECT station, COALESCE(delay_dep, delay_arr), COALESCE(act_dep, act_arr)
                                  FROM runs WHERE train_no=? AND date=? ORDER BY seq""", (no, d)).fetchall()
            if rows:
                runs[(no, d)] = [{"station": s, "delay": dl, "at": a} for s, dl, a in rows if a and dl is not None]
    con.close()
    clock, sent = start, {}
    print(f"Replaying {day} at {speed:.0f}x: {len(runs)} runs. Ctrl+C to stop.")
    while clock < start + timedelta(days=1):
        for (no, d), stops in runs.items():
            seen = [s for s in stops if datetime.fromisoformat(s["at"]).replace(tzinfo=IST) <= clock]
            pos = last_position(no, seen)
            if pos and sent.get(no) != pos:
                sent[no] = pos
                station, delay, _ = pos
                now = datetime.now(IST).isoformat(timespec="seconds")
                print(f"  [{clock:%H:%M}] {no} at {station} {delay:+.0f} min -> {post(base, no, station, delay, d, now)}")
        clock += timedelta(seconds=tick_s * speed)
        time.sleep(tick_s)


def _check():
    a = [{"station": "NGP", "arr": 100, "dep": None, "seq": 155}, {"station": "SLOR", "arr": 200, "dep": None, "seq": 160}]
    b = [{"station": "NGP", "arr": 90, "dep": 120, "seq": 155}, {"station": "SEGM", "arr": 300, "dep": None, "seq": 170}]
    m = merge(a, b)
    assert [s["station"] for s in m] == ["NGP", "SLOR", "SEGM"] and m[0]["dep"] == 120, m


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["live", "replay", "backfill"])
    p.add_argument("day", nargs="?", help="replay: YYYY-MM-DD from data/trackpulse.db; backfill: first day")
    p.add_argument("last", nargs="?", help="backfill: last day (default yesterday)")
    p.add_argument("--trains", help="backfill: comma-separated train numbers (default all)")
    p.add_argument("--base", default="http://127.0.0.1:5000")
    p.add_argument("--speed", type=float, default=60, help="replay: simulated seconds per real second")
    p.add_argument("--once", action="store_true", help="live: one pass, then exit")
    a = p.parse_args()
    if a.mode == "replay":
        replay(a.base, a.day or "2026-08-30", a.speed, tick_s=2)
        return
    if a.mode == "backfill":
        backfill(a.day, a.last or (datetime.now(IST).date() - timedelta(days=1)).isoformat(),
                 a.trains and a.trains.split(","))
        return
    relearned = None
    while True:
        now = datetime.now(IST)
        print(f"{now:%H:%M:%S} live pass")
        live_pass(a.base)
        if a.once:
            return
        if now.hour == RELEARN_HOUR and relearned != now.date():
            relearn()
            relearned = now.date()
        time.sleep(LIVE_EVERY_S)


def relearn():
    """Daily: fetch the latest weather, retrain every model on the newest runs (train.py keeps the new winner only if it
    beats the model in use), then re-score accuracy. The website picks both up on its own."""
    for script in ("live/weather.py --recent", "model/train.py", "model/backtest.py"):
        print(f"  relearn: {script}")
        path, *args = script.split()
        r = subprocess.run([sys.executable, str(ROOT / path), *args], capture_output=True, text=True)
        print("   ", (r.stdout.strip().splitlines() or ["(no output)"])[-1] if r.returncode == 0 else f"failed: {r.stderr[-300:]}")


if __name__ == "__main__":
    main()
