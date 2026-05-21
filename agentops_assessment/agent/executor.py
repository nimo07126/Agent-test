from __future__ import annotations

from typing import Any

from agentops_assessment.agent.planner import PlanStep
from agentops_assessment.agent.state import InMemoryRunStateStore, RunState, StepState
from agentops_assessment.agent.tools import ToolRegistry


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
        call_tool_fn: callable = None,
    ) -> RunState:
        """执行计划并持久化步骤状态。

        实现可恢复的多步骤执行、工具入参渲染、
        步骤事件持久化、错误处理和最终业务结果汇总。
        """
        steps = []
        for step in plan:
            steps.append(
                StepState(
                    step_id=step.id,
                    tool_name=step.tool_name,
                    status="pending",
                )
            )

        state = RunState(
            run_id=run_id,
            status="running",
            steps=steps,
        )
        self.state_store.save(state)

        tool_outputs = {}
        for step in plan:
            step_state = next((s for s in state.steps if s.step_id == step.id), None)
            if step_state is None:
                continue

            try:
                args = {k: v.format(**context) if isinstance(v, str) else v for k, v in step.input_template.items()}

                if call_tool_fn:
                    output = call_tool_fn(step.tool_name, args)
                else:
                    output = self.registry.call(step.tool_name, args)

                sanitized_output = self._sanitize_output(output)
                tool_outputs[step.id] = sanitized_output
                step_state.status = "completed"
                step_state.output = sanitized_output
            except Exception as exc:
                step_state.status = "failed"
                step_state.error = str(exc)

        failed_steps = [s for s in state.steps if s.status == "failed"]
        state.status = "failed" if failed_steps else "completed"
        state.result = tool_outputs if tool_outputs else None
        self.state_store.save(state)

        return state

    def _sanitize_output(self, output: dict[str, Any]) -> dict[str, Any]:
        """对工具输出做脱敏处理，移除敏感字段。"""
        sensitive_fields = {"vendor_secret", "unit_cost_usd", "debug", "candidate_note"}
        if not isinstance(output, dict):
            return output
        return {k: v for k, v in output.items() if k not in sensitive_fields}
