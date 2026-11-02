"""Level 4: the ETA predictor.

Question it answers: "Train T is at station X, running D minutes late. When will it reach
each station ahead?"

How: look at past runs of the same train that were *about as late* at X (the K closest
by delay), see how much their delay changed between X and each later station, and take
the median. A late train finds slack in the timetable and recovers; an on-time train
can't recover what it never lost. Matching on delay captures both.

    python model/eta.py          # demo on one real run the model hasn't seen
"""
import json
import sqlite3
import time
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from statistics import median, quantiles

DB = Path(__file__).parent.parent / "data" / "trackpulse.db"
K = 50            # similar past runs used per prediction
MIN_RUNS = 10     # fewer comparable runs than this -> fall back to "delay stays the same"
SANE = (-120, 600)
FMT = "%Y-%m-%d %H:%M"
IST = timezone(timedelta(hours=5, minutes=30))


@cache
def history(train_no: str, before: str = "9999") -> dict[str, dict[int, tuple[str, float, float]]]:
    """{date: {seq: (station, delay leaving, delay arriving)}} for runs that started before `before`."""
    con = sqlite3.connect(DB)
    out: dict = {}
    for date, seq, stn, arr, dep in con.execute(
            "SELECT date, seq, station, delay_arr, delay_dep FROM runs WHERE train_no=? AND date<?",
            (train_no, before)):
        leave = dep if dep is not None else arr
        reach = arr if arr is not None else dep
        if leave is None or not (SANE[0] <= leave <= SANE[1] and SANE[0] <= reach <= SANE[1]):
            continue
        out.setdefault(date, {})[seq] = (stn, leave, reach)
    con.close()
    return out


@cache
def route(train_no: str) -> list[tuple[int, str, str]]:
    """Scheduled halts in order: (seq, station, 'HH:MM' scheduled arrival), from the latest run's timetable."""
    con = sqlite3.connect(DB)
    rows = con.execute("""SELECT seq, station, sch_arr, sch_dep,
                                 CAST(julianday(substr(COALESCE(sch_arr, sch_dep), 1, 10)) - julianday(date) AS INTEGER)
                          FROM runs
                          WHERE train_no=? AND halts=1 AND seq IN (
                              SELECT seq FROM runs WHERE train_no=? AND date >= date((SELECT MAX(date) FROM runs WHERE train_no=?), '-90 day')
                              GROUP BY seq HAVING COUNT(*) >= 10)
                          GROUP BY seq HAVING date = MAX(date) ORDER BY seq""",
                       (train_no, train_no, train_no)).fetchall()
    if not rows:
        # Cold start: a new, special or diverted train with fewer than 10 runs. Use its latest timetable;
        # the model then predicts from how all trains behave on these stretches, from the first run.
        rows = con.execute("""SELECT seq, station, sch_arr, sch_dep,
                                     CAST(julianday(substr(COALESCE(sch_arr, sch_dep), 1, 10)) - julianday(date) AS INTEGER)
                              FROM runs WHERE train_no=? AND halts=1 AND COALESCE(sch_arr, sch_dep) IS NOT NULL
                                AND date = (SELECT MAX(date) FROM runs WHERE train_no=?) ORDER BY seq""",
                           (train_no, train_no)).fetchall()
    con.close()
    return [(seq, stn, (arr or dep)[11:], day) for seq, stn, arr, dep, day in rows]


@cache
def pairs(train_no: str, at_seq: int, seq: int, before: str = "9999") -> list[tuple[float, float]]:
    """(delay leaving at_seq, delay reaching seq) for past runs that reported at both, sorted by the first."""
    return sorted((r[at_seq][1], r[seq][2]) for r in history(train_no, before).values() if at_seq in r and seq in r)


def nearest(both: list[tuple[float, float]], delay: float, k: int) -> list[tuple[float, float]]:
    """The k pairs whose starting delay is closest to `delay` (both is sorted by it)."""
    i = bisect_left(both, (delay,))
    window = both[max(0, i - k):i + k]
    return sorted(window, key=lambda p: abs(p[0] - delay))[:k]


