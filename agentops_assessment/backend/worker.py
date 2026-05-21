from __future__ import annotations

from agentops_assessment.agent.executor import Executor
from agentops_assessment.agent.planner import Planner, infer_intent
from agentops_assessment.agent.tools import ToolRegistry
from agentops_assessment.backend import database
from agentops_assessment.security import safe_error, sanitize


def execute_run(run_id: str) -> None:
    """后台执行入口。

    TODO(candidate/P0): 用完整的 Planner -> Executor 流程替换此占位实现。
    预期实现应更新 running/completed/failed 状态，持久化步骤事件，
    通过 ToolRegistry 调用工具，记录 token 成本，并保存最终业务结果。
    """
    with database.connect() as conn:
        database.init_db(conn)
        now = database.now_iso()
        row = conn.execute(
            """
            SELECT runs.id AS run_id, runs.task_id, runs.requested_by, tasks.title, tasks.prompt
            FROM runs
            JOIN tasks ON tasks.id = runs.task_id
            WHERE runs.id = ?
            """,
            (run_id,),
        ).fetchone()
        if not row:
            return
        conn.execute(
            "UPDATE runs SET status = ?, started_at = ? WHERE id = ?",
            ("running", now, run_id),
        )
        database.insert_run_event(
            conn,
            run_id,
            "run.started",
            {"task_id": row["task_id"]},
        )
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            ("running", now, row["task_id"]),
        )
        conn.commit()

    planner = Planner()
    plan = planner.create_plan(row["prompt"], {"title": row["title"]})
    with database.connect() as conn:
        database.init_db(conn)
        database.insert_run_event(
            conn,
            run_id,
            "plan.created",
            {
                "steps": [
                    {"id": step.id, "tool_name": step.tool_name, "description": step.description}
                    for step in plan
                ]
            },
        )

    user = _load_user(row["requested_by"])
    context = {
        "title": row["title"],
        "prompt": row["prompt"],
        "intent": infer_intent(f"{row['title']}\n{row['prompt']}"),
        "requested_by": row["requested_by"],
        "user_permissions": user.get("permissions", []),
    }
    registry = ToolRegistry.with_default_clients(retry_attempts=2)
    executor = Executor(registry)
    try:
        state = executor.execute(run_id, plan, context)
        token_cost = max(1, len(row["prompt"].split()) + 24)
        with database.connect() as conn:
            database.init_db(conn)
            finished_at = database.now_iso()
            conn.execute(
                """
                UPDATE runs
                SET status = ?, result_json = ?, error = NULL, token_cost = ?, finished_at = ?
                WHERE id = ?
                """,
                ("completed", database.encode_json(sanitize(state.result)), token_cost, finished_at, run_id),
            )
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ("completed", finished_at, row["task_id"]),
            )
            conn.commit()
    except Exception as exc:
        error = safe_error(exc)
        with database.connect() as conn:
            database.init_db(conn)
            finished_at = database.now_iso()
            database.insert_run_event(conn, run_id, "run.failed", {"error": error})
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


def _load_user(user_id: str) -> dict:
    with database.connect() as conn:
        database.init_db(conn)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return {"id": user_id, "permissions": []}
        return {
            "id": row["id"],
            "permissions": database.decode_json(row["permissions_json"], []),
            "roles": database.decode_json(row["roles_json"], []),
        }
