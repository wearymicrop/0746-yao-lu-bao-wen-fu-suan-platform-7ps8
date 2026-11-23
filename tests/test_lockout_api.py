import copy

from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.m7_archive.bundle import consistent


def _payload(dirty):
    calibrations = copy.deepcopy(dirty["calibrations"])
    calibrations[3]["offset_mc"] = 1100
    return {
        "source": "dirty",
        "operator": "复测员",
        "readings": [
            {
                "zone": "南墙",
                "time": "2026-11-20T09:34:00+08:00",
                "unit": "C",
                "value": "1148",
            }
        ],
        "calibrations": calibrations,
    }


def test_lockout_overview_lists_pending_record(client):
    overview = client.get("/api/lockouts/dirty").json()
    assert overview["boundary_count"] == 1
    assert overview["all_released"] is False
    record = overview["records"][0]
    assert record["status"] == "LOCKED" and record["seconds"] == 120
    assert overview["batches"] == ["YL-1120-07"]


def test_resample_release_flow_and_idempotent_retry(client, dirty):
    payload = _payload(dirty)
    first = client.post("/api/lockouts/dirty/resample", json=payload).json()
    assert first["passed"] is True
    record = first["records"][0]
    assert record["status"] == "RELEASED" and record["attempts"] == 1

    again = client.post("/api/lockouts/dirty/resample", json=payload).json()
    repeated = again["records"][0]
    assert repeated["id"] == record["id"]
    assert repeated["attempts"] == 1 and repeated["seconds"] == record["seconds"]

    overview = client.get("/api/lockouts/dirty").json()
    assert overview["all_released"] is True
    assert overview["release_view"] is not None


def test_failed_attempt_keeps_previous_conclusion(client, dirty):
    bad = {
        "source": "dirty",
        "operator": "复测员",
        "readings": [
            {
                "zone": "南墙",
                "time": "2026-11-20T09:30:00+08:00",
                "unit": "C",
                "value": "1148",
            }
        ],
    }
    response = client.post("/api/lockouts/dirty/resample", json=bad)
    assert response.status_code == 422
    record = client.get("/api/lockouts/dirty").json()["records"][0]
    assert record["status"] == "LOCKED" and record["attempts"] == 0

def test_failed_then_successful_release(client, dirty):
    bad = _payload(dirty)
    bad.pop("calibrations")
    verdict = client.post("/api/lockouts/dirty/resample", json=bad).json()
    assert verdict["passed"] is False
    record = verdict["records"][0]
    assert record["status"] == "LOCKED" and record["attempts"] == 1
    assert record["failure_reasons"]

    ok = client.post(
        "/api/lockouts/dirty/resample", json=_payload(dirty)
    ).json()
    assert ok["passed"] is True
    assert ok["records"][0]["attempts"] == 2
    assert ok["records"][0]["failure_reasons"] == []


def test_archive_through_released_lockout(client, dirty):
    client.post("/api/lockouts/dirty/resample", json=_payload(dirty))
    blocked = client.post(
        "/api/runs", json={"source": "dirty", "operator": "值班员"}
    )
    assert blocked.status_code == 409

    record = client.post(
        "/api/runs",
        json={"source": "dirty", "operator": "值班员", "use_lockout": True},
    ).json()
    assert consistent(record)
    assert record["manifest"]["lockout_records"]
    rerun = client.post(f"/api/runs/{record['id']}/rerun").json()
    assert rerun["reused"] is True


def test_lockout_persists_across_sessions(tmp_path, dirty):
    import pathlib

    db = tmp_path / "lock.sqlite3"
    with TestClient(create_app(db)) as client:
        client.post("/api/lockouts/dirty/resample", json=_payload(dirty))
    with TestClient(create_app(db)) as client:
        overview = client.get("/api/lockouts/dirty").json()
        assert overview["all_released"] is True
        assert overview["records"][0]["status"] == "RELEASED"


def test_resample_requires_operator(client, dirty):
    payload = _payload(dirty)
    payload["operator"] = ""
    response = client.post("/api/lockouts/dirty/resample", json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "OPERATOR_REQUIRED"


def test_concurrent_resample_keeps_single_record_and_timing(client, dirty):
    from concurrent.futures import ThreadPoolExecutor

    payload = _payload(dirty)
    with ThreadPoolExecutor(4) as pool:
        verdicts = list(
            pool.map(
                lambda _: client.post(
                    "/api/lockouts/dirty/resample", json=payload
                ).json(),
                range(4),
            )
        )
    assert all(item["passed"] for item in verdicts)
    records = {item["records"][0]["id"] for item in verdicts}
    assert len(records) == 1
    stored = client.get("/api/lockouts/dirty").json()["records"]
    assert len(stored) == 1 and stored[0]["seconds"] == 120
    assert stored[0]["status"] == "RELEASED"
