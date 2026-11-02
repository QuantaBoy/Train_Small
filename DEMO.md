# Track Pulse: 3-minute demo

Before the judges arrive: laptop on, `python app.py` (or `python -m flask --app app run --host 0.0.0.0`) and
`python live/feed.py live` running for at least 10 minutes. Open the site on the laptop and, if a phone is used, on the
laptop's hotspot address.

## 1. The problem, on the home page (30 s)

* Read the headline: **"Late now doesn't mean late there."**
* Point at the ticket stub: Kaveri Express leaves Katpadi 30 min late; carrying the delay forward says 30 min late at
  Bengaluru, Track Pulse says about 10. The timetable has spare time there and late trains use it.
* Scroll to **Running now**: real trains, live. Each card shows where the train is on its route, how late it is, and
  how late it will be further ahead (winning back or losing time).

## 2. One train, end to end (60 s)

Open a disrupted train from Running now (or search one, e.g. 12616).

* **Summary (right)**: expected time at the last station, how late, and "wins back / loses X on the way".
* **Journey (left)**: every station ahead. Point at one station's bar:
  "**Expected** 12:39 is our best estimate; 8 in 10 trains arrive between **Earliest** 12:28 and **Latest** 12:57.
  That window comes from how far off we really were on 90 days of runs the model never saw."
* Green / red track between stations: where it wins back or loses time. "Others" line: RailRadar's forecast and the
  delay carried forward, for comparison.
* Small grey text: the **reasons** (SHAP), e.g. "last 14 days on this stretch +10 min".
* **Where it wins back time**: recoveries spotted on unseen runs (e.g. 90% for Paschim Express), and the stretches with
  real vs predicted minutes. Carrying the delay forward spots 0%.
* **Trains on the line ahead / trains this delay can hold up**: one train's delay reaching others.

## 3. Operations (45 s)

* **Control**: counts by status, possibly held trains, platform clashes, late trains with others following. This is
  the OCC view from our PPT.
* **Stations**: open a station board (e.g. New Delhi), the screen a platform display would show.
* **Map**: every line in India, single vs double track, with the live trains on it.

## 4. Proof (30 s)

* Home, **Checked on runs it never saw**: 19.1 min average error vs 33.8 carrying the delay forward vs 60.9 timetable;
  3.3 min for the next 30 minutes.
* Spotting time recovery, train by train; live head-to-head with RailRadar once trains have arrived.
* **How it works**: data, models tried, why the blend wins, P10/P50/P90, limits, API.

## If asked

* *Where does live data come from?* Public whereismytrain cache and RailRadar, merged per station; in service the
  same API takes NTES / RTIS / COA.
* *Any train?* Search any of ~4,700 trains or a special's number; first open fetches its last 90 days.
* *Does it keep learning?* Every live ETA is scored on arrival; the model retrains every night at 03:00 and is only
  replaced by a more accurate one.
* *Not built yet:* graph neural network for delay spread (we use congestion inputs and route overlap instead), and
  the production stack (Kafka, TimescaleDB, Redis, MLflow) from the PPT.
