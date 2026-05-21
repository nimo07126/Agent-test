from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from agentops_assessment.agent.fake_llm import FakeLLM


@dataclass(frozen=True)
class PlanStep:
    id: str
    tool_name: str
    description: str
    input_template: dict[str, Any] = field(default_factory=dict)
    required_permissions: list[str] = field(default_factory=list)
    optional: bool = False


class Planner:
    def __init__(self, llm: FakeLLM | None = None) -> None:
        self.llm = llm or FakeLLM()
        self.last_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}

    def create_plan(self, prompt: str, context: dict[str, Any] | None = None) -> list[PlanStep]:
        """为业务请求创建多步骤工具计划。"""
        completion = self.llm.complete(prompt)
        self.last_usage = {
            "prompt_tokens": int(completion.get("prompt_tokens", 0)),
            "completion_tokens": int(completion.get("completion_tokens", 0)),
        }
        sku = _extract_sku(prompt)
        approval_requested = _approval_requested(prompt)
        analysis_only = _analysis_only(prompt)
        knowledge_query = (
            f"{sku or ''} 库存异常 审批规则 供应商风险".strip()
            if approval_requested
            else f"{sku or ''} 库存异常 处理规则".strip()
        )
        plan = [
            PlanStep(
                id="get_inventory",
                tool_name="erp.get_inventory",
                description="读取 ERP 库存、供应商和安全库存数据。",
                input_template={"sku": sku},
                required_permissions=["erp:read"],
            ),
            PlanStep(
                id="get_sales_forecast",
                tool_name="bi.get_sales",
                description="读取 BI 销售与未来 14 天预测。",
                input_template={"sku": sku},
                required_permissions=["bi:read"],
            ),
            PlanStep(
                id="search_policy",
                tool_name="knowledge.search",
                description="检索库存异常和审批规则并返回引用。",
                input_template={"query": knowledge_query, "top_k": 3},
                required_permissions=["knowledge:read"],
            ),
            PlanStep(
                id="get_supplier_risk",
                tool_name="supplier.get_risk",
                description="按 ERP 供应商编号查询供应商风险。",
                input_template={"supplier_id": "$inventory.supplier_id"},
                required_permissions=["supplier:read"],
            ),
        ]
        if approval_requested and not analysis_only:
            plan.append(
                PlanStep(
                    id="create_approval_draft",
                    tool_name="oa.create_approval_draft",
                    description="在业务规则满足时创建 OA 补货审批草稿。",
                    input_template={"approval_type": "inventory_replenishment"},
                    required_permissions=["oa:approval:write"],
                    optional=True,
                )
            )
        return plan


def _extract_sku(text: str) -> str | None:
    match = re.search(r"\b[A-Z]{2,}[-_][A-Z0-9-]+\b", text.upper())
    return match.group(0).replace("_", "-") if match else None


def _approval_requested(text: str) -> bool:
    lowered = text.lower()
    markers = ["审批", "草稿", "补货", "approval", "draft", "replenishment"]
    return any(marker in lowered for marker in markers)


def _analysis_only(text: str) -> bool:
    lowered = text.lower()
    blockers = ["只分析", "仅分析", "不要创建", "无需审批", "不创建", "analysis only", "do not create"]
    return any(marker in lowered for marker in blockers)
