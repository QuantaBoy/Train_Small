"""A static snapshot of the live site, for hosts that only serve files (the Vercel link).

Renders every page from the running app into public/: home, control, map, How it works, the train page of every
followed or running train, the busiest station boards, and the JSON a display would read. Search and the station
picker work on the snapshot (static/snapshot.js); the what-if form needs the live server and says so.

    python snapshot.py        # then commit public/ and push; Vercel serves it
"""
import json
import re
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "public"
STATION_PAGES = 80  # the busiest stations on running trains' routes ahead

sys.path.insert(0, str(ROOT / "model"))
from api import IST, NAMES, TRAINS, running  # noqa: E402
from app import app  # noqa: E402
from eta import route  # noqa: E402


def write(path: str, body: bytes):
    f = OUT / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(body)


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(ROOT / "static", OUT / "static")
    c = app.test_client()
    taken = datetime.now(IST)
    live = [p["train_no"] for p in running()]
    trains = sorted(set(TRAINS) | set(live))
    ahead = Counter(code for p in running() for seq, code, *_ in route(p["train_no"]) if seq > p["seq"])
    stations = [code for code, _ in ahead.most_common(STATION_PAGES)]

    pages = {"index.html": "/", "control.html": "/control", "map.html": "/map", "docs.html": "/docs",
             "station/index.html": "/station/"}
    pages |= {f"train/{no}.html": f"/train/{no}" for no in trains}
    pages |= {f"station/{code}.html": f"/station/{code}" for code in stations}
    index = {"taken": taken.strftime("%d %b %Y, %H:%M IST"), "trains": {no: NAMES.get(no, no) for no in trains},
             "stations": stations}
    banner = (f'<p class="snapshot" role="status">Snapshot taken {index["taken"]}. Track Pulse updates every 5 minutes on '
              f'our server; this copy shows the site as it was then.</p>')
    script = f'<script>window.SNAPSHOT = {json.dumps(index)};</script><script src="/static/snapshot.js"></script>'
    ok = 0
    for path, url in pages.items():
        r = c.get(url)
        if r.status_code != 200:
            print(f"  skipped {url}: {r.status_code}")
            continue
        html = r.get_data(as_text=True)
        html = re.sub(r'<meta http-equiv="refresh"[^>]*>', "", html)  # a snapshot never changes
        html = html.replace("</header>", "</header>\n  " + banner, 1).replace("</body>", script + "\n</body>", 1)
        write(path, html.encode("utf-8"))
        ok += 1
    write("404.html", c.get("/train/00000").get_data())  # the app's own not-found page

    api = {"api/control.json": "/api/control", "api/accuracy.json": "/api/accuracy",
           "api/platforms/clashes.json": "/api/platforms/clashes"}
    api |= {f"api/trains/{no}/live.json": f"/api/trains/{no}/live" for no in live}
    api |= {f"api/stations/{code}/arrivals.json": f"/api/stations/{code}/arrivals" for code in stations}
    for path, url in api.items():
        r = c.get(url)
        if r.status_code == 200:
            write(path, r.get_data())
    write("snapshot.json", json.dumps(index, indent=1).encode())
    print(f"{ok} pages, {len(trains)} trains, {len(stations)} stations, taken {index['taken']} -> {OUT}")


if __name__ == "__main__":
    main()
