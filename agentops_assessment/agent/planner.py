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


SKU_PATTERN = re.compile(r"\b[A-Z]{2,}-[A-Z0-9-]+\b", re.IGNORECASE)


def extract_sku(text: str) -> str | None:
    match = SKU_PATTERN.search(text.upper())
    return match.group(0) if match else None


def infer_intent(text: str) -> str:
    lowered = text.lower()
    analysis_only_markers = [
        "只分析",
        "仅分析",
        "不创建",
        "不要创建",
        "不生成oa",
        "不生成 oa",
        "不生成审批草稿",
        "不创建审批草稿",
        "不创建 oa",
        "不创建oa",
        "不要实际创建",
        "analysis only",
    ]
    if any(marker in lowered for marker in analysis_only_markers):
        return "analysis_only"
    recommendation_markers = [
        "建议文本",
        "建议文案",
        "建议内容",
        "审批建议文本",
        "生成建议",
        "返回建议",
        "recommendation",
        "分析结论",
    ]
    if any(marker in lowered for marker in recommendation_markers):
        return "recommendation_text"
    create_markers = [
        "生成补货审批建议",
        "创建审批草稿",
        "创建 oa 审批草稿",
        "创建oa审批草稿",
        "生成审批草稿",
        "生成 oa 草稿",
        "生成oa草稿",
        "创建草稿",
        "提交审批",
        "发起审批",
        "oa 审批",
        "oa审批",
        "approval draft",
        "create approval",
    ]
    if any(marker in lowered for marker in create_markers):
        return "create_approval_draft"
    return "analysis_only"


class Planner:
    def __init__(self, llm: FakeLLM | None = None) -> None:
        self.llm = llm or FakeLLM()

    def create_plan(self, prompt: str, context: dict[str, Any] | None = None) -> list[PlanStep]:
        """为业务请求创建多步骤工具计划。

        TODO(candidate/P0): 推断 SKU 和业务意图，选择必要工具，并返回一个
        确定性的计划。计划应覆盖 ERP、BI、知识库、必要的供应商风险
        和可能的 OA 审批步骤，不能写死单个用户、SKU 或样例 prompt。
        """
        context = context or {}
        title = context.get("title", "")
        text = f"{title}\n{prompt}"
        llm_result = self.llm.complete(text)
        sku = extract_sku(text)
        intent = infer_intent(text)
        if not sku:
            return [
                PlanStep(
                    id="invalid_request",
                    tool_name="system.fail",
                    description="无法从请求中识别 SKU。",
                    input_template={"reason": "missing_sku", "intent": intent, "llm": llm_result},
                )
            ]
        return [
            PlanStep(
                id="get_inventory",
                tool_name="erp.get_inventory",
                description="读取 ERP 库存数据。",
                input_template={"sku": sku, "intent": intent},
            ),
            PlanStep(
                id="get_sales",
                tool_name="bi.get_sales",
                description="读取 BI 销售和预测数据。",
                input_template={"sku": sku},
            ),
            PlanStep(
                id="search_policy",
                tool_name="knowledge.search",
                description="检索库存和审批规则。",
                input_template={
                    "query": f"{sku} 库存异常 补货 审批 规则",
                    "top_k": 3,
                },
            ),
            PlanStep(
                id="get_supplier_risk",
                tool_name="supplier.get_risk",
                description="查询供应商风险。",
                input_template={"supplier_id": "$inventory.supplier_id"},
            ),
            PlanStep(
                id="maybe_create_approval",
                tool_name="oa.create_approval_draft",
                description="在意图、业务规则和权限允许时创建 OA 审批草稿。",
                input_template={"sku": sku, "intent": intent},
            ),
        ]
