from __future__ import annotations

from pathlib import Path

from agentops_assessment.agent.executor import Executor
from agentops_assessment.agent.planner import Planner
from agentops_assessment.agent.tools import ToolRegistry
from agentops_assessment.backend import database


def execute_run(run_id: str) -> None:
    """后台执行入口。

    用完整的 Planner -> Executor 流程替换此占位实现。
    预期实现应更新 running/completed/failed 状态，持久化步骤事件，
    通过 ToolRegistry 调用工具，记录 token 成本，并保存最终业务结果。
    """
    fixtures_dir = Path("fixtures")
    registry = ToolRegistry.with_default_clients(fixtures_dir=fixtures_dir, retry_attempts=2)
    planner = Planner()
    executor = Executor(registry=registry)

    with database.connect() as conn:
        database.init_db(conn)
        now = database.now_iso()
        conn.execute(
            "UPDATE runs SET status = ?, started_at = ? WHERE id = ?",
            ("running", now, run_id),
        )
        conn.commit()

    try:
        run_row = None
        with database.connect() as conn:
            database.init_db(conn)
            run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

        if not run_row:
            with database.connect() as conn:
                database.init_db(conn)
                now = database.now_iso()
                conn.execute(
                    "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                    ("failed", "Run not found", now, run_id),
                )
                conn.commit()
            return

        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (run_row["task_id"],)).fetchone()
        prompt = task_row["prompt"] if task_row else ""

        database.insert_run_event(
            conn,
            run_id,
            "run.started",
            {"message": "Planner 开始创建计划"},
        )

        context = {}
        plan = planner.create_plan(prompt, context)

        database.insert_run_event(
            conn,
            run_id,
            "plan.created",
            {"steps": [{"id": s.id, "tool_name": s.tool_name} for s in plan]},
        )

        def call_tool(name: str, args: dict):
            database.insert_run_event(conn, run_id, "tool.call", {"tool": name, "args": args}, tool_name=name)
            result = registry.call(name, args)
            database.insert_run_event(conn, run_id, "tool.result", {"tool": name, "result": result}, tool_name=name)
            return result

        state = executor.execute(run_id, plan, context, call_tool_fn=call_tool)

        for step in state.steps:
            database.insert_run_event(
                conn,
                run_id,
                "step.completed" if step.status == "completed" else "step.failed",
                {"step_id": step.step_id, "tool_name": step.tool_name, "output": step.output, "error": step.error},
                tool_name=step.tool_name,
            )

        token_cost = sum(
            step.output.get("prompt_tokens", 0) + step.output.get("completion_tokens", 0)
            for step in state.steps
            if step.output and isinstance(step.output, dict)
        )

        with database.connect() as conn:
            database.init_db(conn)
            now = database.now_iso()
            conn.execute(
                "UPDATE runs SET status = ?, result_json = ?, token_cost = ?, finished_at = ? WHERE id = ?",
                (state.status, database.encode_json(state.result), token_cost, now, run_id),
            )
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (state.status, now, task_row["id"]),
            )
            conn.commit()

    except Exception as exc:
        with database.connect() as conn:
            database.init_db(conn)
            now = database.now_iso()
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                ("failed", str(exc), now, run_id),
            )
            if task_row:
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    ("failed", now, task_row["id"]),
                )
            conn.commit()
        database.insert_run_event(
            conn,
            run_id,
            "run.failed",
            {"error": str(exc)},
        )
