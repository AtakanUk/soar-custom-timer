"""Background worker that fires the timers scheduled by the ``custom_timer`` function.

Due timers are claimed with an atomic ``pending -> processing`` update (safe with
several workers), and the incident field is set to the timer ID so that a
playbook "wait for condition" step can continue.
"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import sys
import time
import traceback
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import resilient

from .notifications import send_alert_email

DEFAULT_DB_PATH = Path("data") / "custom_timer.db"
DEFAULT_LOG_FILE = Path("logs") / "timer_worker.log"
LOG = logging.getLogger(__name__)
SHUTDOWN = False


def _get_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


DB_PATH = Path(os.getenv("TIMER_DB_PATH", str(DEFAULT_DB_PATH)))
POLL_SECONDS = _get_int("TIMER_POLL_SECONDS", 15)
BATCH_SIZE = _get_int("TIMER_BATCH_SIZE", 100)
LOG_FILE = Path(os.getenv("TIMER_LOG_FILE", str(DEFAULT_LOG_FILE)))
TIMER_FIELD = os.getenv("TIMER_FIELD", "custom_timer_done")

SOAR_HOST = os.getenv("SOAR_HOST", "")
SOAR_PORT = _get_int("SOAR_PORT", 443)
SOAR_ORG = os.getenv("SOAR_ORG", "")
SOAR_API_KEY_ID = os.getenv("SOAR_API_KEY_ID", "")
SOAR_API_KEY_SECRET = os.getenv("SOAR_API_KEY_SECRET", "")
SOAR_VERIFY_TLS = _get_bool("SOAR_VERIFY_TLS", True)


def _handle_signal(signum, _frame) -> None:
    global SHUTDOWN
    LOG.info("Received signal %s, shutting down.", signum)
    SHUTDOWN = True


def _setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [timer_worker] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    file_handler = TimedRotatingFileHandler(
        LOG_FILE, when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
    connection.commit()


def _soar_client():
    if not all([SOAR_HOST, SOAR_ORG, SOAR_API_KEY_ID, SOAR_API_KEY_SECRET]):
        raise RuntimeError("IBM SOAR connection settings are incomplete.")
    options = {
        "host": SOAR_HOST,
        "port": SOAR_PORT,
        "org": SOAR_ORG,
        "api_key_id": SOAR_API_KEY_ID,
        "api_key_secret": SOAR_API_KEY_SECRET,
        "cafile": False if not SOAR_VERIFY_TLS else None,
    }
    return resilient.get_client(options)


def mark_incident_timer_done(incident_id: str, timer_id: str, client=None) -> None:
    client = client or _soar_client()
    # get_put re-reads and retries when a playbook changed the incident in between (HTTP 409).
    client.get_put(
        f"/incidents/{incident_id}",
        lambda incident: incident.setdefault("properties", {}).update({TIMER_FIELD: timer_id}),
    )
    LOG.info(
        "Updated incident %s field %s with timer id %s",
        incident_id,
        TIMER_FIELD,
        timer_id,
    )


def fetch_due_timers(connection: sqlite3.Connection, now_epoch: int) -> list[sqlite3.Row]:
    cursor = connection.execute(
        """
        SELECT id, incident_id, seconds, due_epoch, payload
        FROM timers
        WHERE status = 'pending' AND due_epoch <= ?
        ORDER BY due_epoch ASC
        LIMIT ?
        """,
        (now_epoch, BATCH_SIZE),
    )
    return cursor.fetchall()


def try_lock_timer(connection: sqlite3.Connection, timer_id: str) -> bool:
    cursor = connection.execute(
        "UPDATE timers SET status = 'processing' WHERE id = ? AND status = 'pending'",
        (timer_id,),
    )
    connection.commit()
    return cursor.rowcount == 1


def mark_done(connection: sqlite3.Connection, timer_id: str, processed_epoch: int) -> None:
    connection.execute(
        "UPDATE timers SET status = 'done', processed_epoch = ?, error = NULL WHERE id = ?",
        (processed_epoch, timer_id),
    )
    connection.commit()


def mark_failed(
    connection: sqlite3.Connection, timer_id: str, processed_epoch: int, error_text: str
) -> None:
    connection.execute(
        "UPDATE timers SET status = 'failed', processed_epoch = ?, error = ? WHERE id = ?",
        (processed_epoch, error_text[:2000], timer_id),
    )
    connection.commit()


def recover_stuck_timers(connection: sqlite3.Connection) -> int:
    """Timers left in 'processing' by a crashed worker are put back in the queue."""
    cursor = connection.execute("UPDATE timers SET status = 'pending' WHERE status = 'processing'")
    connection.commit()
    if cursor.rowcount:
        LOG.warning("Re-queued %d timer(s) left in 'processing' state", cursor.rowcount)
    return cursor.rowcount


def process_timer(timer_row: dict[str, str], client=None) -> None:
    incident_id = timer_row.get("incident_id", "")
    timer_id = timer_row["id"]
    if not incident_id:
        LOG.warning("Skipping timer %s because no incident id is present.", timer_id)
        return
    mark_incident_timer_done(incident_id, timer_id, client)


def run_once(connection: sqlite3.Connection, now_epoch: int, client=None) -> tuple[int, int]:
    """Fire all due timers once; returns (done, failed)."""
    done = failed = 0
    for row in fetch_due_timers(connection, now_epoch):
        if SHUTDOWN:
            break
        timer_id = row["id"]
        if not try_lock_timer(connection, timer_id):
            continue
        try:
            process_timer(dict(row), client)
            mark_done(connection, timer_id, int(time.time()))
            done += 1
        except Exception as exc:
            details = traceback.format_exc()
            LOG.error("Timer %s for incident %s failed: %s", timer_id, row["incident_id"], exc)
            mark_failed(connection, timer_id, int(time.time()), str(exc))
            failed += 1
            send_alert_email(
                subject=f"SOAR custom timer failure - incident {row['incident_id']}",
                body=(
                    "Timer processing failed.\n\n"
                    f"Timer id: {timer_id}\n"
                    f"Incident id: {row['incident_id']}\n"
                    f"Requested seconds: {row['seconds']}\n"
                    f"Due epoch: {row['due_epoch']}\n"
                    f"Error: {exc}\n\n{details}"
                ),
            )
    return done, failed


def loop() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_schema(connection)
        recover_stuck_timers(connection)
        LOG.info(
            "Timer worker started. db_path=%s poll=%ss batch=%s field=%s",
            DB_PATH,
            POLL_SECONDS,
            BATCH_SIZE,
            TIMER_FIELD,
        )

        while not SHUTDOWN:
            run_once(connection, int(time.time()))
            for _ in range(POLL_SECONDS):
                if SHUTDOWN:
                    break
                time.sleep(1)
    LOG.info("Timer worker stopped.")


def main() -> None:
    _setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    loop()


if __name__ == "__main__":
    main()
