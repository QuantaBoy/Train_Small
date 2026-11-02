"""Level 7: JSON APIs for the three audiences in the problem statement.

    GET  /api/trains                          all trains and their halts
    GET  /api/trains/<no>/eta?at=KPD&delay=30  what-if ETA, nothing stored       (mobile apps)
    POST /api/trains/<no>/position             {"station": "KPD", "delay": 30, "date": "2026-10-26",
                                                "reported_at": "2026-10-26T23:40:00+05:30"}  (date, reported_at optional)
                                               a live feed reports where a train is; returns its ETAs
    GET  /api/trains/<no>/live                 ETAs from the train's last reported position
    GET  /api/trains/<no>/sections             running time between halts: timetable vs real, last 90 days
    GET  /api/stations/<code>/arrivals         every reported train still heading here   (station displays)
    GET  /api/control                          every reported train at a glance          (control rooms)
    GET  /api/accuracy                         how accurate the predictor is (Level 5)
    GET  /api/models                           every model compared, and the one in use (Level 9)
    GET  /api/accuracy/live?days=7             live ETAs scored against what actually happened (Level 10)

Positions live in the `positions` table of data/trackpulse.db. Level 8 (live feed) will
just POST to /position, so nothing here changes when real data arrives. Every new position's
ETAs are logged to `predictions` so they can be scored once the train arrives (Level 10).
ponytail: POST has no auth; add a shared token before this is reachable from the internet.
"""
import json
import sqlite3
import sys
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median, quantiles

from flask import Blueprint, abort, jsonify, request

sys.path.insert(0, str(Path(__file__).parent / "model"))
from eta import DB, model, predict, route  # noqa: E402
from score import live_scores  # noqa: E402

api = Blueprint("api", __name__, url_prefix="/api")


@api.before_request
def no_writes_from_outside():
    """Behind a public tunnel the site is read-only: only the feed on this machine may post positions.
    Tunnels add X-Forwarded-For; the local feed talks to us directly and never sends it."""
    if request.method != "GET" and request.headers.get("X-Forwarded-For"):
        abort(403, "positions can only be posted from the server itself")
IST = timezone(timedelta(hours=5, minutes=30))
STALE_MIN = 60  # a position older than this is flagged; long-distance trains can go an hour between halts
GONE_QUIET_H = 3  # older than this, the run is over (same rule as live/feed.py): not shown as running
_json_cache: dict = {}


def report_file(name: str) -> dict | None:
    """data/<name>.json, re-read whenever the daily relearn rewrites it."""
    path = DB.parent / f"{name}.json"
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    if _json_cache.get(name, (None,))[0] != mtime:
        _json_cache[name] = (mtime, json.loads(path.read_text()))
    return _json_cache[name][1]


