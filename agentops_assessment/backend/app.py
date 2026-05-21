from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status

from agentops_assessment.admin.metrics import build_dashboard
from agentops_assessment.backend import database
from agentops_assessment.backend.auth import get_current_user, require_permissions
from agentops_assessment.backend.schemas import (
    KnowledgeSearchRequest,
    RunCreateOut,
    RunOut,
    TaskCreate,
    TaskOut,
)
from agentops_assessment.backend.worker import execute_run
from agentops_assessment.rag.security import detect_prompt_injection
from agentops_assessment.rag.search import KnowledgeIndex
from agentops_assessment.agent.planner import _analysis_only, _approval_requested


def _task_from_row(row) -> TaskOut:
    return TaskOut(**dict(row))


def _run_from_row(row) -> RunOut:
    data = dict(row)
    data["result"] = database.decode_json(data.pop("result_json"), None)
    return RunOut(**data)


def _can_read_run(row, user: dict) -> bool:
    return (
        row["requested_by"] == user["id"]
        or row["created_by"] == user["id"]
        or "admin:read" in user["permissions"]
    )


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with database.connect() as conn:
            database.init_db(conn)
        yield

    app = FastAPI(
        title="AgentOps 迷你测评服务",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
    def create_task(
        body: TaskCreate,
        user: dict = Depends(require_permissions("tasks:create")),
    ) -> TaskOut:
        injection_matches = detect_prompt_injection(f"{body.title}\n{body.prompt}")
        if injection_matches:
            with database.connect() as conn:
                database.init_db(conn)
                database.insert_audit_log(
                    conn,
                    actor_id=user["id"],
                    action="task.rejected",
                    resource="task",
                    payload={"reason": "prompt_injection_detected", "patterns": injection_matches},
                    decision="deny",
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "prompt_injection_detected"},
            )
        task_id = str(uuid.uuid4())
        now = database.now_iso()
        with database.connect() as conn:
            database.init_db(conn)
            conn.execute(
                """
                INSERT INTO tasks (id, created_by, title, prompt, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, user["id"], body.title, body.prompt, "created", now, now),
            )
            database.insert_audit_log(
                conn,
                actor_id=user["id"],
                action="task.create",
                resource=task_id,
                payload={"title": body.title},
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task_from_row(row)

    @app.post(
        "/api/tasks/{task_id}/run",
        response_model=RunCreateOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def run_task(
        task_id: str,
        background_tasks: BackgroundTasks,
        user: dict = Depends(require_permissions("tasks:run")),
    ) -> RunCreateOut:
        # TODO(candidate/P1): 创建运行前校验工具级权限。
        run_id = str(uuid.uuid4())
        now = database.now_iso()
        with database.connect() as conn:
            database.init_db(conn)
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="任务不存在。")
            if task["created_by"] != user["id"] and "admin:read" not in user["permissions"]:
                database.insert_audit_log(
                    conn,
                    actor_id=user["id"],
                    action="run.create.deny",
                    resource=task_id,
                    payload={"reason": "task_not_visible"},
                    decision="deny",
                )
                raise HTTPException(status_code=403, detail="无权运行该任务。")
            if (
                _approval_requested(task["prompt"])
                and not _analysis_only(task["prompt"])
                and "oa:approval:write" not in user["permissions"]
            ):
                database.insert_audit_log(
                    conn,
                    actor_id=user["id"],
                    action="run.create.deny",
                    resource=task_id,
                    payload={"missing_permissions": ["oa:approval:write"]},
                    decision="deny",
                )
                raise HTTPException(
                    status_code=403,
                    detail={"missing_permissions": ["oa:approval:write"]},
                )
            conn.execute(
                """
                INSERT INTO runs (id, task_id, requested_by, status, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, task_id, user["id"], "queued", now),
            )
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ("queued", now, task_id),
            )
            database.insert_audit_log(
                conn,
                actor_id=user["id"],
                action="run.create",
                resource=run_id,
                payload={"task_id": task_id},
            )
        background_tasks.add_task(execute_run, run_id)
        return RunCreateOut(run_id=run_id, task_id=task_id, status="queued")

    @app.get("/api/runs/{run_id}", response_model=RunOut)
    def get_run(run_id: str, user: dict = Depends(get_current_user)) -> RunOut:
        with database.connect() as conn:
            database.init_db(conn)
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="运行记录不存在。")
            joined = conn.execute(
                """
                SELECT runs.*, tasks.created_by
                FROM runs
                JOIN tasks ON tasks.id = runs.task_id
                WHERE runs.id = ?
                """,
                (run_id,),
            ).fetchone()
            if not _can_read_run(joined, user):
                database.insert_audit_log(
                    conn,
                    actor_id=user["id"],
                    action="run.read.deny",
                    resource=run_id,
                    payload={"reason": "run_not_visible"},
                    decision="deny",
                )
                raise HTTPException(status_code=403, detail="无权查看该运行记录。")
            database.insert_audit_log(
                conn,
                actor_id=user["id"],
                action="run.read",
                resource=run_id,
                payload={},
            )
        return _run_from_row(row)

    @app.get("/api/runs/{run_id}/events")
    def get_run_events(run_id: str, user: dict = Depends(get_current_user)) -> dict[str, Any]:
        with database.connect() as conn:
            database.init_db(conn)
            run = conn.execute(
                """
                SELECT runs.*, tasks.created_by
                FROM runs
                JOIN tasks ON tasks.id = runs.task_id
                WHERE runs.id = ?
                """,
                (run_id,),
            ).fetchone()
            if not run:
                raise HTTPException(status_code=404, detail="运行记录不存在。")
            if not _can_read_run(run, user):
                database.insert_audit_log(
                    conn,
                    actor_id=user["id"],
                    action="run.events.read.deny",
                    resource=run_id,
                    payload={"reason": "run_not_visible"},
                    decision="deny",
                )
                raise HTTPException(status_code=403, detail="无权查看该运行事件。")
            rows = conn.execute(
                """
                SELECT seq, type, tool_name, payload_json, created_at
                FROM run_events
                WHERE run_id = ?
                ORDER BY seq ASC
                """,
                (run_id,),
            ).fetchall()
            database.insert_audit_log(
                conn,
                actor_id=user["id"],
                action="run.events.read",
                resource=run_id,
                payload={},
            )
        return {
            "run_id": run_id,
            "events": [
                {
                    "seq": row["seq"],
                    "type": row["type"],
                    "tool_name": row["tool_name"],
                    "payload": database.decode_json(row["payload_json"], {}),
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    @app.post("/api/knowledge/search")
    def search_knowledge(
        body: KnowledgeSearchRequest,
        user: dict = Depends(require_permissions("knowledge:read")),
    ) -> dict[str, Any]:
        index = KnowledgeIndex()
        result = index.search(
            body.query,
            user_permissions=user["permissions"],
            top_k=body.top_k,
        )
        return result

    @app.get("/api/admin/dashboard")
    def admin_dashboard(user: dict = Depends(require_permissions("admin:read"))) -> dict[str, Any]:
        with database.connect() as conn:
            database.init_db(conn)
            database.insert_audit_log(
                conn,
                actor_id=user["id"],
                action="admin.dashboard.read",
                resource="dashboard",
                payload={},
            )
            return build_dashboard(conn)

    @app.get("/api/admin/audit-logs")
    def admin_audit_logs(user: dict = Depends(require_permissions("admin:read"))) -> dict[str, Any]:
        with database.connect() as conn:
            database.init_db(conn)
            rows = conn.execute(
                """
                SELECT actor_id, action, resource, decision, payload_json, created_at
                FROM audit_logs
                ORDER BY id DESC
                LIMIT 100
                """
            ).fetchall()
        return {
            "logs": [
                {
                    "actor_id": row["actor_id"],
                    "action": row["action"],
                    "resource": row["resource"],
                    "decision": row["decision"],
                    "payload": database.decode_json(row["payload_json"], {}),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    return app


app = create_app()
