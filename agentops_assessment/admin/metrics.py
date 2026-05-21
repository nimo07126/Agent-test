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
    durations = []
    for row in conn.execute(
        "SELECT started_at, finished_at FROM runs WHERE started_at IS NOT NULL AND finished_at IS NOT NULL"
    ).fetchall():
        try:
            started = datetime.fromisoformat(row["started_at"])
            finished = datetime.fromisoformat(row["finished_at"])
            durations.append(max(0.0, (finished - started).total_seconds()))
        except ValueError:
            continue
    recent_failures = [
        {
            "run_id": row["id"],
            "task_id": row["task_id"],
            "error": row["error"],
            "finished_at": row["finished_at"],
        }
        for row in conn.execute(
            """
            SELECT id, task_id, error, finished_at
            FROM runs
            WHERE status = 'failed'
            ORDER BY COALESCE(finished_at, created_at) DESC
            LIMIT 5
            """
        ).fetchall()
    ]
    queue_counts = {
        row["status"]: row["c"]
        for row in conn.execute("SELECT status, COUNT(*) AS c FROM runs GROUP BY status").fetchall()
    }
    permission_denials = conn.execute(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE decision = 'deny'"
    ).fetchone()["c"]

    average_run_seconds = sum(durations) / len(durations) if durations else 0
    return {
        "task_count": task_count,
        "run_count": run_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "failure_rate": failed_count / run_count if run_count else 0,
        "token_cost": token_cost,
        "tool_call_counts": dict(tool_counts),
        "avg_duration_seconds": average_run_seconds,
        "average_run_seconds": average_run_seconds,
        "recent_failures": recent_failures,
        "queue_health": {
            "queued": queue_counts.get("queued", 0),
            "running": queue_counts.get("running", 0),
            "completed": queue_counts.get("completed", 0),
            "failed": queue_counts.get("failed", 0),
        },
        "permission_denials": permission_denials,
        "generated_at": database.now_iso(),
    }