TRAINS = {
    "12007": "Mysuru Shatabdi", "12008": "Chennai Shatabdi",
    "12027": "Bengaluru Shatabdi", "12028": "Chennai Shatabdi",
    "12607": "Lalbagh SF Express", "12608": "Lalbagh SF Express",
    "12639": "Brindavan SF Express", "12640": "Brindavan SF Express",
    "12657": "Bengaluru SF Mail", "12658": "Chennai SF Mail",
    "16021": "Kaveri Express", "16022": "Kaveri Express",
    "22625": "Double Decker Express", "22626": "Double Decker Express",
    "12951": "Mumbai Rajdhani", "12952": "Mumbai Rajdhani",
    "12953": "August Kranti Rajdhani", "12954": "August Kranti Rajdhani",
    "12903": "Golden Temple Mail", "12904": "Golden Temple Mail",
    "12925": "Paschim Express", "12926": "Paschim Express",
    "12301": "Howrah Rajdhani", "12302": "Howrah Rajdhani",
    "12313": "Sealdah Rajdhani", "12314": "Sealdah Rajdhani",
    "12311": "Netaji Express", "12312": "Netaji Express",
    "12423": "Dibrugarh Rajdhani", "12424": "Dibrugarh Rajdhani",
    "12621": "Tamil Nadu Express", "12622": "Tamil Nadu Express",
    "12615": "Grand Trunk Express", "12616": "Grand Trunk Express",
    "12625": "Kerala Express", "12626": "Kerala Express",
    "12627": "Karnataka Express", "12628": "Karnataka Express",
    "12723": "Telangana Express", "12724": "Telangana Express",
    "12809": "Howrah Mail via Nagpur", "12810": "Howrah Mail via Nagpur",
    "12859": "Gitanjali Express", "12860": "Gitanjali Express",
    "12163": "Chennai Express", "12164": "Chennai Express",
}
# Trains that share track are grouped, so congestion on one shows up in the others' ETAs.
# Every regular coaching train in India (open datameet list); TRAINS above are the ones we follow
# from the start. Any other train is adopted the first time someone opens it (live/feed.py adopt()).
NAMES = json.loads((Path(__file__).parent / "data" / "trains.json").read_text(encoding="utf-8")) | TRAINS
CORRIDORS = {
    "Chennai, Bengaluru and Mysuru": ["12007", "12008", "12027", "12028", "12607", "12608", "12639", "12640",
                                      "12657", "12658", "16021", "16022", "22625", "22626"],
    "Delhi to Mumbai and Amritsar": ["12951", "12952", "12953", "12954", "12903", "12904", "12925", "12926"],
    "Delhi to Howrah, Sealdah and Assam": ["12301", "12302", "12313", "12314", "12311", "12312", "12423", "12424"],
    "Delhi to the South, over two days or more": ["12621", "12622", "12615", "12616", "12625", "12626",
                                                  "12627", "12628", "12723", "12724"],
    "Mumbai to Howrah and Chennai": ["12809", "12810", "12859", "12860", "12163", "12164"],
}
# Names from the open datameet railways station list (data/stations.json, only the codes we use),
# with hand-written names on top for our home corridor and stations renamed since that list was made.
STATIONS = json.loads((Path(__file__).parent / "data" / "stations.json").read_text(encoding="utf-8")) | {
    "CSMT": "Mumbai CSMT", "MMCT": "Mumbai Central", "DDU": "Pt. Deen Dayal Upadhyaya Jn",
    "PRYJ": "Prayagraj Jn", "RKMP": "Rani Kamlapati", "VGLJ": "Virangana Lakshmibai Jhansi Jn",
    "KLBG": "Kalaburagi",
    "MAS": "Chennai Central", "PER": "Perambur", "TRL": "Tiruvallur", "AJJ": "Arakkonam Jn",
    "AVN": "Anavardikhanpettai", "SHU": "Sholinghur", "WJR": "Walajah Road", "KPD": "Katpadi Jn",
    "AB": "Ambur", "VN": "Vaniyambadi", "JTJ": "Jolarpettai Jn", "KPN": "Kuppam", "BWT": "Bangarapet Jn",
    "WFD": "Whitefield", "KJM": "Krishnarajapuram", "BNCE": "Bengaluru East", "BNC": "Bengaluru Cantt",
    "SBC": "KSR Bengaluru", "KGI": "Kengeri", "BID": "Bidadi", "RMGM": "Ramanagaram", "CPT": "Channapatna",
    "MAD": "Maddur", "MYA": "Mandya", "PANP": "Pandavapura", "MYS": "Mysuru Jn", "AP": "Ashokapuram",
}

con = sqlite3.connect(DB, check_same_thread=False, timeout=30)
con.execute("PRAGMA journal_mode=WAL")  # the feed's threads write while the site reads; WAL lets both go at once
con.row_factory = sqlite3.Row
con.execute("""CREATE TABLE IF NOT EXISTS positions (
    train_no TEXT PRIMARY KEY, date TEXT, station TEXT, seq INTEGER, delay REAL, updated_at TEXT)""")
con.execute("""CREATE TABLE IF NOT EXISTS predictions (
    train_no TEXT, date TEXT, made_at TEXT, at_station TEXT, delay_now REAL,
    station TEXT, pred_delay REAL, lo REAL, hi REAL, model TEXT)""")
