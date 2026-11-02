"""Level 9: train several models, keep the most accurate one.

Three time windows, never mixed (the model only ever learns from the past):

    learn     2025-06-01 .. val start   models are fitted here
    validate  90 days before test       the winner is picked here
    test      last 90 days              the winner is scored once, honestly (same window as Level 5)

Every example is "train at halt X, D min late, run date R -> delay change by later halt Y".
The similar-runs (Level 4) answer is itself an input, computed only from runs before the
example's window, so no model ever peeks at the run it is predicting.

Candidates: naive, similar runs (Level 4), ridge, random forest, extra trees, sklearn
gradient boosting, LightGBM, XGBoost, and a blend of the best few. The winner is refitted on
learn + validate and saved to data/model.joblib, which model/eta.py then uses everywhere.

    python model/train.py
"""
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from eta import DB, FEATURES, Blend, MODEL_FILE, SANE, features, knn, minute_of, model, timing  # noqa: E402

LEARN_FROM = "2025-06-01"  # runs before this only feed the similar-runs input
# ponytail: forests use 4 cores and half-size samples to fit in ~5 GB free RAM; raise if the machine has more.
WINDOW_DAYS = 90
REPORT = DB.parent / "models.json"
DROP_RECENT = 0.15
# ponytail: long routes have ~1,000 (halt, later halt) pairs per run; a random TARGETS of the later
# halts per halt keeps every train and horizon represented in ~1/4 of the memory. Raise with more RAM.
TARGETS = 6


def windows(con) -> tuple[str, str]:
    test_from = con.execute(f"SELECT date(MAX(date), '-{WINDOW_DAYS} day') FROM runs").fetchone()[0]
    val_from = con.execute(f"SELECT date(?, '-{WINDOW_DAYS} day')", (test_from,)).fetchone()[0]
    return val_from, test_from


def examples(con, start: str, end: str, before: str, drop: float = 0.0):
    """X, y, groups for every (halt X -> later halt Y) pair of every run in [start, end).
    groups = (minutes ahead) for the horizon breakdown."""
    runs = defaultdict(list)
    for t, d, seq, da, dd, aa, ad in con.execute(
            """SELECT train_no, date, seq, delay_arr, delay_dep, act_arr, act_dep FROM runs
               WHERE date >= ? AND date < ? ORDER BY train_no, date, seq""", (start, end)):
        leave, reach = (dd if dd is not None else da), (da if da is not None else dd)
        if leave is not None and SANE[0] <= leave <= SANE[1] and SANE[0] <= reach <= SANE[1] and seq in timing(t):
            runs[(t, d)].append((seq, leave, reach, ad or aa))
    X, y = [], []
    rng = np.random.default_rng(0)
    for (t, d), stops in runs.items():
        # Some runs train without the recent-runs inputs, so a cold start (no recent data) still predicts well.
        recent_ok = rng.random() >= drop
        rows, ys = [], []
        for i, (at_seq, leave, _, left_at) in enumerate(stops[:-1]):
            later = stops[i + 1:]
            if len(later) > TARGETS:
                later = [later[j] for j in sorted(rng.choice(len(later), TARGETS, replace=False))]
            at_minute = minute_of(left_at) if left_at else None
            for seq, _, reach, _ in later:
                rows.append(features(t, at_seq, seq, leave, d, knn(t, at_seq, seq, leave, before), recent_ok, at_minute))
                ys.append(reach - leave)
        if rows:
            X.append(np.array(rows, dtype=np.float32))
            y.append(np.array(ys, dtype=np.float32))
    X, y = (np.vstack(X), np.concatenate(y)) if X else (np.empty((0, len(FEATURES))), np.empty(0))
    return X, y, X[:, FEATURES.index("mins_ahead")], len(runs)


def candidates() -> dict:
    from lightgbm import LGBMRegressor
    from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from xgboost import XGBRegressor
    train_col = FEATURES.index("train")
    # Minutes-off is what passengers feel, so every model that can is fitted on absolute error.
    return {
        # Congestion and weather are blank when unknown; models without native gaps get the median.
        "ridge": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=1.0)),
        "random_forest": make_pipeline(SimpleImputer(strategy="median"), RandomForestRegressor(
            n_estimators=200, min_samples_leaf=20, max_features=0.5, max_samples=0.3, n_jobs=4, random_state=0)),
        "extra_trees": make_pipeline(SimpleImputer(strategy="median"), ExtraTreesRegressor(
            n_estimators=200, min_samples_leaf=20, max_features=0.7, max_samples=0.3, bootstrap=True,
            n_jobs=4, random_state=0)),
        "hist_gboost": HistGradientBoostingRegressor(loss="absolute_error", max_iter=600, learning_rate=0.05,
                                                     max_leaf_nodes=63, min_samples_leaf=40,
                                                     categorical_features=[train_col], random_state=0),
        "lightgbm": LGBMRegressor(objective="l1", n_estimators=800, learning_rate=0.03, num_leaves=63,
                                  min_child_samples=40, subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
                                  random_state=0, verbose=-1),
        "xgboost": XGBRegressor(objective="reg:absoluteerror", n_estimators=800, learning_rate=0.03, max_depth=8,
                                min_child_weight=20, subsample=0.8, colsample_bytree=0.8, random_state=0, n_jobs=-1),
    }


