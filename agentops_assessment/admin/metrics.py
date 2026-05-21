from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime

from agentops_assessment.backend import database


def build_dashboard(conn: sqlite3.Connection) -> dict:
    task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    run_count = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
    failed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'failed'"
    ).fetchone()["c"]
    completed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'completed'"
    ).fetchone()["c"]
    token_cost = conn.execute("SELECT COALESCE(SUM(token_cost), 0) AS c FROM runs").fetchone()[
        "c"
    ]
    events = conn.execute("SELECT tool_name FROM run_events WHERE tool_name IS NOT NULL").fetchall()
    tool_counts = Counter(row["tool_name"] for row in events)

    duration_rows = conn.execute(
        """
        SELECT started_at, finished_at
        FROM runs
        WHERE started_at IS NOT NULL AND finished_at IS NOT NULL
        """
    ).fetchall()
    durations: list[float] = []
    for row in duration_rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
            finished = datetime.fromisoformat(row["finished_at"])
        except ValueError:
            continue
        durations.append((finished - started).total_seconds() * 1000)
    recent_failure_rows = conn.execute(
        """
        SELECT id, task_id, error, finished_at
        FROM runs
        WHERE status = 'failed'
        ORDER BY finished_at DESC
        LIMIT 5
        """
    ).fetchall()
    queued_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status IN ('queued', 'running')"
    ).fetchone()["c"]
    permission_denials = conn.execute(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE decision = 'deny'"
    ).fetchone()["c"]
    return {
        "task_count": task_count,
        "run_count": run_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "failure_rate": failed_count / run_count if run_count else 0,
        "token_cost": token_cost,
        "tool_call_counts": dict(tool_counts),
        "avg_duration_ms": sum(durations) / len(durations) if durations else 0,
        "recent_failures": [
            {
                "run_id": row["id"],
                "task_id": row["task_id"],
                "error": row["error"],
                "finished_at": row["finished_at"],
            }
            for row in recent_failure_rows
        ],
        "queue_health": {
            "queued_or_running": queued_count,
            "status": "busy" if queued_count else "idle",
        },
        "permission_denials": permission_denials,
        "generated_at": database.now_iso(),
    }
