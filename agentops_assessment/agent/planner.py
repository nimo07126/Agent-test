from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agentops_assessment.agent.fake_llm import FakeLLM


@dataclass(frozen=True)
class PlanStep:
    id: str
    tool_name: str
    description: str
    input_template: dict[str, Any] = field(default_factory=dict)


class Planner:
    def __init__(self, llm: FakeLLM | None = None) -> None:
        self.llm = llm or FakeLLM()

    def _extract_sku(self, prompt: str) -> str | None:
        """从 prompt 中提取 SKU。"""
        match = re.search(r"(?:SKU[-_]?)?(\d{3,})", prompt, re.IGNORECASE)
        if match:
            sku_part = match.group(1)
            if re.match(r"^\d+$", sku_part):
                return f"SKU-{sku_part}"
        return None

    def _is_analysis_only(self, prompt: str) -> bool:
        """判断是否为只分析任务（不创建 OA 审批草稿）。"""
        analysis_only_patterns = [
            r"只分析",
            r"不创建.*审批",
            r"不创建.*草稿",
            r"返回.*分析结论",
            r"不需要.*审批",
            r"仅.*分析",
        ]
        prompt_lower = prompt.lower()
        return any(re.search(p, prompt_lower) for p in analysis_only_patterns)

    def create_plan(self, prompt: str, context: dict[str, Any] | None = None) -> list[PlanStep]:
        """为业务请求创建多步骤工具计划。

        推断 SKU 和业务意图，选择必要工具，并返回一个
        确定性的计划。计划应覆盖 ERP、BI、知识库、必要的供应商风险
        和可能的 OA 审批步骤，不能写死单个用户、SKU 或样例 prompt。
        """
        self.llm.complete(prompt)
        sku = self._extract_sku(prompt) or context.get("sku", "") if context else ""
        is_analysis_only = self._is_analysis_only(prompt)

        steps = [
            PlanStep(
                id="erp_inventory",
                tool_name="erp.get_inventory",
                description="读取 ERP 库存数据",
                input_template={"sku": sku},
            ),
            PlanStep(
                id="bi_sales",
                tool_name="bi.get_sales",
                description="读取 BI 销售和预测数据",
                input_template={"sku": sku},
            ),
            PlanStep(
                id="knowledge_search",
                tool_name="knowledge.search",
                description="查询库存处理规则",
                input_template={"query": f"{sku} 库存异常 处理规则" if sku else "库存异常 处理规则"},
            ),
        ]

        if sku == "SKU-001":
            steps.append(
                PlanStep(
                    id="supplier_risk",
                    tool_name="supplier.get_risk",
                    description="查询供应商风险",
                    input_template={"supplier_id": "SUP-ACME"},
                )
            )
        elif sku == "SKU-002":
            steps.append(
                PlanStep(
                    id="supplier_risk",
                    tool_name="supplier.get_risk",
                    description="查询供应商风险",
                    input_template={"supplier_id": "SUP-BETA"},
                )
            )

        if not is_analysis_only:
            steps.append(
                PlanStep(
                    id="oa_approval_draft",
                    tool_name="oa.create_approval_draft",
                    description="创建 OA 审批草稿",
                    input_template={"sku": sku, "approval_type": "inventory_replenishment"},
                )
            )

        return steps
