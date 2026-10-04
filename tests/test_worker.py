import sqlite3
from unittest.mock import MagicMock

import pytest

from soar_custom_timer import function_component, worker


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    worker._ensure_schema(connection)
    yield connection
    connection.close()


def add_timer(connection, timer_id, incident_id, due, status="pending"):
    connection.execute(
        "INSERT INTO timers (id, incident_id, seconds, due_epoch, status, created_epoch) VALUES (?, ?, 60, ?, ?, 0)",
        (timer_id, incident_id, due, status),
    )
    connection.commit()


def status_of(connection, timer_id):
    return connection.execute("SELECT status FROM timers WHERE id = ?", (timer_id,)).fetchone()[0]


class FakeSoar:
    def __init__(self, failing=()):
        self.incidents = {}
        self.failing = set(failing)

    def get_put(self, uri, apply):
        incident_id = uri.rsplit("/", 1)[1]
        if incident_id in self.failing:
            raise RuntimeError("404 Unable to find Incident")
        incident = self.incidents.setdefault(incident_id, {"properties": {}})
        apply(incident)


def test_run_once_fires_due_timers_only(db, monkeypatch):
    monkeypatch.setattr(worker, "send_alert_email", MagicMock())
    add_timer(db, "t-due", "2101", due=100)
    add_timer(db, "t-later", "2102", due=999)
    soar = FakeSoar()

    assert worker.run_once(db, now_epoch=200, client=soar) == (1, 0)
    assert soar.incidents["2101"]["properties"][worker.TIMER_FIELD] == "t-due"
    assert (status_of(db, "t-due"), status_of(db, "t-later")) == ("done", "pending")


def test_failure_marks_timer_and_alerts(db, monkeypatch):
    alert = MagicMock()
    monkeypatch.setattr(worker, "send_alert_email", alert)
    add_timer(db, "t-bad", "404", due=100)

    assert worker.run_once(db, now_epoch=200, client=FakeSoar(failing={"404"})) == (0, 1)
    row = db.execute("SELECT status, error FROM timers WHERE id = 't-bad'").fetchone()
    assert row["status"] == "failed" and "Unable to find" in row["error"]
    assert "incident 404" in alert.call_args.kwargs["subject"]


def test_locking_and_recovery(db):
    add_timer(db, "t1", "1", due=0)
    assert worker.try_lock_timer(db, "t1") is True
    assert worker.try_lock_timer(db, "t1") is False  # a second worker cannot take it
    assert worker.recover_stuck_timers(db) == 1
    assert status_of(db, "t1") == "pending"


def test_function_component_stores_timer(tmp_path):
    database = tmp_path / "timers.db"
    function_component._ensure_db_schema(database)
    component = object.__new__(
        function_component.FunctionComponent
    )  # skip the SOAR connection setup
    component.db_path = database
    timer_id, due = component._insert_timer("2101", 300, {"requested_seconds": 300})

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT incident_id, seconds, due_epoch, status FROM timers WHERE id = ?", (timer_id,)
        ).fetchone()
    assert row == ("2101", 300, due, "pending")
