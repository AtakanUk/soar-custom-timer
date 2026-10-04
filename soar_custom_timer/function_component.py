from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from resilient_circuits import AppFunctionComponent, FunctionResult, app_function
from resilient_lib import IntegrationError

from .notifications import send_alert_email

LOG = logging.getLogger(__name__)
PACKAGE_NAME = "soar_custom_timer"
FUNCTION_NAME = "custom_timer"
DEFAULT_MAX_WAIT = 3_024_000
DEFAULT_DB_PATH = Path("data") / "custom_timer.db"


def _ensure_db_schema(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS timers (
                id TEXT PRIMARY KEY,
                incident_id TEXT,
                seconds INTEGER NOT NULL,
                due_epoch INTEGER NOT NULL,
                status TEXT NOT NULL,
                payload TEXT,
                created_epoch INTEGER NOT NULL,
                processed_epoch INTEGER,
                error TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_timers_due_status ON timers(status, due_epoch)"
        )


class FunctionComponent(AppFunctionComponent):
    """IBM SOAR app function that schedules a non-blocking timer."""

    def __init__(self, opts: dict[str, Any]):
        super().__init__(opts, PACKAGE_NAME)
        try:
            self.max_wait = int(
                self.options.get(
                    "max_wait_seconds",
                    os.getenv("TIMER_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT),
                )
            )
        except (TypeError, ValueError):
            self.max_wait = DEFAULT_MAX_WAIT

        configured_path = self.options.get("db_path") or os.getenv("TIMER_DB_PATH")
        self.db_path = Path(configured_path) if configured_path else DEFAULT_DB_PATH

        try:
            _ensure_db_schema(self.db_path)
        except Exception as exc:  # pragma: no cover - defensive startup path
            details = traceback.format_exc()
            LOG.error("Failed to prepare the timer database: %s", exc)
            send_alert_email(
                subject="SOAR custom timer database error",
                body=(
                    "The timer database could not be prepared.\n\n"
                    f"Path: {self.db_path}\n"
                    f"Error: {exc}\n\n{details}"
                ),
            )
            raise

    def _insert_timer(
        self, incident_id: str, seconds: int, payload: dict[str, Any]
    ) -> tuple[str, int]:
        now_epoch = int(time.time())
        due_epoch = now_epoch + seconds
        timer_id = str(uuid.uuid4())

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO timers (
                    id, incident_id, seconds, due_epoch, status, payload, created_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timer_id,
                    incident_id,
                    seconds,
                    due_epoch,
                    "pending",
                    json.dumps(payload),
                    now_epoch,
                ),
            )

        return timer_id, due_epoch

    @app_function(FUNCTION_NAME)
    def _app_function(self, fn_inputs: Any):
        yield self.status_message(f"Starting App Function: '{FUNCTION_NAME}'")

        raw_timer = getattr(fn_inputs, "timer_count", None)
        if raw_timer is None:
            raise IntegrationError("The required input 'timer_count' is missing.")

        try:
            seconds = int(str(raw_timer).strip())
        except ValueError as exc:
            raise IntegrationError("The 'timer_count' input must be numeric.") from exc

        if seconds < 0:
            raise IntegrationError("Negative wait values are not supported.")
        if seconds > self.max_wait:
            raise IntegrationError(
                f"The requested wait value exceeds the configured maximum of {self.max_wait} seconds."
            )

        incident_id = str(getattr(fn_inputs, "incident_id", "") or "").strip()
        if not incident_id:
            raise IntegrationError("The required input 'incident_id' is missing.")

        try:
            timer_id, due_epoch = self._insert_timer(
                incident_id=incident_id,
                seconds=seconds,
                payload={
                    "source_function": FUNCTION_NAME,
                    "requested_seconds": seconds,
                },
            )
        except Exception as exc:
            details = traceback.format_exc()
            LOG.error("Failed to schedule timer for incident %s: %s", incident_id, exc)
            send_alert_email(
                subject="SOAR custom timer scheduling error",
                body=(
                    f"A timer could not be stored for incident {incident_id}.\n\n"
                    f"Error: {exc}\n\n{details}"
                ),
            )
            raise IntegrationError(f"Timer scheduling failed: {exc}") from exc

        results = {
            "timer_id": timer_id,
            "incident_id": incident_id,
            "requested_seconds": seconds,
            "due_epoch": due_epoch,
            "status": "scheduled",
        }
        LOG.info("Timer scheduled for incident %s with id %s", incident_id, timer_id)
        yield self.status_message(f"Finished running App Function: '{FUNCTION_NAME}'")
        yield FunctionResult(results)
