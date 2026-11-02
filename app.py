"""Level 6+: the Flask website.

    /                 live trains, accuracy, the trains we cover
    /train/<number>   where the train is and how late -> predicted arrival at every station ahead
    /docs             how it works: data, models, why this model, routing, self-check, API
    /api/...          JSON APIs, see api.py

    python app.py     # http://127.0.0.1:5000
"""
import sys
from pathlib import Path

from datetime import date, datetime, timedelta

from flask import Flask, abort, redirect, render_template, request, url_for

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "model"))
from api import NAMES, CORRIDORS, feed_status, platform_clashes, station_board, train_view, DELAYED_MIN, DISRUPTED_MIN, SECTION_DAYS, STATIONS, TRAINS, api, con, sections, control_rows, running, live_scores, report_file  # noqa: E402
from eta import FEATURES, IST, history, pairs, timing, K, MIN_RUNS, RECENT_DAYS, REFRESH_S, eta, minute_of, model, predict, route, traffic  # noqa: E402
from train import DROP_RECENT  # noqa: E402
sys.path.insert(0, str(ROOT / "live"))
from feed import adopt, source_name  # noqa: E402
from score import rival_scores  # noqa: E402

app = Flask(__name__)
app.register_blueprint(api)

METHODS = {"model": "ML model", "similar_runs": "Similar past runs", "carried": "Current delay carried"}


def span(minutes) -> str:
    """134 -> '2 h 14 min', 45 -> '45 min'. Long-distance delays run to hours; nobody reads '594 min'."""
    m = abs(round(minutes))
    if m >= 1440:
        return f"{m // 1440} d {m % 1440 // 60} h {m % 60:02d} min"
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def late(minutes) -> str:
    """Signed delay: '+2 h 14 min', '-4 min'."""
    return ("-" if round(minutes) < 0 else "+") + span(minutes)


def on_day(sch: str, delay: float, day: int = 0, run_date: str | None = None) -> str:
    """The day an expected time falls on, once delay can push it past midnight: the date on a live
    run ('Tue 3 Nov'), otherwise 'day 2'; empty on the first day."""
    h, m = map(int, sch.split(":"))
    d = day + (h * 60 + m + round(delay)) // 1440
    if run_date:
        x = date.fromisoformat(run_date) + timedelta(days=d)
        return f"{x:%a} {x.day} {x:%b}"
    return f"day {d + 1}" if d > 0 else ""


@app.context_processor
def helpers():
    return {"stn": lambda code: STATIONS.get(code, code), "methods": METHODS, "eta": eta, "late": late, "span": span, "on_day": on_day,
            "feed_down": feed_status(),
            "nice": lambda name: name.removeprefix("blend_").replace("+", " + ").replace("_", " ")}


def hero_example() -> dict | None:
    """The one-line proof for the home page: Kaveri Express 30 min late at Katpadi, and where it ends up."""
    stops = route("16021")
    at = next((s for s in stops if s[1] == "KPD"), None)
    if not at:
        return None
    sbc = {p["station"]: p for p in predict("16021", at[0], 30)}.get("SBC")
    return sbc and {"delay_now": 30, "at": "KPD", "to": "SBC", "delay_then": sbc["delay"],
                    "sch": sbc["sch"], "eta": eta(sbc["sch"], sbc["delay"])}


@app.get("/")
def index():
    corridors = {c: [{"no": n, "name": TRAINS[n], "route": route(n)} for n in nos if route(n)]
                 for c, nos in CORRIDORS.items()}
    live = control_rows()
    for t in live:  # how far along its route each train is, for the little track on its card
        codes = [c for _, c, *_ in route(t["train"])]
        t |= {"from": codes[0], "to": codes[-1],
              "progress": codes.index(t["at"]) / (len(codes) - 1) if t["at"] in codes else 0}
    q = request.args.get("q", "")
    return render_template("index.html", q=q, hits=[(n, NAMES[n]) for n in matches(q)[:30]], all_count=len(NAMES), corridors=corridors, train_count=len(TRAINS), acc=report_file("accuracy"), live=live, rivals=rival_scores(con, 7), names=NAMES,
                           models=report_file("models"), self_check=live_scores(con, 7), hero=hero_example())