# Another app's forecast for the same moment (made_at = the position it was made from), so the two
# can be scored side by side once the train arrives. Filled by live/feed.py.
con.execute("""CREATE TABLE IF NOT EXISTS rivals (
    train_no TEXT, date TEXT, made_at TEXT, station TEXT, pred_delay REAL, source TEXT,
    PRIMARY KEY (train_no, date, made_at, station, source))""")
con.commit()


def now() -> datetime:
    return datetime.now(IST)


def running(no: str | None = None) -> list[sqlite3.Row]:
    """Positions reported within GONE_QUIET_H. The feed never deletes a finished run's position,
    so this is what keeps yesterday's trains off the live views."""
    cutoff = (now() - timedelta(hours=GONE_QUIET_H)).isoformat(timespec="seconds")
    return con.execute("SELECT * FROM positions WHERE updated_at > ? AND train_no = COALESCE(?, train_no) "
                       "ORDER BY train_no", (cutoff, no)).fetchall()


def train_or_404(no: str) -> list[tuple]:
    if no not in NAMES:
        abort(404, f"unknown train {no}")
    return route(no)


def seq_of(stops: list[tuple], station: str) -> int:
    """Station code -> its seq on this train's route; 400 if the train doesn't halt there (or it's the last stop)."""
    for seq, code, *_ in stops[:-1]:
        if code == station.upper():
            return seq
    abort(400, f"train does not halt at {station} before its last stop")


def delay_arg(value) -> float:
    try:
        d = float(value)
    except (TypeError, ValueError):
        abort(400, "delay must be a number of minutes")
    if not -120 <= d <= 600:
        abort(400, "delay must be between -120 and 600 minutes")
    return d


def date_arg(value) -> str:
    if value is None:
        return now().date().isoformat()
    try:
        return Date.fromisoformat(value).isoformat()
    except ValueError:
        abort(400, "date must be YYYY-MM-DD (the day the train left its origin)")


def reported_arg(value) -> str:
    """When the train was seen there (ISO 8601 with offset); defaults to now. Future times are refused."""
    if value is None:
        return now().isoformat(timespec="seconds")
    try:
        t = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        abort(400, "reported_at must be ISO 8601, e.g. 2026-10-26T19:43:00+05:30")
    if t.tzinfo is None or t > now() + timedelta(minutes=5):
        abort(400, "reported_at needs a timezone offset and cannot be in the future")
    return t.astimezone(IST).isoformat(timespec="seconds")


def etas(no: str, at_seq: int, delay: float, run_date: str, at_time: str | None = None,
         explain: bool = True) -> list[dict]:
    """Predictions with full timestamps: run date + scheduled day offset + time + delay.
    at_time: when the train was seen there (ISO); the congestion inputs look back from it. Default now."""
    out = []
    for p in predict(no, at_seq, delay, run_date=run_date, at_time=at_time, explain=explain):
        sch = datetime.fromisoformat(f"{run_date}T{p['sch']}").replace(tzinfo=IST) + timedelta(days=p["day"])
        at = lambda d: None if d is None else (sch + timedelta(minutes=d)).isoformat(timespec="minutes")  # noqa: E731
        out.append({"station": p["station"], "name": STATIONS.get(p["station"], p["station"]),
                    "scheduled": sch.isoformat(timespec="minutes"), "eta": at(p["delay"]),
                    "eta_range": [at(p["lo"]), at(p["hi"])] if p["lo"] is not None else None,
                    "delay_min": p["delay"], "delay_lo": p["lo"], "delay_hi": p["hi"],
                    "p10": at(p["lo"]) if p["lo"] is not None else None, "p50": at(p["delay"]),
                    "p90": at(p["hi"]) if p["hi"] is not None else None,
                    "change_min": round(p["delay"] - delay), "similar_runs": p["runs"], "method": p["method"],
                    "why": [{"reason": r, "minutes": m} for r, m in p["why"]]})
    return out


HELD_GRACE_MIN = 15  # past the next station's P90 by this much, with no report: flag an unscheduled halt


def held(at: str, upcoming: list[dict]) -> dict | None:
    """Unscheduled halt: the train should have reached its next station by now (even a slow run, P90,
    plus HELD_GRACE_MIN) but has not reported there. Likely held at a signal, for a crossing, or by a fault."""
    if not upcoming:
        return None
    nxt = upcoming[0]
    over = (now() - datetime.fromisoformat(nxt["p90"] or nxt["eta"])).total_seconds() / 60
    return {"between": [at, nxt["station"]], "minutes_over": round(over)} if over > HELD_GRACE_MIN else None


