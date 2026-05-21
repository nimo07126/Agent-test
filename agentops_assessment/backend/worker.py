from __future__ import annotations

import os

from agentops_assessment.agent.executor import Executor
from agentops_assessment.agent.planner import Planner
from agentops_assessment.agent.tools import ToolRegistry
from agentops_assessment.backend.auth import get_user
from agentops_assessment.backend import database
from agentops_assessment.rag.security import redact_sensitive


def execute_run(run_id: str) -> None:
    """后台执行入口。"""
    with database.connect() as conn:
        database.init_db(conn)
        row = conn.execute(
            """
            SELECT runs.*, tasks.prompt, tasks.title, tasks.created_by
            FROM runs
            JOIN tasks ON tasks.id = runs.task_id
            WHERE runs.id = ?
            """,
            (run_id,),
        ).fetchone()
        if not row:
            return
        now = database.now_iso()
        conn.execute(
            "UPDATE runs SET status = ?, started_at = ? WHERE id = ?",
            ("running", now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            ("running", now, row["task_id"]),
        )
        conn.commit()
        database.insert_run_event(
            conn,
            run_id,
            "run.started",
            {"task_id": row["task_id"]},
        )

    planner = Planner()
    plan = planner.create_plan(row["prompt"], {"title": row["title"]})
    user = get_user(row["requested_by"])
    user_permissions = user["permissions"] if user else []
    sku = next(
        (
            step.input_template.get("sku")
            for step in plan
            if step.input_template.get("sku")
        ),
        None,
    )

    fixtures_dir = os.getenv("ASSESSMENT_FIXTURES_DIR", "fixtures")
    registry = ToolRegistry.with_default_clients(fixtures_dir=fixtures_dir, retry_attempts=2)

    def emit_event(event_type: str, payload: dict, tool_name: str | None = None) -> None:
        with database.connect() as event_conn:
            database.init_db(event_conn)
            database.insert_run_event(event_conn, run_id, event_type, payload, tool_name)

    def audit(action: str, resource: str, payload: dict, decision: str = "allow") -> None:
        with database.connect() as audit_conn:
            database.init_db(audit_conn)
            database.insert_audit_log(
                audit_conn,
                actor_id=row["requested_by"],
                action=action,
                resource=resource,
                payload=payload,
                decision=decision,
            )

    emit_event(
        "plan.created",
        {
            "steps": [
                {
                    "id": step.id,
                    "tool_name": step.tool_name,
                    "required_permissions": step.required_permissions,
                    "optional": step.optional,
                }
                for step in plan
            ]
        },
    )

    executor = Executor(registry)
    try:
        state = executor.execute(
            run_id,
            plan,
            {
                "task_id": row["task_id"],
                "prompt": row["prompt"],
                "actor_id": row["requested_by"],
                "user_permissions": user_permissions,
                "sku": sku,
                "emit_event": emit_event,
                "audit": audit,
            },
        )
        token_cost = planner.last_usage["prompt_tokens"] + planner.last_usage["completion_tokens"]
        with database.connect() as conn:
            database.init_db(conn)
            finished_at = database.now_iso()
            conn.execute(
                """
                UPDATE runs
                SET status = ?, result_json = ?, token_cost = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    "completed",
                    database.encode_json(redact_sensitive(state.result or {})),
                    token_cost,
                    finished_at,
                    run_id,
                ),
            )
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ("completed", finished_at, row["task_id"]),
            )
            conn.commit()
            database.insert_run_event(conn, run_id, "run.completed", {"token_cost": token_cost})
    except Exception as exc:
        with database.connect() as conn:
            database.init_db(conn)
            finished_at = database.now_iso()
            error = str(exc)
            conn.execute(
                """
                UPDATE runs
                SET status = ?, error = ?, finished_at = ?
                WHERE id = ?
                """,
                ("failed", error, finished_at, run_id),
            )
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ("failed", finished_at, row["task_id"]),
            )
            conn.commit()
            database.insert_run_event(conn, run_id, "run.failed", {"error": error})
            database.insert_audit_log(
                conn,
                actor_id=row["requested_by"],
                action="run.failed",
                resource=run_id,
                payload={"error": error, "task_id": row["task_id"]},
                decision="deny",
            )
