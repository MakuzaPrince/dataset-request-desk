import io
from datetime import datetime

from sqlalchemy import func, select

from app.models import Episode, ImportRun, Quality
from app.services.importer import import_episodes
from tests.conftest import SEED_CSV

HEADER = "episode_id,robot_id,task_name,recorded_at,duration_seconds,operator_name,quality\n"


def run(db, text: str) -> dict:
    return import_episodes(db, io.StringIO(text), filename="t.csv")


def run_seed(db) -> dict:
    with SEED_CSV.open(encoding="utf-8-sig", newline="") as fh:
        return import_episodes(db, fh, filename=SEED_CSV.name)


def count(db) -> int:
    return db.scalar(select(func.count()).select_from(Episode))


def get(db, episode_id) -> Episode:
    return db.scalars(select(Episode).where(Episode.episode_id == episode_id)).one()


def test_seed_file_import_report(db):
    report = run_seed(db)
    assert report["rows_read"] == 191
    assert report["imported"] == 173 == count(db)
    assert report["skipped"] == 18
    assert report["imported"] + report["unchanged"] + report["skipped"] == report["rows_read"]
    assert report["skipped_by_reason"] == {
        "blank_row": 2,
        "conflicting_duplicate_in_file": 2,
        "duplicate_in_file": 2,
        "duration_out_of_range": 1,
        "invalid_duration": 1,
        "invalid_quality": 1,
        "invalid_recorded_at": 1,
        "malformed_row": 1,
        "missing_duration": 2,
        "missing_episode_id": 1,
        "missing_quality": 1,
        "missing_robot_id": 1,
        "recorded_at_in_future": 1,
        "unknown_robot": 1,
    }
    unknown = next(s for s in report["skipped_rows"] if s["reason"] == "unknown_robot")
    assert unknown == {"line": 162, "episode_id": "EP-00024", "reason": "unknown_robot",
                       "detail": "robot 'arm-99' is not a known robot"}


def test_reimporting_the_same_file_creates_nothing(db):
    first = run_seed(db)
    second = run_seed(db)
    assert second["imported"] == 0
    assert second["unchanged"] == first["imported"]
    assert second["skipped_by_reason"] == first["skipped_by_reason"]
    assert count(db) == first["imported"]
    assert db.scalar(select(func.count()).select_from(ImportRun)) == 2


def test_messy_values_are_normalised(db):
    run_seed(db)
    assert get(db, "EP-00006").task_name == "pick cup"          # "  Pick Cup "
    assert get(db, "EP-00007").task_name == "pick cup"          # "PICK CUP"
    assert get(db, "EP-00009").quality == Quality.good          # "Good"
    assert get(db, "EP-00010").quality == Quality.usable        # "USABLE"
    assert get(db, "EP-00008").robot_id == "arm-01"             # " arm-01"
    assert get(db, "EP-00014").recorded_at == datetime(2026, 8, 14, 9, 15)   # 14/08/2026 09:15 (day-first)
    assert get(db, "EP-00013").recorded_at == datetime(2026, 8, 14, 9, 12)   # space-separated ISO
    assert get(db, "EP-00018").duration_seconds == 45.5
    assert get(db, "EP-90002").task_name == "pick cup, then place"          # quoted comma
    assert get(db, "EP-90005").operator_name is None                        # imported with a warning


def test_conflicting_duplicates_keep_the_first_row(db):
    report = run_seed(db)
    assert get(db, "EP-00011").quality == Quality.bad            # line 3 wins over line 168
    assert get(db, "EP-00003").robot_id == "humanoid-01"        # "ep-00003" on line 189 is the same id
    conflicts = {s["episode_id"]: s["line"] for s in report["skipped_rows"] if s["reason"] == "conflicting_duplicate_in_file"}
    assert conflicts == {"EP-00011": 168, "EP-00003": 189}


def test_existing_episode_is_never_overwritten(db):
    run(db, HEADER + "E-1,arm-01,pick cup,2026-08-01T10:00:00,30,Ann,good\n")
    report = run(db, HEADER + "E-1,arm-01,pick cup,2026-08-01T10:00:00,30,Ann,bad\n"
                              "E-2,arm-02,pick cup,2026-08-01T10:00:00,30,Ann,good\n")
    assert report["imported"] == 1
    assert report["skipped_by_reason"] == {"conflicts_with_existing": 1}
    assert get(db, "E-1").quality == Quality.good


def test_row_with_several_problems_reports_all_of_them(db):
    report = run(db, HEADER + "E-9,arm-01,,not a date,-1,Ann,great\n")
    [row] = report["skipped_rows"]
    assert row["reason"] == "missing_task_name"
    assert "not a date" in row["detail"] and "-1" in row["detail"] and "great" in row["detail"]


def test_missing_required_columns_rejects_the_file(client, h):
    r = client.post(
        "/api/episodes/import",
        files={"file": ("x.csv", b"episode_id,robot_id\nE-1,arm-01\n", "text/csv")},
        headers=h["ops"],
    )
    assert r.status_code == 422
    assert "task_name" in r.json()["detail"]


def test_import_endpoint_is_idempotent(client, db, h):
    data = SEED_CSV.read_bytes()
    first = client.post("/api/episodes/import", files={"file": ("e.csv", data, "text/csv")}, headers=h["ops"])
    second = client.post("/api/episodes/import", files={"file": ("e.csv", data, "text/csv")}, headers=h["ops"])
    assert first.status_code == second.status_code == 200
    assert (first.json()["imported"], second.json()["imported"], second.json()["unchanged"]) == (173, 0, 173)
    assert count(db) == 173


def test_non_utf8_file_is_rejected(client, h):
    r = client.post("/api/episodes/import", files={"file": ("e.csv", HEADER.encode() + b"\xff\xfe\x00", "text/csv")},
                    headers=h["ops"])
    assert r.status_code == 422