def knn(train_no: str, at_seq: int, seq: int, delay_now: float, before: str = "9999") -> tuple[float, float, float, int]:
    """Similar past runs' delay change at_seq -> seq: (median, 10th pct, 90th pct, runs used).
    Too few comparable runs -> (0, 0, 0, n): "delay stays the same"."""
    both = pairs(train_no, at_seq, seq, before)
    if len(both) < MIN_RUNS:
        return 0.0, 0.0, 0.0, len(both)
    changes = [reach - leave for leave, reach in nearest(both, delay_now, K)]
    p = quantiles(changes, n=10)
    return median(changes), p[0], p[-1], len(changes)


RECENT_DAYS = 14


def recent(train_no: str, at_seq: int, seq: int, run_date: str) -> tuple[float, int]:
    """How this train's delay changed at_seq -> seq over the last RECENT_DAYS days before run_date:
    (median change, runs). Picks up what history can't: a new speed restriction, a maintenance
    block, monsoon running. (0, 0) when there are no recent runs."""
    runs = history(train_no)
    d = datetime.strptime(run_date, "%Y-%m-%d")
    changes = []
    for i in range(1, RECENT_DAYS + 1):
        r = runs.get((d - timedelta(days=i)).strftime("%Y-%m-%d"))
        if r and at_seq in r and seq in r:
            changes.append(r[seq][2] - r[at_seq][1])
    return (median(changes), len(changes)) if changes else (0.0, 0)


@cache
def timing(train_no: str) -> dict[int, tuple[int, float, int]]:
    """seq -> (scheduled minutes from the run's start day 00:00, km from origin, halt index) for route halts."""
    con = sqlite3.connect(DB)
    km = dict(con.execute("SELECT seq, AVG(km) FROM runs WHERE train_no=? GROUP BY seq", (train_no,)))
    con.close()
    return {seq: (day * 1440 + int(sch[:2]) * 60 + int(sch[3:]), km.get(seq) or 0.0, i)
            for i, (seq, _, sch, day) in enumerate(route(train_no))}


# Append only: the position is the model's code for the train. Must match api.TRAINS (tests check).
TRAIN_CODES = ["12007", "12008", "12027", "12028", "12607", "12608", "12639", "12640",
               "12657", "12658", "16021", "16022", "22625", "22626",
               "12951", "12952", "12953", "12954", "12903", "12904", "12925", "12926",
               "12301", "12302", "12313", "12314", "12311", "12312", "12423", "12424",
               "12621", "12622", "12615", "12616", "12625", "12626", "12627", "12628", "12723", "12724",
               "12809", "12810", "12859", "12860", "12163", "12164"]
FEATURES = ["delay_now", "knn_change", "knn_lo", "knn_hi", "knn_n", "mins_ahead", "km_ahead", "hops_ahead",
            "target_hour", "weekday", "month", "train", "at_seq", "seq", "recent_change", "recent_n",
            "traffic_n", "traffic_delay", "traffic_gain", "rain_mm", "tmin"]
NAN = float("nan")


# ---- congestion: what other trains are doing on the track ahead, right now -----------------

TRAFFIC_KM, TRAFFIC_MIN = 200, 120  # look this far ahead along the route, this far back in time
EPOCH = datetime(2000, 1, 1)


def minute_of(stamp) -> float:
    """'YYYY-MM-DD HH:MM' or a datetime, in IST -> minutes since 2000-01-01 IST."""
    t = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
    if t.tzinfo:
        t = t.astimezone(IST)
    return (t.replace(tzinfo=None) - EPOCH).total_seconds() / 60


@cache
def reports_on(run_date: str) -> dict[str, list[tuple[float, str, float, float]]]:
    """Every report from runs that started on run_date: station -> [(minute seen, train, delay, gain)].
    gain = delay change since that run's previous report (positive: it is losing time)."""
    con = sqlite3.connect(DB)
    out: dict = {}
    prev: dict = {}
    for no, stn, aa, ad, da, dd in con.execute(
            """SELECT train_no, station, act_arr, act_dep, delay_arr, delay_dep FROM runs
               WHERE date=? ORDER BY train_no, seq""", (run_date,)):
        seen, delay = aa or ad, da if da is not None else dd
        if seen is None or delay is None or not SANE[0] <= delay <= SANE[1]:
            continue
        out.setdefault(stn, []).append((minute_of(seen), no, delay, delay - prev.get(no, delay)))
        prev[no] = dd if dd is not None and SANE[0] <= dd <= SANE[1] else delay
    con.close()
    return out