def matches(q: str) -> list[str]:
    """Trains whose number or name contains the search text; followed trains first."""
    q = q.strip().lower()
    if not q:
        return []
    num = q.split()[0]
    if num in NAMES or (len(num) == 5 and num.isdigit()):
        return [num]
    hits = [n for n, name in NAMES.items() if q in f"{n} {name}".lower()]
    return sorted(hits, key=lambda n: (n not in TRAINS, n))


@app.get("/find")
def find():
    """Home search box: one match opens the train; several are listed under the box."""
    q = request.args.get("q", "")
    hits = matches(q)
    if len(hits) == 1:
        return redirect(url_for("train", number=hits[0]))
    return redirect(url_for("index", q=q, _anchor="search"))


def on_my_line(number: str, ahead: list[str], passed: list[str]) -> list[dict]:
    """Other running trains that still have to pass stations this train has yet to reach: how one
    train's delay spreads to another. role:
      ahead    same direction, in front: a late one holds the line for this train
      behind   same direction, still to pass stations this train has already passed: this train's
               delay can hold it up (downstream impact)
      crossing opposite direction: shares stations and platforms"""
    mine = [c for _, c, *_ in route(number)]
    out = []
    for pos in running():
        no = pos["train_no"]
        if no == number or no not in NAMES:
            continue
        theirs = [c for seq, c, *_ in route(no) if seq >= pos["seq"]]
        shared = [c for c in theirs if c in ahead]
        if not shared:
            continue
        # Direction from the whole routes: one shared station ahead says nothing about which way they run.
        common = [c for _, c, *_ in route(no) if c in mine]  # in their order
        same_way = len(common) < 2 or mine.index(common[0]) < mine.index(common[-1])
        role = "crossing" if not same_way else "behind" if any(c in passed for c in theirs) else "ahead"
        out.append({"no": no, "name": NAMES[no], "at": pos["station"], "delay": round(pos["delay"]),
                    "shared": shared, "same_way": same_way, "role": role})
    return sorted(out, key=lambda t: (["ahead", "behind", "crossing"].index(t["role"]), -t["delay"]))


@app.get("/train/<number>")
def train(number):
    if number not in NAMES:
        # Not in the train list (a special, or a train newer than the list): ask the live source.
        name = len(number) == 5 and number.isdigit() and source_name(number)
        if not name:
            abort(404)
        NAMES[number] = name
    stops = route(number)
    if len(stops) < 2 and adopt(number):  # a train nobody has opened yet: fetch its history now
        for f in (history, pairs, route, timing):
            f.cache_clear()
        stops = route(number)
    if len(stops) < 2:
        abort(404)  # not running lately, or the source has no reports for it
    # Open on the train's live position when the feed has one; the form can still override it.
    pos = next(iter(running(number)), None)
    live = pos is not None and "at" not in request.args
    at = request.args.get("at", type=int, default=pos["seq"] if live else stops[0][0])
    delay = request.args.get("delay", type=int, default=round(pos["delay"]) if live else 20)
    delay = max(-60, min(600, delay))  # keep input in a sane range
    if at not in {s[0] for s in stops[:-1]}:
        at = stops[0][0]
    rows = [{**p, "eta": eta(p["sch"], p["delay"]), "naive": eta(p["sch"], delay), "change": p["delay"] - delay}
            for p in predict(number, at, delay, run_date=pos["date"] if live else None,
                                     at_time=pos["updated_at"] if live else None)]
    for prev, r in zip([None] + rows, rows):
        r["leg"] = r["delay"] - (prev["delay"] if prev else delay)  # minutes won back (<0) or lost on the way here
    ahead = [r["station"] for r in rows]
    seen = int(minute_of(pos["updated_at"] if live else datetime.now(IST)))  # whole minutes: traffic() is cached
    n, line_delay, line_gain = traffic(number, at, seen)
    here = next(s for s in stops if s[0] == at)
    # RailRadar's forecast made from this same position (live only), shown beside ours.
    rival = dict(con.execute("SELECT station, pred_delay FROM rivals WHERE train_no=? AND date=? AND made_at=?",
                             (number, pos["date"], pos["updated_at"])).fetchall()) if live else {}
    report = report_file("accuracy") or {}
    halt = train_view(number, pos, explain=False)["held"] if live else None
    known_runs = con.execute("SELECT COUNT(DISTINCT date) FROM runs WHERE train_no=?", (number,)).fetchone()[0]
    secs = sections(number)
    return render_template("train.html", no=number, here=here, passed=[s for s in stops if s[0] < at],
                           line=on_my_line(number, ahead, [s[1] for s in stops if s[0] <= at]), delay_at={r["station"]: r["delay"] for r in rows}, traffic={"n": n, "delay": line_delay, "gain": line_gain},
                           slack={(s["from"], s["to"]): s for s in secs}, name=NAMES[number], stops=stops, at=at, delay=delay,
                           live=pos if live else None, rows=rows, acc=report.get(number), rival=rival, known_runs=known_runs, halt=halt,
                           recovery=report.get("recovery", {}).get(number),
                           worst=max([r["delay"] for r in rows] + [delay, 1]), sections=secs,
                           section_days=SECTION_DAYS, prev_code=[here[1]] + ahead)


