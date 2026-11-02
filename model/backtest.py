"""Level 5: how accurate is the predictor, on runs it has never seen?

Split by time: the model learns from runs before TEST_FROM and is tested on every run from
then on. For each test run, stand at every reported station in turn, predict every later
reported station, and compare with what actually happened. Three predictors are scored:

    ours       - model/eta.py (similar past runs, recovery-aware)
    naive      - "the delay stays the same" (what a running-late display implies)
    timetable  - "the train arrives on time" (the printed schedule)

    python model/backtest.py     # prints the scores, writes data/accuracy.json
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).parent))
from eta import BAND_MIN, DB, SANE, band_bucket, predict  # noqa: E402
from statistics import quantiles  # noqa: E402

TEST_DAYS = 90
OUT = DB.parent / "accuracy.json"
RECOVERS = 5  # minutes won back between two stations that count as "the train recovered here"


def minutes(a: str, b: str) -> float:
    """Minutes from 'YYYY-MM-DD HH:MM' a to b."""
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60


def bucket(minutes_ahead: float) -> str:
    return "0-30 min ahead" if minutes_ahead <= 30 else "30-90 min ahead" if minutes_ahead <= 90 else "90+ min ahead"


def bands_from(resid: dict) -> dict:
    """Errors -> [P10, P90] offsets around the prediction, where there are enough of them."""
    out = {}
    for key, errs in resid.items():
        if len(errs) >= BAND_MIN:
            q = quantiles(errs, n=10)
            out[key] = [round(q[0], 1), round(q[-1], 1)]
    return out


def recovery(legs: dict) -> dict:
    """Per train: of the stretches where it really won back RECOVERS+ minutes, how many we predicted
    (caught), how often a predicted recovery came true (right), and the stretches where it recovers most."""
    out: dict = {}
    for (t, a, b), xs in legs.items():
        o = out.setdefault(t, {"real": [], "said": [], "stretches": []})
        o["real"] += [g for g in xs if g[0] >= RECOVERS]
        o["said"] += [g for g in xs if g[1] >= RECOVERS]
        if len(xs) >= 10:
            o["stretches"].append({"from": a, "to": b, "runs": len(xs), "real": round(mean(r for r, _ in xs), 1),
                                   "predicted": round(mean(p for _, p in xs), 1)})
    for t, o in out.items():
        real, said = o.pop("real"), o.pop("said")
        o |= {"recoveries": len(real),
              "caught": round(mean(p >= RECOVERS for _, p in real), 3) if real else None,
              "right": round(mean(r >= RECOVERS for r, _ in said), 3) if said else None,
              "real_avg": round(mean(r for r, _ in real), 1) if real else None,
              "predicted_avg": round(mean(p for _, p in real), 1) if real else None}
        o["stretches"] = sorted(o["stretches"], key=lambda s: -s["real"])[:5]
    return out


def main():
    con = sqlite3.connect(DB)
    test_from = con.execute(f"SELECT date(MAX(date), '-{TEST_DAYS} day') FROM runs").fetchone()[0]
    rows = con.execute("""SELECT train_no, date, seq, station, sch_arr, sch_dep, delay_arr, delay_dep, COALESCE(act_dep, act_arr)
                          FROM runs WHERE date >= ? ORDER BY train_no, date, seq""", (test_from,)).fetchall()
    con.close()

    runs = defaultdict(list)
    for t, d, seq, stn, sa, sd, da, dd, left in rows:
        leave, reach = (dd if dd is not None else da), (da if da is not None else dd)
        if leave is not None and SANE[0] <= leave <= SANE[1] and SANE[0] <= reach <= SANE[1]:
            runs[(t, d)].append((seq, stn, sd or sa, sa or sd, leave, reach, left))

    err = defaultdict(lambda: defaultdict(list))  # group -> predictor -> abs errors
    # Recovery: on each stretch between consecutive halts, minutes really won back vs minutes we said
    # would be won back. Carrying the delay forward always says 0, so it never spots a recovery.
    legs = defaultdict(list)  # (train, from, to) -> [(real gain, predicted gain)]
    resid = defaultdict(list)  # "train|bucket" and "all|bucket" -> real minus predicted delay
    for (t, d), stops in runs.items():
        for i, (at_seq, at_stn, at_sch, _, leave, _, left) in enumerate(stops[:-1]):
            if not left:
                continue
            pred = {p["seq"]: p["delay"] for p in predict(t, at_seq, leave, before=test_from, run_date=d,
                                                          at_time=left, explain=False)}
            first = True
            for seq, stn, _, sch_reach, _, reach, _ in stops[i + 1:]:
                if seq not in pred:
                    continue  # not a scheduled halt in the route
                if first:
                    legs[(t, at_stn, stn)].append((leave - reach, leave - pred[seq]))
                    first = False
                ahead = minutes(at_sch, sch_reach)  # multi-day trains: can be more than a day
                for key in (f"{t}|{band_bucket(ahead)}", f"all|{band_bucket(ahead)}"):
                    resid[key].append(reach - pred[seq])
                for group in ("all", t, bucket(ahead)):
                    e = err[group]
                    e["ours"].append(abs(pred[seq] - reach))
                    e["naive"].append(abs(leave - reach))
                    e["timetable"].append(abs(reach))

    summary = {g: {"predictions": len(e["ours"]), **{k: round(mean(v), 2) for k, v in e.items()},
                   "within_5_min": round(mean(x <= 5 for x in e["ours"]), 3)}
               for g, e in err.items()}
    summary["recovery"] = recovery(legs)
    summary["bands"] = bands_from(resid)
    summary["_meta"] = {"trained_on": f"runs before {test_from}", "tested_on": f"{len(runs)} runs from {test_from}"}
    OUT.write_text(json.dumps(summary, indent=2))

    print(f"Trained on runs before {test_from}; tested on {len(runs)} unseen runs.\n")
    print(f"{'':18} {'predictions':>11} {'ours':>6} {'naive':>6} {'timetable':>9} {'ours<=5min':>10}   (mean abs error, min)")
    order = ["all", "0-30 min ahead", "30-90 min ahead", "90+ min ahead"] + sorted(g for g in err if g.isdigit())
    for g in order:
        s = summary[g]
        print(f"{g:18} {s['predictions']:>11} {s['ours']:>6} {s['naive']:>6} {s['timetable']:>9} {s['within_5_min']:>10.0%}")

    assert bucket(10) == "0-30 min ahead" and bucket(120) == "90+ min ahead"
    b = bands_from({"1|0-1h": list(range(-50, 50)), "2|0-1h": [1, 2]})
    assert b == {"1|0-1h": [-40.9, 39.9]} and band_bucket(30) == "0-1h" and band_bucket(2000) == "24h+", b
    r = recovery({("1", "A", "B"): [(10, 8)] * 6 + [(0, 6)] * 4})["1"]
    assert (r["recoveries"], r["caught"], r["right"], r["stretches"][0]["real"]) == (6, 1.0, 0.6, 6.0), r
    assert minutes("2026-10-20 23:50", "2026-10-22 00:20") == 1470


if __name__ == "__main__":
    main()
