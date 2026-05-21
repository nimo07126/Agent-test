from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentops_assessment.agent.planner import PlanStep
from agentops_assessment.agent.state import InMemoryRunStateStore, RunState, StepState
from agentops_assessment.agent.tools import ToolRegistry
from agentops_assessment.rag.security import redact_sensitive

EventWriter = Callable[[str, dict[str, Any], str | None], None]
AuditWriter = Callable[[str, str, dict[str, Any], str], None]


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
        """执行计划并持久化步骤状态。"""
        emit_event: EventWriter = context.get("emit_event", lambda *_args: None)
        audit: AuditWriter = context.get("audit", lambda *_args: None)
        user_permissions = set(context.get("user_permissions", []))
        state = RunState(
            run_id=run_id,
            status="running",
            steps=[
                StepState(
                    step_id=step.id,
                    tool_name=step.tool_name,
                    status="pending",
                )
                for step in plan
            ],
        )
        self.state_store.save(state)

        outputs: dict[str, dict[str, Any]] = {}
        approval_skipped_reason: str | None = None
        try:
            for index, step in enumerate(plan):
                step_state = state.steps[index]
                missing = [permission for permission in step.required_permissions if permission not in user_permissions]
                if missing:
                    if step.tool_name == "oa.create_approval_draft":
                        approval_skipped_reason = f"缺少权限: {', '.join(missing)}"
                        step_state.status = "skipped"
                        step_state.output = {"approval_skipped_reason": approval_skipped_reason}
                        emit_event(
                            "tool.skipped",
                            {
                                "step_id": step.id,
                                "reason": approval_skipped_reason,
                                "missing_permissions": missing,
                            },
                            step.tool_name,
                        )
                        audit(
                            "oa.approval.skip",
                            context.get("task_id", run_id),
                            {"missing_permissions": missing, "sku": context.get("sku")},
                            "skip",
                        )
                        continue
                    raise PermissionError(f"缺少工具权限: {', '.join(missing)}")

                if step.tool_name == "oa.create_approval_draft" and not _should_create_approval(outputs):
                    approval_skipped_reason = "业务规则未达到创建 OA 审批草稿阈值。"
                    step_state.status = "skipped"
                    step_state.output = {"approval_skipped_reason": approval_skipped_reason}
                    emit_event(
                        "tool.skipped",
                        {"step_id": step.id, "reason": approval_skipped_reason},
                        step.tool_name,
                    )
                    audit(
                        "oa.approval.skip",
                        context.get("task_id", run_id),
                        {"reason": approval_skipped_reason, "sku": context.get("sku")},
                        "skip",
                    )
                    continue

                args = self._build_args(step, context, outputs)
                step_state.status = "running"
                result = self.registry.call(step.tool_name, args)
                result = redact_sensitive(result)
                attempts = self.registry.last_call_attempts.get(step.tool_name, 1)
                step_state.status = "completed"
                step_state.output = result
                outputs[step.tool_name] = result
                emit_event(
                    "tool.call",
                    {"step_id": step.id, "attempts": attempts, "result": result},
                    step.tool_name,
                )
                audit(
                    "tool.call",
                    step.tool_name,
                    {"step_id": step.id, "attempts": attempts, "result": result},
                    "allow",
                )
                if step.tool_name == "oa.create_approval_draft":
                    audit(
                        "approval.draft.create",
                        result.get("approval_draft_id", context.get("task_id", run_id)),
                        {
                            "sku": context.get("sku"),
                            "approval_type": result.get("approval_type"),
                            "payload": args,
                        },
                        "allow",
                    )

            state.result = self._build_result(context, outputs, approval_skipped_reason)
            state.status = "completed"
            self.state_store.save(state)
            return state
        except Exception as exc:
            for step_state in state.steps:
                if step_state.status == "running":
                    step_state.status = "failed"
                    step_state.error = str(exc)
                    emit_event(
                        "tool.failed",
                        {"step_id": step_state.step_id, "error": str(exc)},
                        step_state.tool_name,
                    )
                    break
            state.status = "failed"
            self.state_store.save(state)
            raise

    def _build_args(
        self,
        step: PlanStep,
        context: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        sku = context.get("sku")
        if step.tool_name in {"erp.get_inventory", "bi.get_sales"}:
            if not sku:
                raise ValueError("无法从任务提示中识别 SKU。")
            return {"sku": sku}
        if step.tool_name == "knowledge.search":
            args = dict(step.input_template)
            args["user_permissions"] = context.get("user_permissions", [])
            args.setdefault("top_k", 3)
            return args
        if step.tool_name == "supplier.get_risk":
            inventory = outputs.get("erp.get_inventory") or {}
            supplier_id = inventory.get("supplier_id")
            if not supplier_id:
                raise ValueError("ERP 数据缺少供应商编号。")
            return {"supplier_id": supplier_id}
        if step.tool_name == "oa.create_approval_draft":
            inventory = outputs.get("erp.get_inventory") or {}
            sales = outputs.get("bi.get_sales") or {}
            supplier_risk = outputs.get("supplier.get_risk") or {}
            return redact_sensitive(
                {
                    "sku": sku,
                    "warehouse": inventory.get("warehouse"),
                    "stock_gap": inventory.get("stock_gap", 0),
                    "forecast_units_next_14d": sales.get("forecast_units_next_14d", 0),
                    "stockout_risk": sales.get("stockout_risk"),
                    "supplier_risk": supplier_risk,
                    "approval_type": step.input_template.get("approval_type", "inventory_replenishment"),
                }
            )
        return dict(step.input_template)

    def _build_result(
        self,
        context: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        approval_skipped_reason: str | None,
    ) -> dict[str, Any]:
        inventory = outputs.get("erp.get_inventory") or {}
        sales = outputs.get("bi.get_sales") or {}
        knowledge = outputs.get("knowledge.search") or {}
        supplier_risk = outputs.get("supplier.get_risk") or {}
        approval = outputs.get("oa.create_approval_draft") or {}
        stock_gap = int(inventory.get("stock_gap") or 0)
        result = {
            "sku": context.get("sku"),
            "warehouse": inventory.get("warehouse"),
            "current_stock": inventory.get("current_stock"),
            "safety_stock": inventory.get("safety_stock"),
            "reserved_stock": inventory.get("reserved_stock"),
            "stock_gap": stock_gap,
            "forecast_units_next_14d": sales.get("forecast_units_next_14d"),
            "stockout_risk": sales.get("stockout_risk"),
            "supplier_risk": supplier_risk,
            "citations": knowledge.get("citations", []),
            "filtered_doc_ids": knowledge.get("filtered_doc_ids", []),
            "recommendation": _recommendation(stock_gap, sales, supplier_risk),
        }
        if approval:
            result.update(approval)
            result["recommended_action"] = "create_replenishment_approval"
        elif approval_skipped_reason:
            result["approval_skipped_reason"] = approval_skipped_reason
            result["recommended_action"] = "analysis_only"
        else:
            result["recommended_action"] = "monitor_inventory" if stock_gap <= 0 else "prepare_replenishment_analysis"
        return redact_sensitive(result)


def _should_create_approval(outputs: dict[str, dict[str, Any]]) -> bool:
    inventory = outputs.get("erp.get_inventory") or {}
    sales = outputs.get("bi.get_sales") or {}
    current_stock = int(inventory.get("current_stock") or 0)
    reserved_stock = int(inventory.get("reserved_stock") or 0)
    available_stock = max(0, current_stock - reserved_stock)
    stock_gap = int(inventory.get("stock_gap") or 0)
    forecast = int(sales.get("forecast_units_next_14d") or 0)
    sales_impact = float(sales.get("sales_usd_14d") or 0)
    return stock_gap > 0 and forecast > available_stock and (stock_gap >= 30 or sales_impact > 5000)


def _recommendation(stock_gap: int, sales: dict[str, Any], supplier_risk: dict[str, Any]) -> str:
    if stock_gap <= 0:
        return "当前库存未低于安全库存，建议继续监控。"
    forecast = sales.get("forecast_units_next_14d", 0)
    risk_level = supplier_risk.get("risk_level", "unknown")
    return f"建议补货并复核审批，库存缺口 {stock_gap} 件，未来 14 天预测需求 {forecast} 件，供应商风险 {risk_level}。"