@cache
def traffic(train_no: str, at_seq: int, at_minute: float) -> tuple[float, float, float]:
    """Other trains seen at this train's halts in the next TRAFFIC_KM, in the last TRAFFIC_MIN minutes:
    (how many reports, their median delay, their median gain since their previous stop).
    Trains ahead losing time = congestion. Uses only reports made before at_minute, so training
    and live prediction see exactly what was knowable at the time."""
    t = timing(train_no)
    km0 = t[at_seq][1]
    ahead = {stn for seq, stn, *_ in route(train_no) if seq > at_seq and t[seq][1] - km0 <= TRAFFIC_KM}
    day = EPOCH + timedelta(minutes=at_minute)
    seen = [(d, g) for k in range(4) for stn in ahead  # runs that started up to 3 days ago can be there now
            for m, no, d, g in reports_on((day - timedelta(days=k)).strftime("%Y-%m-%d")).get(stn, ())
            if no != train_no and at_minute - TRAFFIC_MIN <= m <= at_minute]
    if not seen:
        return 0, NAN, NAN
    return len(seen), median(d for d, _ in seen), median(g for _, g in seen)


# ---- weather: rain and coldest temperature (a fog proxy) where the train is heading ---------

def cell(lat: float, lng: float) -> str:
    """Half-degree grid square (~50 km): stations close together share one weather lookup."""
    return f"{round(lat * 2) / 2:.1f},{round(lng * 2) / 2:.1f}"


@cache
def weather() -> dict[tuple[str, str], tuple[float, float]]:
    """(cell, date) -> (rain mm, min temperature C), filled by live/weather.py. Empty if not fetched yet."""
    con = sqlite3.connect(DB)
    try:
        return {(c, d): (r, t) for c, d, r, t in con.execute("SELECT cell, date, rain_mm, tmin FROM weather")}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


@cache
def station_cells(train_no: str) -> dict[int, str]:
    """seq -> weather cell, for every stop of this train with a known position."""
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT seq, AVG(lat), AVG(lng) FROM runs WHERE train_no=? AND lat IS NOT NULL GROUP BY seq",
                       (train_no,)).fetchall()
    con.close()
    return {seq: cell(lat, lng) for seq, lat, lng in rows}


