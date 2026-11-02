# Track Pulse

**SIH 2026 · Problem SIH26028 · Dynamic forecast of expected time of arrival (ETA) for coaching trains · Team Tech Sprints**

Track Pulse gives every station ahead of a running train an arrival time with a confidence range, learnt from how
trains really run. Its core idea: *late now doesn't mean late there.* The timetable hides spare time on some
stretches, and late trains use it to win time back; on other stretches they lose more. Track Pulse learns where.

## What it does

| Feature | Where |
|---|---|
| ETA at every station ahead, with **P10 / P50 / P90** (earliest likely, expected, latest likely) | Train page, API |
| **Recovery by section**: where a late train wins back time, and how well we predict it | Train page, home |
| **Cascading delays**: trains ahead that can hold this one up, and trains behind that this one's delay can hold up | Train page |
| **Unscheduled halt alert**: no report although even a slow run would have reached the next station | Home, train page |
| **Regimes**: normal, delayed, disrupted | Home, control API |
| **Reasons** behind each ETA (LightGBM SHAP values) | Train page |
| Weather (Open-Meteo) and congestion (other trains on the next 200 km) as model inputs | Model |
| **Station display board** with expected times and ranges | Stations |
| **Network map** of every line in India (single vs double track) with live trains | Map |
| Any of ~4,700 trains, including specials and new trains (history fetched on first open) | Search |
| **Checks itself**: every live ETA is scored when the train arrives; the model retrains nightly and is only replaced by a better one | Home, How it works |
| Head-to-head with RailRadar's forecast and with "delay carried forward" on the same moments | Home, train page |

## Accuracy (runs the model never saw, last 90 days)

| | Average error |
|---|---|
| Track Pulse | **19.1 min** (3.3 min for arrivals in the next 30 minutes) |
| Delay carried forward | 33.8 min |
| Printed timetable | 60.9 min |

Of 24,001 times a train won back 5+ minutes between two stations, Track Pulse predicted 81%. Carrying the delay
forward predicts none.

## How it works

```
live sources (whereismytrain cache + RailRadar, merged)  ──►  live/feed.py  ──►  runs table (SQLite)
                                                                   │                  │
                                                                   ▼                  ▼
                                               POST /api/trains/<no>/position   model/train.py (nightly)
                                                                   │            LightGBM + HistGB + XGBoost blend,
                                                                   ▼            champion / challenger
                                    app.py (Flask website) + api.py (JSON API) ◄── model/eta.py (predict, P10/P90, SHAP)
```

* `etl/` – cleans the historical runs (Kaggle stop-level delay history) into one `runs` table
* `model/eta.py` – features, similar past runs, prediction, confidence range, reasons
* `model/train.py` – trains and picks the best model on a time split; `model/backtest.py` – accuracy, recovery and range calibration on unseen runs
* `live/feed.py` – live positions every 5 minutes, backfill, adopting new trains, nightly relearn; `live/weather.py` – weather
* `api.py` – JSON API for apps, station displays and control rooms; `app.py` + `templates/` – the website

## Run it

```
pip install -r requirements.txt
python etl/clean.py                              # needs the Kaggle CSVs in data/raw/
python live/feed.py backfill 2025-03-01 2026-10-31
python live/weather.py
python model/train.py && python model/backtest.py
python app.py                                    # http://127.0.0.1:5000
python live/feed.py live                         # in a second terminal
```

The run database and trained model are built locally and not stored in the repository (they are over 100 MB).

## Data sources

Historical runs: Kaggle "Indian Railways: stop-level delay history" (GPL-3.0). Live positions: whereismytrain.in
public cache and RailRadar. Train and station names: datameet/railways. Track geometry: India railway network open
data. Weather: Open-Meteo. None of these are official Indian Railways data; a deployment would take the same inputs
from NTES, RTIS and COA through the same API.

## History

The first prototype (FastAPI: network map, station weather, schedules) is earlier in this repository's history. Track
Pulse replaces it with a Flask app built around the ETA model; its network map carries over the first prototype's
track data.