def train_view(no: str, pos: sqlite3.Row, explain: bool = True) -> dict:
    age = (now() - datetime.fromisoformat(pos["updated_at"])).total_seconds() / 60
    ahead = etas(no, pos["seq"], pos["delay"], pos["date"], pos["updated_at"], explain)
    return {"train": no, "name": NAMES[no], "date": pos["date"], "at": pos["station"], "delay_min": pos["delay"],
            "updated_at": pos["updated_at"], "minutes_since_update": round(age), "stale": age > STALE_MIN,
            "held": held(pos["station"], ahead), "etas": ahead}


@api.errorhandler(400)
@api.errorhandler(404)
def error(e):
    return jsonify(error=e.description), e.code


@api.get("/trains")
def trains():
    return jsonify([{"train": no, "name": name,
                     "halts": [{"station": s, "name": STATIONS.get(s, s), "scheduled": t, "day": d}
                               for _, s, t, d in route(no)]}
                    for no, name in TRAINS.items()])


@api.get("/trains/<no>/eta")
def what_if(no):
    stops = train_or_404(no)
    at = request.args.get("at") or abort(400, "at=<station code> is required")
    delay = delay_arg(request.args.get("delay", 0))
    return jsonify(train=no, name=NAMES[no], at=at.upper(), delay_min=delay,
                   etas=etas(no, seq_of(stops, at), delay, date_arg(request.args.get("date"))))


@api.post("/trains/<no>/position")
def report(no):
    stops = train_or_404(no)
    body = request.get_json(silent=True) or abort(400, "send JSON: {station, delay, date?}")
    station = str(body.get("station") or abort(400, "station is required"))
    seq, delay, run_date = seq_of(stops, station), delay_arg(body.get("delay")), date_arg(body.get("date"))
    seen = reported_arg(body.get("reported_at"))
    before = con.execute("SELECT date, station, delay FROM positions WHERE train_no=?", (no,)).fetchone()
    con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?)", (no, run_date, station.upper(), seq, delay, seen))
    view = train_view(no, con.execute("SELECT * FROM positions WHERE train_no=?", (no,)).fetchone())
    # Log the forecast once per new position (the feed re-sends unchanged ones every 5 min).
    if before is None or tuple(before) != (run_date, station.upper(), delay):
        m = model()
        con.executemany("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(no, run_date, seen, station.upper(), delay, e["station"], e["delay_min"], e["delay_lo"],
                          e["delay_hi"], m["name"] if m else "similar_runs") for e in view["etas"]])
    con.commit()
    return jsonify(view), 201


@api.get("/trains/<no>/live")
def live(no):
    train_or_404(no)
    pos = con.execute("SELECT * FROM positions WHERE train_no=?", (no,)).fetchone()
    if pos is None:
        abort(404, f"no position reported for {no} yet")
    return jsonify(train_view(no, pos))


SECTION_DAYS = 90