def features(train_no: str, at_seq: int, seq: int, delay_now: float, run_date: str, k: tuple,
             recent_ok: bool = True, at_minute: float | None = None) -> list[float]:
    """One model input row, in FEATURES order. `k` is knn()'s output for the same pair.
    recent_ok=False blanks the recent-runs inputs (training uses it so the model copes when they're missing).
    at_minute: when the train was at at_seq (minute_of), for the congestion inputs; None = unknown.
    Shared by training (model/train.py) and prediction, so both see exactly the same inputs."""
    t = timing(train_no)
    (m0, km0, i0), (m1, km1, i1) = t[at_seq], t[seq]
    d = datetime.strptime(run_date, "%Y-%m-%d")
    code = TRAIN_CODES.index(train_no) if train_no in TRAIN_CODES else -1
    arrive_day = (d + timedelta(days=m1 // 1440)).strftime("%Y-%m-%d")
    wx = weather().get((station_cells(train_no).get(seq), arrive_day), (NAN, NAN))
    return [delay_now, k[0], k[1], k[2], k[3], m1 - m0, km1 - km0, i1 - i0,
            (m1 % 1440) // 60, d.weekday(), d.month, code, at_seq, seq,
            *(recent(train_no, at_seq, seq, run_date) if recent_ok else (0.0, 0)),
            *(traffic(train_no, at_seq, at_minute) if at_minute is not None else (0, NAN, NAN)), *wx]


MODEL_FILE = DB.parent / "model.joblib"


class Blend:
    """Mean of several fitted models' predictions. Lives here, not in train.py, so the saved
    model unpickles wherever eta is imported."""
    def __init__(self, models):
        self.models = models

    def predict(self, X):
        return sum(m.predict(X) for m in self.models) / len(self.models)


@cache
def model():
    """The best model chosen by model/train.py, or None (then the plain similar-runs median is used)."""
    if not MODEL_FILE.exists():
        return None
    import joblib
    return joblib.load(MODEL_FILE)


REASONS = {  # plain-language groups of FEATURES, for "why this prediction"
    "how late it is now": ["delay_now"],
    "similar past runs": ["knn_change", "knn_lo", "knn_hi", "knn_n"],
    "last 14 days on this stretch": ["recent_change", "recent_n"],
    "distance to go": ["mins_ahead", "km_ahead", "hops_ahead"],
    "time of day": ["target_hour"],
    "day and season": ["weekday", "month"],
    "this train and section": ["train", "at_seq", "seq"],
    "trains on the track ahead": ["traffic_n", "traffic_delay", "traffic_gain"],
    "weather ahead": ["rain_mm", "tmin"],
}


def reasons(m, rows: list[list[float]], top: int = 2) -> list[list[tuple[str, int]]]:
    """Per row, the biggest pushes on the predicted delay change: [(reason, minutes), ...].
    SHAP values from LightGBM's built-in TreeSHAP (pred_contrib), summed per REASONS group.
    ponytail: explains only the LightGBM half of the blend; HistGradientBoosting has no
    TreeSHAP, add the `shap` package if the blend's other half must be explained too."""
    lgbm = next((x for x in getattr(m, "models", [m]) if hasattr(x, "booster_")), None)
    if lgbm is None:
        return [[] for _ in rows]
    idx = {f: i for i, f in enumerate(FEATURES)}
    out = []
    for c in lgbm.predict(rows, pred_contrib=True):
        pushes = [(name, round(sum(c[idx[f]] for f in fs))) for name, fs in REASONS.items()]
        out.append(sorted([p for p in pushes if p[1]], key=lambda p: -abs(p[1]))[:top])
    return out


# ---- confidence range: P10 / P50 / P90 -------------------------------------------------------
# P50 is the prediction. P10 and P90 come from how wrong the model really was on runs it never saw
# (model/backtest.py), per train and per how far ahead the station is, so 8 in 10 real arrivals
# land inside P10..P90 by construction. Similar-run spread is the fallback before the first backtest.
BAND_MIN = 50  # errors needed before a train gets its own band; else the all-trains band


def band_bucket(minutes_ahead: float) -> str:
    return next(b for b, top in (("0-1h", 60), ("1-3h", 180), ("3-8h", 480), ("8-24h", 1440), ("24h+", 1e9))
                if minutes_ahead <= top)


@cache
def bands() -> dict:
    try:
        return json.loads((DB.parent / "accuracy.json").read_text()).get("bands", {})
    except FileNotFoundError:
        return {}


def band(train_no: str, minutes_ahead: float) -> list[float] | None:
    b = band_bucket(minutes_ahead)
    return bands().get(f"{train_no}|{b}") or bands().get(f"all|{b}")


REFRESH_S = 900
_loaded = [time.monotonic()]


def refresh():
    """Drop the in-memory history and model every REFRESH_S, so a long-running server sees the
    runs the feed keeps saving and the model the daily relearn writes, without a restart.
    ponytail: the first prediction after a refresh reloads history (~3 s); fine for ~50 trains."""
    if time.monotonic() - _loaded[0] > REFRESH_S:
        for f in (history, pairs, route, timing, model, reports_on, traffic, weather, station_cells, bands):
            f.cache_clear()
        _loaded[0] = time.monotonic()


def predict(train_no: str, at_seq: int, delay_now: float, before: str = "9999", run_date: str | None = None,
            at_time=None, explain: bool = True) -> list[dict]:
    """Predicted delay at every scheduled halt after `at_seq`, using only runs before `before`.
    The delay change comes from the trained model (Level 9) when there is one, else from similar
    past runs (Level 4). `lo`/`hi`: the band 8 in 10 similar past runs fell within, centred on the
    prediction. `day`: the scheduled arrival's day offset from the run's start date.
    at_time: when the train was at at_seq ('YYYY-MM-DD HH:MM' or datetime, IST), for the
    congestion inputs; default now. explain=False skips the per-station reasons (faster, for testing)."""
    refresh()
    run_date = run_date or datetime.now(IST).strftime("%Y-%m-%d")
    at_minute = minute_of(at_time or datetime.now(IST))
    ahead = [(seq, stn, sch, day) for seq, stn, sch, day in route(train_no) if seq > at_seq]
    ks = [knn(train_no, at_seq, seq, delay_now, before) for seq, *_ in ahead]
    # Routing (explained on the website's How it works page):
    #   trained model + train standing at a timetable halt -> "model"
    #   else enough similar past runs                       -> "similar_runs"
    #   else                                                -> "carried" (current delay carried forward)
    m = model()
    # The model only knows timetable halts, and only the inputs it was trained on.
    use_model = bool(m and m["features"] == FEATURES and ahead and at_seq in timing(train_no))
    if use_model:
        rows = [features(train_no, at_seq, seq, delay_now, run_date, k, at_minute=at_minute)
                for (seq, *_), k in zip(ahead, ks)]
        changes = [float(c) for c in m["model"].predict(rows)]
        whys = reasons(m["model"], rows) if explain else [[] for _ in ahead]
    else:
        changes = [k[0] for k in ks]
        whys = [[] for _ in ahead]
    out = []
    t = timing(train_no)
    for (seq, stn, sch, day), k, change, why in zip(ahead, ks, changes, whys):
        enough = k[3] >= MIN_RUNS
        p50 = delay_now + change
        b = band(train_no, t[seq][0] - t[at_seq][0]) if seq in t and at_seq in t else None
        lo, hi = (p50 + b[0], p50 + b[1]) if b else (p50 + k[1] - k[0], p50 + k[2] - k[0]) if enough else (None, None)
        out.append({"seq": seq, "station": stn, "sch": sch, "day": day, "delay": round(p50),
                    "lo": None if lo is None else round(min(lo, p50)),
                    "hi": None if hi is None else round(max(hi, p50)), "runs": k[3],
                    "method": "model" if use_model else "similar_runs" if enough else "carried", "why": why})
    return out


def eta(sch_hhmm: str, delay: float) -> str:
    """'18:48' + 12 min -> '19:00'."""
    return (datetime.strptime(sch_hhmm, "%H:%M") + timedelta(minutes=delay)).strftime("%H:%M")


def demo(train_no="16021", date=None, at_station="KPD"):
    """Replay one real run: stand at `at_station`, predict the rest, compare with what happened."""
    con = sqlite3.connect(DB)
    date = date or con.execute("SELECT MAX(date) FROM runs WHERE train_no=? AND station=?",
                               (train_no, at_station)).fetchone()[0]
    actual = {seq: (dep if dep is not None else arr, arr if arr is not None else dep) for seq, arr, dep in con.execute(
        "SELECT seq, delay_arr, delay_dep FROM runs WHERE train_no=? AND date=?", (train_no, date))}
    at_seq = con.execute("SELECT seq FROM runs WHERE train_no=? AND date=? AND station=?",
                         (train_no, date, at_station)).fetchone()[0]
    con.close()
    now = actual[at_seq][0]
    print(f"{train_no} on {date}: at {at_station}, {now:+.0f} min late. Predicting the rest "
          f"from runs before {date} only.\n")
    print(f"{'station':>8} {'sched':>6} {'ours':>6} {'naive':>6} {'real':>6}   err ours / naive (min)")
    for p in predict(train_no, at_seq, now, before=date, run_date=date):
        real = actual.get(p["seq"], (None, None))[1]
        r = f"{eta(p['sch'], real):>6}   {abs(p['delay'] - real):4.0f} / {abs(now - real):4.0f}" if real is not None else "     -"
        print(f"{p['station']:>8} {p['sch']:>6} {eta(p['sch'], p['delay']):>6} {eta(p['sch'], now):>6} {r}")


if __name__ == "__main__":
    assert eta("23:50", 20) == "00:10"
    demo()