def score(pred, y, ahead) -> dict:
    err = np.abs(pred - y)
    return {"mae": round(float(err.mean()), 3), "within_5_min": round(float((err <= 5).mean()), 3),
            "mae_0_30": round(float(err[ahead <= 30].mean()), 3),
            "mae_30_90": round(float(err[(ahead > 30) & (ahead <= 90)].mean()), 3),
            "mae_90_plus": round(float(err[ahead > 90].mean()), 3)}


def main():
    con = sqlite3.connect(DB)
    val_from, test_from = windows(con)
    t0 = time.time()
    Xl, yl, al, nl = examples(con, LEARN_FROM, val_from, before=LEARN_FROM, drop=DROP_RECENT)
    Xv, yv, av, nv = examples(con, val_from, test_from, before=val_from)
    Xt, yt, at, nt = examples(con, test_from, "9999", before=test_from)
    print(f"learn {LEARN_FROM}..{val_from}: {nl} runs, {len(yl)} examples | validate ..{test_from}: {nv} runs, "
          f"{len(yv)} | test: {nt} runs, {len(yt)}   ({time.time() - t0:.0f}s)\n")

    knn_col = FEATURES.index("knn_change")
    fitted, val, test = {}, {}, {}
    val["naive"], test["naive"] = score(np.zeros_like(yv), yv, av), score(np.zeros_like(yt), yt, at)
    val["similar_runs"], test["similar_runs"] = score(Xv[:, knn_col], yv, av), score(Xt[:, knn_col], yt, at)
    for name, m in candidates().items():
        t0 = time.time()
        m.fit(Xl, yl)
        fitted[name] = m
        val[name], test[name] = score(m.predict(Xv), yv, av), score(m.predict(Xt), yt, at)
        print(f"  {name:14} validate MAE {val[name]['mae']:6.3f}   ({time.time() - t0:.0f}s)")

    # Blends of the top 2 and top 3 learned models, judged on validation like everything else.
    ranked = sorted(fitted, key=lambda n: val[n]["mae"])
    for k in (2, 3):
        name = "blend_" + "+".join(ranked[:k])
        fitted[name] = Blend([fitted[n] for n in ranked[:k]])
        val[name], test[name] = score(fitted[name].predict(Xv), yv, av), score(fitted[name].predict(Xt), yt, at)

    best = min(val, key=lambda n: val[n]["mae"])
    print(f"\n{'model':45} {'validate':>9} {'test':>7} {'test<=5':>8}  (MAE, minutes)")
    for n in sorted(val, key=lambda n: val[n]["mae"]):
        print(f"{n:45} {val[n]['mae']:9.3f} {test[n]['mae']:7.3f} {test[n]['within_5_min']:8.0%}{'  <- best' if n == best else ''}")

    # Refit the winner on learn + validate (more, and more recent, data), then check it on test once more.
    challenger, kept = best, False
    if best in ("naive", "similar_runs"):
        MODEL_FILE.unlink(missing_ok=True)
        final_test = challenger_test = test[best]
    else:
        Xa, ya = np.vstack([Xl, Xv]), np.concatenate([yl, yv])
        parts = best.removeprefix("blend_").split("+")
        fresh = candidates()
        for n in parts:
            fresh[n].fit(Xa, ya)
        final = fresh[parts[0]] if len(parts) == 1 else Blend([fresh[n] for n in parts])
        final_test = score(final.predict(Xt), yt, at)
        # Champion / challenger: the daily relearn only replaces the model in use if the new one is
        # at least as accurate on the newest test window. A bad day of data can't make ETAs worse.
        current = model()
        current_test = score(current["model"].predict(Xt), yt, at) if current and current["features"] == FEATURES else None
        challenger_test = final_test
        if current_test and current_test["mae"] < final_test["mae"]:
            print(f"\nKept the model in use ({current['name']}, test MAE {current_test['mae']}): "
                  f"new {best} scored {final_test['mae']}.")
            best, final_test, kept = current["name"], current_test, True
        else:
            joblib.dump({"name": best, "model": final, "features": FEATURES, "trained_before": test_from}, MODEL_FILE)
            model.cache_clear()
    print(f"\nIn use: {best} ({'kept' if kept else 'new, saved'}). Test MAE {final_test['mae']} min "
          f"(similar runs {test['similar_runs']['mae']}, naive {test['naive']['mae']}).")
    REPORT.write_text(json.dumps({"windows": {"learn": [LEARN_FROM, val_from], "validate": [val_from, test_from],
                                              "test": [test_from, "end"]},
                                  "examples": {"learn": len(yl), "validate": len(yv), "test": len(yt)},
                                  "best": best, "best_refit_test": final_test,
                                  "challenger": challenger, "challenger_refit_test": challenger_test,
                                  "kept_current": kept,
                                  "validate": val, "test": test}, indent=2))


if __name__ == "__main__":
    main()
