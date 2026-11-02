"""Levels 7 + 10 check: every API endpoint, happy path and bad input. Leaves positions and predictions as it found them.

    python tests/check_api.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from api import CORRIDORS, TRAINS, con, status  # noqa: E402
from eta import TRAIN_CODES  # noqa: E402
from app import app  # noqa: E402

c = app.test_client()
saved = con.execute("SELECT * FROM positions").fetchall()
last_pred = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM predictions").fetchone()[0]
con.execute("DELETE FROM positions")
try:
    assert len(c.get("/api/trains").json) == len(TRAINS)
    assert set(TRAINS) == set(TRAIN_CODES) == {n for nos in CORRIDORS.values() for n in nos}, "train lists out of sync"
    sec = c.get("/api/trains/16021/sections").json["sections"]
    assert sec and all(s["scheduled_min"] > 0 and s["typical_min"] > 0 for s in sec)

    r = c.get("/api/trains/16021/eta?at=KPD&delay=30&date=2026-10-25").json
    sbc = next(e for e in r["etas"] if e["station"] == "SBC")
    assert sbc["scheduled"].startswith("2026-10-26T03:"), sbc  # left 25th, reaches past midnight -> next day
    assert sbc["eta_range"][0] <= sbc["eta"] <= sbc["eta_range"][1]
    assert sbc["method"] != "model" or sbc["why"], sbc  # ML predictions carry their SHAP reasons

    for bad, code in [("/api/trains/99999/eta?at=KPD", 404), ("/api/trains/16021/eta", 400),
                      ("/api/trains/16021/eta?at=XYZ", 400), ("/api/trains/16021/eta?at=KPD&delay=abc", 400),
                      ("/api/trains/16021/eta?at=KPD&delay=5000", 400), ("/api/trains/16021/live", 404)]:
        assert c.get(bad).status_code == code, bad
    assert c.post("/api/trains/16021/position", data="x").status_code == 400

    assert c.post("/api/trains/16021/position", json={"station": "KPD", "delay": 30, "date": "2026-10-25"}).status_code == 201
    assert c.post("/api/trains/22625/position", json={"station": "AJJ", "delay": 10}).status_code == 201
    assert c.get("/api/trains/16021/live").json["at"] == "KPD"
    board = c.get("/api/stations/kpn/arrivals").json["arrivals"]
    assert {a["train"] for a in board} == {"16021", "22625"}
    ctl = c.get("/api/control").json["trains"]
    assert {t["train"] for t in ctl} == {"16021", "22625"} and not ctl[0]["stale"]
    assert next(t for t in ctl if t["train"] == "16021")["status"] in ("delayed", "disrupted")  # 30 min late now
    assert [status(5, 10), status(5, 20), status(70, 40), status(20, None)] == ["normal", "delayed", "disrupted", "delayed"]
    assert "all" in c.get("/api/accuracy").json
    logged = con.execute("SELECT COUNT(*) FROM predictions WHERE rowid > ?", (last_pred,)).fetchone()[0]
    assert logged > 0, "new positions must log their ETAs"
    c.post("/api/trains/22625/position", json={"station": "AJJ", "delay": 10})  # unchanged -> not logged again
    assert con.execute("SELECT COUNT(*) FROM predictions WHERE rowid > ?", (last_pred,)).fetchone()[0] == logged
    assert c.get("/api/accuracy/live").status_code == 200 and c.get("/api/accuracy/live?days=0").status_code == 400
    print("all API checks passed")
finally:
    con.execute("DELETE FROM positions")
    con.execute("DELETE FROM predictions WHERE rowid > ?", (last_pred,))
    con.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?)", saved)
    con.commit()
