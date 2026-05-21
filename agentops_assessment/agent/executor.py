from __future__ import annotations

from typing import Any

from agentops_assessment.backend import database
from agentops_assessment.agent.planner import PlanStep
from agentops_assessment.agent.state import InMemoryRunStateStore, RunState, StepState
from agentops_assessment.agent.tools import ToolRegistry
from agentops_assessment.security import safe_error, sanitize


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        state_store: InMemoryRunStateStore | None = None,
    ) -> None:
        self.registry = registry
        self.state_store = state_store or InMemoryRunStateStore()

    def execute(
        self,
        run_id: str,
        plan: list[PlanStep],
        context: dict[str, Any],
    ) -> RunState:
        """执行计划并持久化步骤状态。

        TODO(candidate/P0): 实现可恢复的多步骤执行、工具入参渲染、
        步骤事件持久化、错误处理和最终业务结果汇总。
        """
        state = RunState(run_id=run_id, status="running")
        self.state_store.save(state)
        scratch: dict[str, Any] = {
            "intent": context.get("intent", "analysis_only"),
            "user_permissions": context.get("user_permissions", []),
            "requested_by": context.get("requested_by"),
        }

        for step in plan:
            step_state = StepState(step_id=step.id, tool_name=step.tool_name, status="running")
            state.steps.append(step_state)
            try:
                if step.tool_name == "system.fail":
                    raise ValueError(step.input_template.get("reason", "invalid_request"))
                rendered_args = self._render_args(step.input_template, scratch, context)
                if step.id == "maybe_create_approval":
                    decision = self._approval_decision(scratch, rendered_args)
                    if not decision["should_create"]:
                        step_state.status = "skipped"
                        step_state.output = decision
                        self._event(run_id, "tool.skipped", decision, step.tool_name)
                        if decision["decision"] == "deny":
                            self._audit(
                                actor_id=scratch.get("requested_by", "system"),
                                action="oa.approval.create",
                                resource=str(scratch.get("sku", "unknown")),
                                decision="deny",
                                payload=decision,
                            )
                        continue
                    rendered_args.update(decision["payload"])
                self._event(run_id, "tool.started", {"step_id": step.id, "description": step.description}, step.tool_name)
                output = self.registry.call(step.tool_name, rendered_args)
                step_state.status = "completed"
                step_state.output = output
                self._store_output(step, output, scratch)
                if step.tool_name == "oa.create_approval_draft":
                    self._audit(
                        actor_id=scratch.get("requested_by", "system"),
                        action="oa.approval.create",
                        resource=str(scratch.get("sku", output.get("approval_draft_id", "unknown"))),
                        decision="allow",
                        payload={"approval": output, "sku": scratch.get("sku")},
                    )
                self._event(
                    run_id,
                    "tool.completed",
                    {
                        "step_id": step.id,
                        "attempts": self.registry.last_call_attempts.get(step.tool_name, 1),
                        "output": output,
                    },
                    step.tool_name,
                )
            except Exception as exc:
                step_state.status = "failed"
                step_state.error = safe_error(exc)
                state.status = "failed"
                self._event(
                    run_id,
                    "tool.failed",
                    {"step_id": step.id, "error": step_state.error},
                    step.tool_name,
                )
                self.state_store.save(state)
                raise

        state.status = "completed"
        state.result = self._build_result(scratch)
        self.state_store.save(state)
        self._event(run_id, "run.completed", {"result": state.result})
        return state

    def _event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        tool_name: str | None = None,
    ) -> None:
        with database.connect() as conn:
            database.init_db(conn)
            database.insert_run_event(conn, run_id, event_type, sanitize(payload), tool_name)

    def _audit(
        self,
        actor_id: str,
        action: str,
        resource: str,
        decision: str,
        payload: dict[str, Any],
    ) -> None:
        with database.connect() as conn:
            database.init_db(conn)
            database.insert_audit_log(
                conn,
                actor_id=actor_id,
                action=action,
                resource=resource,
                decision=decision,
                payload=sanitize(payload),
            )

    def _render_args(
        self,
        template: dict[str, Any],
        scratch: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        args: dict[str, Any] = {}
        for key, value in template.items():
            if isinstance(value, str) and value.startswith("$"):
                args[key] = self._resolve(value[1:], scratch)
            else:
                args[key] = value
        if "user_permissions" not in args:
            args["user_permissions"] = context.get("user_permissions", [])
        return args

    def _resolve(self, dotted_path: str, scratch: dict[str, Any]) -> Any:
        value: Any = scratch
        for part in dotted_path.split("."):
            value = value[part]
        return value

    def _store_output(self, step: PlanStep, output: dict[str, Any], scratch: dict[str, Any]) -> None:
        if step.id == "get_inventory":
            scratch["inventory"] = output
            scratch["sku"] = output.get("sku", step.input_template.get("sku"))
        elif step.id == "get_sales":
            scratch["sales"] = output
        elif step.id == "search_policy":
            scratch["knowledge"] = output
        elif step.id == "get_supplier_risk":
            scratch["supplier_risk"] = output
        elif step.id == "maybe_create_approval":
            scratch["approval"] = output

    def _approval_decision(self, scratch: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        intent = args.get("intent") or scratch.get("intent")
        if intent == "analysis_only":
            return {
                "should_create": False,
                "decision": "skip",
                "reason": "analysis_only_intent",
            }
        permissions = set(scratch.get("user_permissions", []))
        if "oa:approval:write" not in permissions:
            return {
                "should_create": False,
                "decision": "deny",
                "reason": "missing_permission",
                "missing_permissions": ["oa:approval:write"],
            }
        inventory = scratch.get("inventory", {})
        sales = scratch.get("sales", {})
        stock_gap = int(inventory.get("stock_gap", 0))
        sales_impact = float(sales.get("sales_usd_14d", 0))
        if stock_gap < 30 and sales_impact <= 5000:
            return {
                "should_create": False,
                "decision": "skip",
                "reason": "business_threshold_not_met",
            }
        payload = {
            "sku": scratch.get("sku") or args.get("sku"),
            "approval_type": "inventory_replenishment",
            "stock_gap": stock_gap,
            "forecast_units_next_14d": sales.get("forecast_units_next_14d"),
            "supplier_risk": scratch.get("supplier_risk", {}),
        }
        return {"should_create": True, "decision": "allow", "payload": payload}

    def _build_result(self, scratch: dict[str, Any]) -> dict[str, Any]:
        inventory = scratch.get("inventory", {})
        sales = scratch.get("sales", {})
        knowledge = scratch.get("knowledge", {})
        result = {
            "sku": scratch.get("sku"),
            "name": inventory.get("name"),
            "warehouse": inventory.get("warehouse"),
            "current_stock": inventory.get("current_stock"),
            "safety_stock": inventory.get("safety_stock"),
            "reserved_stock": inventory.get("reserved_stock"),
            "stock_gap": inventory.get("stock_gap"),
            "forecast_units_next_14d": sales.get("forecast_units_next_14d"),
            "stockout_risk": sales.get("stockout_risk"),
            "supplier_risk": scratch.get("supplier_risk"),
            "citations": knowledge.get("citations", []),
            "filtered_doc_ids": knowledge.get("filtered_doc_ids", []),
            "analysis": knowledge.get("answer", ""),
        }
        approval = scratch.get("approval")
        if approval:
            result.update(approval)
        return sanitize(result)
