"""Weather along every route: daily rain and coldest temperature (a fog proxy), from Open-Meteo
(free, no key). Stations are grouped into half-degree squares (~50 km) so one lookup covers
several stations. Fills the `weather` table that model/eta.py reads.

    python live/weather.py            # history since FROM, plus the last week and next 3 days
    python live/weather.py --recent   # only the last week and next 3 days (the feed runs this daily)
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "model"))
from eta import DB, cell  # noqa: E402

FROM = "2025-03-01"  # same start as the nationwide backfill
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive?start_date={start}&end_date={end}&"
FORECAST = "https://api.open-meteo.com/v1/forecast?past_days=7&forecast_days=3&"
QUERY = "latitude={lat}&longitude={lng}&daily=precipitation_sum,temperature_2m_min&timezone=Asia%2FKolkata"
CHUNK = 10  # squares per request


def squares(con) -> list[str]:
    """Every half-degree square a train in `runs` stops in."""
    return sorted({cell(lat, lng) for lat, lng in con.execute(
        "SELECT DISTINCT ROUND(lat, 2), ROUND(lng, 2) FROM runs WHERE lat IS NOT NULL")})


def fetch(url: str, cells: list[str]) -> list[tuple]:
    lat = ",".join(c.split(",")[0] for c in cells)
    lng = ",".join(c.split(",")[1] for c in cells)
    req = urllib.request.Request(url + QUERY.format(lat=lat, lng=lng), headers={"User-Agent": "TrackPulse-SIH-prototype"})
    data = json.load(urllib.request.urlopen(req, timeout=60))
    data = data if isinstance(data, list) else [data]
    rows = []
    for c, d in zip(cells, data):
        daily = d["daily"]
        rows += [(c, day, r, t) for day, r, t in zip(daily["time"], daily["precipitation_sum"], daily["temperature_2m_min"])
                 if r is not None or t is not None]
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recent", action="store_true")
    a = p.parse_args()
    con = sqlite3.connect(DB, timeout=60)
    con.execute("""CREATE TABLE IF NOT EXISTS weather (cell TEXT, date TEXT, rain_mm REAL, tmin REAL,
                   PRIMARY KEY (cell, date))""")
    cells = squares(con)
    jobs = [FORECAST]
    if not a.recent:
        # The archive lags about 5 days; the forecast's past_days covers the gap.
        # A square counts as done once it has history back to FROM (the forecast only adds recent days).
        have = dict(con.execute("SELECT cell, MIN(date) FROM weather GROUP BY cell"))
        end = (date.today() - timedelta(days=6)).isoformat()
        todo = [c for c in cells if have.get(c, "9999") > FROM]
        jobs += [(ARCHIVE.format(start=FROM, end=end), todo)]
    print(f"{len(cells)} weather squares")
    for job in jobs:
        url, todo = job if isinstance(job, tuple) else (job, cells)
        for i in range(0, len(todo), CHUNK):
            part = todo[i:i + CHUNK]
            try:
                rows = fetch(url, part)
            except Exception as e:  # e.g. the free daily quota: re-run tomorrow, it resumes
                print(f"  stopped at square {i}: {e}")
                break
            con.executemany("INSERT OR REPLACE INTO weather VALUES (?,?,?,?)", rows)
            con.commit()
            print(f"  {i + len(part)}/{len(todo)} squares, {len(rows)} days", flush=True)
            time.sleep(1)
    print(con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM weather").fetchone())
    con.close()


if __name__ == "__main__":
    main()