def sections(no: str) -> list[dict]:
    """Each stretch between consecutive halts: timetabled running time against the real one over the
    last SECTION_DAYS. slack_min > 0: the timetable allows more than trains usually need, so a late
    train can win time back there; < 0: trains usually lose time there."""
    stops = route(no)
    seqs = [s[0] for s in stops]
    since = con.execute(f"SELECT date(MAX(date), '-{SECTION_DAYS} day') FROM runs WHERE train_no=?", (no,)).fetchone()[0]
    runs: dict = {}
    for d, seq, sa, sd, aa, ad in con.execute(
            f"""SELECT date, seq, sch_arr, sch_dep, act_arr, act_dep FROM runs
                WHERE train_no=? AND date>=? AND seq IN ({",".join("?" * len(seqs))})""", (no, since, *seqs)):
        runs.setdefault(d, {})[seq] = (sa, sd, aa, ad)
    mins = lambda a, b: (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60  # noqa: E731
    out = []
    for (a, a_stn, *_), (b, b_stn, *_) in zip(stops, stops[1:]):
        sch, real = None, []
        for r in runs.values():
            if a in r and b in r:
                (_, a_sd, _, a_ad), (b_sa, _, b_aa, _) = r[a], r[b]
                sch = mins(r[a][1] or r[a][0], b_sa or r[b][1])
                if a_ad and b_aa and 0 < mins(a_ad, b_aa) < 3 * sch + 60:
                    real.append(mins(a_ad, b_aa))
        if sch is None or len(real) < 10:
            continue
        typical = median(real)
        out.append({"from": a_stn, "to": b_stn, "scheduled_min": round(sch), "typical_min": round(typical),
                    "slow_min": round(quantiles(real, n=10)[-1]), "slack_min": round(sch - typical), "runs": len(real)})
    return out


@api.get("/trains/<no>/sections")
def train_sections(no):
    train_or_404(no)
    return jsonify(train=no, name=NAMES[no], days=SECTION_DAYS, sections=sections(no))


def station_board(code: str) -> list[dict]:
    """Every running train still to reach this station, soonest first: what a station display shows."""
    code, board = code.upper(), []
    for pos in running():
        no = pos["train_no"]
        if no not in NAMES or code not in [c for s, c, *_ in route(no) if s > pos["seq"]]:
            continue  # skip the prediction for trains that never come here
        for e in etas(no, pos["seq"], pos["delay"], pos["date"], pos["updated_at"], explain=False):
            if e["station"] == code:
                board.append({"train": no, "name": NAMES[no], "now_at": pos["station"],
                              "updated_at": pos["updated_at"],
                              **{k: e[k] for k in ("scheduled", "eta", "eta_range", "p10", "p50", "p90",
                                                   "delay_min", "delay_lo", "delay_hi")}})
    return sorted(board, key=lambda a: a["eta"])


@api.get("/stations/<code>/arrivals")
def arrivals(code):
    return jsonify(station=code.upper(), name=STATIONS.get(code.upper(), code.upper()), arrivals=station_board(code))


DELAYED_MIN, DISRUPTED_MIN = 15, 60  # status cut-offs, in minutes late


def status(delay_now: float, delay_end: float | None) -> str:
    """normal / delayed / disrupted from the worse of how late the train is now and how late
    it is predicted to finish, so a train about to lose time is flagged before it does."""
    worst = max(delay_now, delay_now if delay_end is None else delay_end)
    return "disrupted" if worst >= DISRUPTED_MIN else "delayed" if worst >= DELAYED_MIN else "normal"


SEVERITY = {"disrupted": 0, "delayed": 1, "normal": 2}


def control_rows() -> list[dict]:
    """Every running train, worst status first."""
    out = []
    for pos in running():
        if pos["train_no"] not in NAMES:
            continue
        v = train_view(pos["train_no"], pos, explain=False)  # the overview never shows reasons
        last = v["etas"][-1] if v["etas"] else None
        out.append({k: v[k] for k in ("train", "name", "at", "delay_min", "minutes_since_update", "stale", "held")} | {
            "last_station": last and last["station"], "delay_there_min": last and last["delay_min"],
            "trend": None if not last else "recovering" if last["change_min"] <= -3 else
                     "losing" if last["change_min"] >= 3 else "steady",
            "status": status(v["delay_min"], last and last["delay_min"])})
    return sorted(out, key=lambda t: SEVERITY[t["status"]])


@api.get("/control")
def control():
    return jsonify(trains=control_rows(), generated_at=now().isoformat(timespec="seconds"))


@api.get("/accuracy")
def accuracy():
    return jsonify(report_file("accuracy"))


@api.get("/accuracy/live")
def accuracy_live():
    """The model checking itself: every logged live ETA whose train has since arrived, scored."""
    days = request.args.get("days", default=7, type=int)
    if not 1 <= days <= 365:
        abort(400, "days must be 1..365")
    return jsonify(live_scores(con, days))


@api.get("/models")
def models():
    """Every model trained in Level 9, its validation and test error, and which one is live."""
    return jsonify(report_file("models") or {"error": "no models trained yet: python model/train.py"})