@app.get("/station/")
@app.get("/station/<code>")
def station(code=None):
    """The station display board: every running train still to come here, with P10/P50/P90."""
    code = (code or request.args.get("code", "")).strip().upper()
    if code and code not in STATIONS:
        hit = next((c for c, n in STATIONS.items() if n.lower().startswith(code.lower())), None)
        if hit:
            return redirect(url_for("station", code=hit))
    return render_template("station.html", code=code, name=STATIONS.get(code, code),
                           clashes=platform_clashes(code) if code else [],
                           board=station_board(code) if code else [], stations=STATIONS,
                           now=datetime.now(IST).strftime("%H:%M"))


@app.get("/map")
def network_map():
    """Every operational line in India (single vs double), with the live trains on it."""
    where = dict((s, (lat, lng)) for s, lat, lng in con.execute(
        "SELECT station, AVG(lat), AVG(lng) FROM runs WHERE lat IS NOT NULL GROUP BY station"))
    trains = [{**t, "lat": where[t["at"]][0], "lng": where[t["at"]][1], "at_name": STATIONS.get(t["at"], t["at"])}
              for t in control_rows() if t["at"] in where]
    return render_template("map.html", trains=trains)


@app.get("/control")
def control_room():
    """The control office screen: flags first (held trains, platform clashes, late trains with others
    following), then every running train, worst first."""
    rows = control_rows()
    spread = []
    for t in rows:
        if t["delay_min"] < DELAYED_MIN:
            continue
        pos = next(iter(running(t["train"])), None)
        stops = route(t["train"])
        if not pos or not stops:
            continue
        behind = [o for o in on_my_line(t["train"], [c for s, c, *_ in stops if s > pos["seq"]],
                                         [c for s, c, *_ in stops if s <= pos["seq"]]) if o["role"] == "behind"]
        if behind:
            spread.append({"train": t["train"], "name": t["name"], "delay": t["delay_min"], "behind": behind})
    counts = {k: sum(t["status"] == k for t in rows) for k in ("normal", "delayed", "disrupted")}
    return render_template("control.html", rows=rows, counts=counts, held=[t for t in rows if t["held"]],
                           clashes=platform_clashes(), spread=spread, now=datetime.now(IST).strftime("%H:%M"))


@app.get("/docs")
def docs():
    m = model()
    return render_template("docs.html", models=report_file("models"), acc=report_file("accuracy"),
                           self_check=live_scores(con, 7), in_use=m["name"] if m else None,
                           features=FEATURES, k=K, min_runs=MIN_RUNS, recent_days=RECENT_DAYS,
                           refresh_min=REFRESH_S // 60, trains=len(TRAINS), corridors=CORRIDORS,
                           drop_recent=DROP_RECENT, delayed_min=DELAYED_MIN, disrupted_min=DISRUPTED_MIN,
                           all_count=len(NAMES), rivals=rival_scores(con, 7))


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return {"error": "not found"}, 404
    return render_template("404.html"), 404


if __name__ == "__main__":
    assert matches("12951") == ["12951"] and matches("zzzz") == []
    assert on_day("23:30", 45) == "day 2" and on_day("10:00", 5) == "" and span(1500) == "1 d 1 h 00 min"
    assert late(594) == "+9 h 54 min" and late(-4) == "-4 min" and span(60) == "1 h 00 min"
    app.run(debug=True)
