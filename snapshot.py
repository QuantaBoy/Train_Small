"""A static snapshot of the live site, for hosts that only serve files (the Vercel link).

Renders every page from the running app into public/: home, control, map, How it works, the train page of every
followed or running train, the busiest station boards, and the JSON a display would read. Search and the station
picker work on the snapshot (static/snapshot.js), and so does the what-if form (below).

    python snapshot.py            # then commit public/ and push; Vercel serves it
    python snapshot.py --whatif   # also re-work the what-if answers (~20 min; after a retrain)

What-if on the snapshot: the model's answer for every train, every station and WHATIF_DELAYS minutes late is
worked out here and saved in public/whatif/<train>.json; the page interpolates between the two nearest delays.
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
from api import IST, NAMES, STATIONS, TRAINS, running  # noqa: E402
from app import app  # noqa: E402
from eta import eta, predict, route  # noqa: E402

WHATIF_DELAYS = [0, 15, 30, 60, 120, 240]


def whatif(no: str) -> dict:
    """{stops: [[seq, code, sch, day]], names, delays, grid: {seq: {delay: [[stop index, p50, p10, p90]]}}}."""
    stops = route(no)
    index = {seq: i for i, (seq, *_) in enumerate(stops)}
    grid = {}
    for seq, *_ in stops[:-1]:
        grid[seq] = {d: [[index[p["seq"]], p["delay"], p["lo"], p["hi"]] for p in predict(no, seq, d, explain=False)]
                     for d in WHATIF_DELAYS}
    return {"stops": [list(s) for s in stops], "names": {code: STATIONS.get(code, code) for _, code, *_ in stops},
            "delays": WHATIF_DELAYS, "grid": grid}


def write(path: str, body: bytes):
    f = OUT / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(body)


def main():
    keep = OUT / "whatif"
    saved = {f.name: f.read_bytes() for f in keep.glob("*.json")} if keep.exists() else {}
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(ROOT / "static", OUT / "static")
    for name, body in saved.items():  # what-if answers only change when the model does
        write(f"whatif/{name}", body)
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
    if "--whatif" in sys.argv:
        for i, no in enumerate(trains, 1):
            write(f"whatif/{no}.json", json.dumps(whatif(no), separators=(",", ":")).encode())
            print(f"  what-if {i}/{len(trains)} {no}", flush=True)
    print(f"{ok} pages, {len(trains)} trains, {len(stations)} stations, taken {index['taken']} -> {OUT}")


if __name__ == "__main__":
    main()
