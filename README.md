# Track Pulse

**SIH 2026 · Problem SIH26028 · Dynamic forecast of expected time of arrival (ETA) for coaching trains**
**Theme: Smart Automation · Team Tech Sprints (team ID 141310)**

Track Pulse gives every station ahead of a running train an arrival time with a confidence range, learnt from how
trains really run. Its core idea: **late now doesn't mean late there.** The timetable hides spare time on some
stretches, and late trains use it to win time back; on other stretches they lose more. Track Pulse learns where,
for every train, and tells passengers, station displays and control offices through one API.

> Running-status displays today show *timetable + current delay*. Track Pulse shows *where the delay will be won
> back or grow*, how sure it is, and why.

## Contents

* [What it does](#what-it-does)
* [Results](#results)
* [The website](#the-website)
* [How it works](#how-it-works)
* [The model](#the-model)
* [API](#api)
* [Run it](#run-it)
* [Project layout](#project-layout)
* [Data sources](#data-sources)
* [Limits and what is next](#limits-and-what-is-next)
* [History](#history)

## What it does

| Problem statement asks for | Track Pulse | Where |
|---|---|---|
| Real-time ETA at every station | ETA for every station ahead of each live train, updated every 5 minutes | Train page, API |
| Historical delay patterns, average sectional running times | Learnt per stretch from up to 5 years of runs; timetable vs usual vs slow-day running times | Train page, `/sections` |
| Weather, congestion, preceding-train delays | Open-Meteo rain and fog-proxy temperature; reports of other trains on the next 200 km | Model inputs |
| Confidence | **P10 / P50 / P90**: earliest likely, expected, latest likely; 8 in 10 arrivals fall inside | Train page, station board, API |
| Delay recovery | Where each train wins back time, and how well we predict it on unseen runs | Train page, home |
| Cascading delays | Trains ahead that can hold this one up; trains behind this one's delay can hold up | Train page, control |
| Operating regimes | Normal, delayed, disrupted | Home, control |
| Reasons behind ETA changes | SHAP values: the two inputs that moved each prediction most, in minutes | Train page, API |
| Unscheduled halts | "Possibly held" when even a slow run would have reached the next station | Home, train page, control |
| Platform conflicts | Two trains due at one platform within 5 minutes | Station board, control, API |
| New, special, diverted trains | Any train number; history fetched on first open; learnt ETAs from the first run | Search |
| Scale to thousands of trains | ~4,700 trains searchable; any opened train joins the live feed | Search, feed |
| ML that keeps refining | Every live ETA scored on arrival; nightly retrain; a new model only replaces a better one | Home, How it works |
| APIs for apps, station displays, control rooms | JSON API for each | [API](#api) |

## Results

**On runs the model never saw** (3,832 runs, 90 days from 4 Aug 2026):

| | Average error | Within 5 min |
|---|---|---|
| **Track Pulse** | **19.1 min** | 34% |
| Delay carried forward (running-status displays) | 33.8 min | |
| Printed timetable | 60.9 min | |
| Track Pulse, arrivals in the next 30 minutes | **3.3 min** | **86%** |

**Spotting recovery:** of 24,001 times a train won back 5 or more minutes between two stations, Track Pulse predicted
**81%**. Carrying the delay forward predicts none. Examples: Paschim Express 12925 wins back 47 min on average between
Kurukshetra and Ambala Cantt (we predict 49); Netaji Express 12312 wins back 60 min between Mirzapur and Pt. Deen
Dayal Upadhyaya Jn (we predict 61).

**Live, against other estimates** (first day, same 415 arrivals): Track Pulse **9.7 min** average error, RailRadar's
own forecast 10.5 min, delay carried forward 13.8 min. Across all 923 live arrivals checked that day: Track Pulse
11.4 min, delay carried forward 16.7 min.

Long-distance trains are the hardest: Howrah Mail 12809 loses about 4 hours between Jalgaon and Kharagpur on most runs,
and the model learns that (a train 5 h 38 min late at Bhusaval is predicted about 10 hours late at Kharagpur, as on
past runs).

## The website

| Page | What it shows |
|---|---|
| **Home** | Search any train; running trains as cards with a mini track showing how far along each one is; accuracy and recovery, train by train; live head-to-head |
| **Train** | Left: the journey as a track, each station with Expected, Earliest and Latest, green and red stretches where it wins back or loses time, reasons, and other estimates. Right: arrival summary, what-if form (any station, any delay), trains in front and coming the other way, trains this delay can hold up. Below: where this train wins back time, running time between stations |
| **Control** | Status counts; possibly held trains, platform clashes, late trains with others following; every running train, worst first. Refreshes every minute |
| **Stations** | A platform display board for any station: every train on its way, platform, expected time and range |
| **Map** | Every operational line in India, single and double track, with live trains coloured by status |
| **How it works** | Data, inputs, models tried, results, testing, P10/P50/P90, recovery, cascades, halts, limits, API |

Light theme, works on phones; delays read as hours and minutes ("+5 h 38 min") with the day or date for arrivals past
midnight. A 3-minute walkthrough for judges is in [DEMO.md](DEMO.md).

## How it works

```
 whereismytrain cache ─┐
                       ├─► live/feed.py (every 5 min) ─► POST /api/trains/<no>/position ─► positions, ETAs logged
 RailRadar ────────────┘        │  merge per station          │
                                │  (fresher wins)             ▼
                                ▼                       app.py + api.py (Flask)
                         runs table (SQLite) ◄──────────  model/eta.py: predict, P10/P90, SHAP, recovery
                                │
          03:00 nightly:  live/weather.py → model/train.py → model/backtest.py
                          (weather)          (retrain, champion/challenger)  (accuracy, recovery, P10/P90 bands)
```

1. **History.** `etl/clean.py` turns stop-level run history into one `runs` table: one row per train, run and
   station, with scheduled and actual times. Delay is computed from timestamps, so early trains show as negative.
2. **Live.** `live/feed.py` asks both sources about every followed train, keeps the fresher report per station, saves
   every observed stop (so the model keeps learning), posts the position, logs RailRadar's forecast and expected
   platforms, and writes a heartbeat. If the heartbeat stops for 15 minutes the site says live updates are paused and
   shows last-known positions.
3. **Predict.** For the train's position and delay, `model/eta.py` builds inputs for every station ahead and asks the
   model in use for the delay there. P10 and P90 come from the model's measured errors; reasons from SHAP.
4. **Check and relearn.** Every ETA from a live position is logged and scored when the train arrives. Every night the
   models are retrained and the test re-run; a new model replaces the old one only if it is at least as accurate.

## The model

**Inputs (21):** delay now, the train and section, distance and scheduled time to go, time of day, day and season,
similar past runs (the 50 runs that were about as late here, and how their delay changed), the last 14 days on this
stretch, congestion (other trains' reports on the next 200 km in the last 2 hours: how many, how late, whether losing
time), and weather where the train is heading (rain, coldest temperature as a fog proxy).

**Models tried:** ridge regression, random forest, extra trees, histogram gradient boosting, LightGBM, XGBoost, and
blends of the best. **In use:** a blend of LightGBM, HistGradientBoosting and XGBoost, chosen on a validation window
and reported once on a later test window. The split is always by date, never random.

**P10 / P50 / P90:** P50 is the prediction. P10 and P90 are the 10th and 90th percentile of the model's real errors
on unseen runs, per train and per horizon (next hour, 1 to 3 h, 3 to 8 h, 8 to 24 h, more than a day), so 8 in 10
arrivals land inside.

**Reasons:** LightGBM's exact SHAP values, grouped into plain reasons ("last 14 days on this stretch +10 min").

**Cold start:** a train with fewer than 10 runs uses its latest timetable, and the model answers from how all trains
behave on comparable stretches until its own history builds up.

## API

All JSON, times ISO 8601 in IST.

| Request | For | Returns |
|---|---|---|
| `GET /api/trains` | Apps | Followed trains and their stations |
| `GET /api/trains/16021/eta?at=KPD&delay=30` | Apps, controllers | What-if ETAs for every station ahead |
| `GET /api/trains/16021/live` | Apps | ETAs from the train's last reported position |
| `POST /api/trains/16021/position` | Live feed | Record a position: `{"station", "delay", "date", "reported_at"}` |
| `GET /api/trains/16021/sections` | Planners | Running time between stations: timetable, usual, slow days |
| `GET /api/stations/NDLS/arrivals` | Station displays | Trains on their way, with platform, P10/P50/P90 |
| `GET /api/control` | Control office | Every running train: position, delay, delay ahead, trend, status, possible halt |
| `GET /api/platforms/clashes` | Control office | Two trains due at one platform within 5 minutes |
| `GET /api/accuracy`, `/api/accuracy/live`, `/api/models` | Anyone | Test accuracy, recovery, live scores, model comparison |

Each station ahead carries `scheduled`, `eta`, `p10`, `p50`, `p90`, `delay_min`, `change_min` (negative = time won
back), `method`, `similar_runs` and `why`.

## Run it

```
pip install -r requirements.txt
python etl/clean.py                                   # Kaggle CSVs in data/raw/
python live/feed.py backfill 2025-03-01 2026-10-31    # history for the followed trains
python live/weather.py
python model/train.py && python model/backtest.py
python app.py                                         # http://127.0.0.1:5000
python live/feed.py live                              # second terminal: live positions + nightly relearn
python tests/check_api.py                             # API checks
```

To reach it from a phone, run `python -m flask --app app run --host 0.0.0.0` and open the laptop's address on port
5000 (a laptop hotspot works where campus Wi-Fi blocks devices from each other).

The run database (over 100 MB) and the trained model are built locally and not stored in the repository.

## Project layout

| Path | What |
|---|---|
| `app.py` | Website routes: home, train, control, stations, map, how it works |
| `api.py` | JSON API, live positions, control rows, platform clashes, station boards |
| `model/eta.py` | Inputs, similar runs, prediction, P10/P90 bands, reasons |
| `model/train.py` | Train all models, pick the best, champion / challenger |
| `model/backtest.py` | Accuracy, recovery spotting and P10/P90 calibration on unseen runs |
| `model/score.py` | Live scoring, head-to-head with RailRadar |
| `live/feed.py` | Live feed, backfill, adopting new trains, nightly relearn |
| `live/weather.py` | Weather history and forecast per 50 km square |
| `etl/` | Cleaning the historical runs |
| `templates/`, `static/` | Pages, styles, network map data |
| `data/*.json` | Train and station names, latest accuracy and model reports |
| `network_map/`, `cross checked/`, `Railway Network Official.pdf` | Reference data from the first prototype |

## Data sources

* Historical runs: Kaggle "Indian Railways: stop-level delay history" (GPL-3.0), plus runs collected from the live
  sources since March 2025.
* Live positions: whereismytrain.in public cache and RailRadar.
* Train and station names: [datameet/railways](https://github.com/datameet/railways).
* Track geometry: India railway network open data (carried over from the first prototype).
* Weather: [Open-Meteo](https://open-meteo.com/).

None of these are official Indian Railways data. In service, the same API would take its inputs from NTES, RTIS and
COA, with no change to the model.

## Limits and what is next

* Positions come from station reports, not GPS; with RTIS the same API would get a position every few seconds.
* Congestion and cascades only see the trains Track Pulse follows; with the whole network in the feed they see all
  traffic.
* A station can only be predicted once it has reported in past runs, so a few routes end a station or two early.
* Planned for deployment, not in the prototype: a graph neural network for delay propagation, and the production stack
  from our proposal (Kafka, TimescaleDB/PostGIS, Redis cache, MLflow model registry, zone-wise rollout).

## History

The first prototype (FastAPI: network map, station weather, schedules, September 2026) is earlier in this
repository's history. Track Pulse replaced it from 25 October 2026 with a Flask app built around the ETA model; its
network map reuses the first prototype's track data.
