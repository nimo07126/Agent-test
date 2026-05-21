from __future__ import annotations

import sqlite3
from collections import Counter

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
    queued_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'queued'"
    ).fetchone()["c"]
    running_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'running'"
    ).fetchone()["c"]
    token_cost = conn.execute("SELECT COALESCE(SUM(token_cost), 0) AS c FROM runs").fetchone()[
        "c"
    ]

    events = conn.execute("SELECT tool_name FROM run_events WHERE tool_name IS NOT NULL").fetchall()
    tool_counts = Counter(row["tool_name"] for row in events)

    recent_failures = conn.execute(
        """
        SELECT r.id, r.task_id, r.error, r.finished_at, t.title
        FROM runs r
        JOIN tasks t ON r.task_id = t.id
        WHERE r.status = 'failed'
        ORDER BY r.finished_at DESC
        LIMIT 5
        """
    ).fetchall()

    avg_duration_ms = None
    duration_rows = conn.execute(
        """
        SELECT started_at, finished_at FROM runs
        WHERE started_at IS NOT NULL AND finished_at IS NOT NULL
        """
    ).fetchall()
    if duration_rows:
        total_ms = 0
        count = 0
        for row in duration_rows:
            try:
                from datetime import datetime, timezone
                start = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00"))
                total_ms += (end - start).total_seconds() * 1000
                count += 1
            except (ValueError, TypeError):
                continue
        if count > 0:
            avg_duration_ms = total_ms / count

    tool_costs = {}
    for tool_name, call_count in tool_counts.items():
        cost_per_call = 10
        tool_costs[tool_name] = call_count * cost_per_call

    return {
        "task_count": task_count,
        "run_count": run_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "failure_rate": failed_count / run_count if run_count else 0,
        "token_cost": token_cost,
        "tool_call_counts": dict(tool_counts),
        "tool_costs": tool_costs,
        "recent_failures": [
            {
                "run_id": row["id"],
                "task_id": row["task_id"],
                "title": row["title"],
                "error": row["error"],
                "finished_at": row["finished_at"],
            }
            for row in recent_failures
        ],
        "avg_duration_ms": avg_duration_ms,
        "queue_health": {
            "queued": queued_count,
            "running": running_count,
            "completed": completed_count,
            "failed": failed_count,
        },
        "generated_at": database.now_iso(),
    }