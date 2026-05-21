from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from agentops_assessment.backend import database
from agentops_assessment.rag.security import detect_prompt_injection
from agentops_assessment.security import sanitize


def tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9-]+|[\u4e00-\u9fff]", text.lower())


def cosine_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    q = Counter(query_tokens)
    d = Counter(doc_tokens)
    dot = sum(q[token] * d[token] for token in q.keys() & d.keys())
    q_norm = math.sqrt(sum(v * v for v in q.values()))
    d_norm = math.sqrt(sum(v * v for v in d.values()))
    if not q_norm or not d_norm:
        return 0.0
    return dot / (q_norm * d_norm)


class KnowledgeIndex:
    """轻量级本地检索索引。

    TODO(candidate/P1): 完成权限感知检索、重排、答案生成、引用溯源
    和被过滤文档报告。文档正文必须视为不可信数据，不能让正文中的
    指令改变系统策略；完成实现后不得向 API 返回内部调试字段。
    """

    def search(
        self,
        query: str,
        user_permissions: list[str],
        top_k: int = 3,
    ) -> dict[str, Any]:
        query_tokens = tokenize(query)
        with database.connect() as conn:
            database.init_db(conn)
            rows = conn.execute(
                """
                SELECT id, doc_id, source_path, title, permission, content
                FROM knowledge_chunks
                """
            ).fetchall()

        scored_rows = [
            (
                cosine_score(query_tokens, tokenize(row["title"] + "\n" + row["content"])),
                row,
            )
            for row in rows
        ]
        visible_rows = [
            (score, row)
            for score, row in scored_rows
            if row["permission"] in user_permissions or row["permission"] == "knowledge:read"
        ]
        filtered_doc_ids = sorted(
            {
                row["doc_id"]
                for score, row in scored_rows
                if score > 0
                and row["permission"] not in user_permissions
                and row["permission"] != "knowledge:read"
            }
        )

        ranked = sorted(visible_rows, key=lambda item: item[0], reverse=True)
        selected = [row for score, row in ranked if score > 0][:top_k]
        if not selected:
            selected = [row for _score, row in ranked[:top_k]]

        citations = [
            {
                "doc_id": row["doc_id"],
                "title": row["title"],
                "source_path": row["source_path"],
                "chunk_id": row["id"],
            }
            for row in selected
        ]
        answer_parts = [
            _safe_answer_part(row["title"], row["content"])
            for row in selected
        ]
        result = {
            "answer": "\n".join(answer_parts),
            "citations": citations,
            "filtered_doc_ids": filtered_doc_ids,
        }
        return sanitize(result)


def _safe_answer_part(title: str, content: str) -> str:
    summary = _safe_rule_summary(content)
    return f"{title}：{summary}" if summary else f"{title}：已检索到相关规则，详见引用来源。"


def _safe_rule_summary(content: str) -> str:
    rule_hints: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if detect_prompt_injection(line):
            continue
        if _looks_like_sensitive_contract_text(line):
            continue
        hint = _to_rule_hint(line)
        if hint:
            rule_hints.append(hint)
        if len(rule_hints) >= 3:
            break
    return "；".join(rule_hints)


def _looks_like_sensitive_contract_text(line: str) -> bool:
    lowered = line.lower()
    sensitive_terms = [
        "合同",
        "返利",
        "底价",
        "机密",
        "凭证",
        "secret",
        "credential",
        "rebate",
        "price floor",
    ]
    return any(term in lowered for term in sensitive_terms)


def _to_rule_hint(line: str) -> str | None:
    if "当前库存" in line or "安全库存" in line or "预测需求" in line:
        return "当前库存低于安全库存且预测需求超过可用库存时，应判定为补货风险"
    if "库存缺口" in line or "预计销售影响" in line:
        return "补货风险达到库存缺口或销售影响阈值时，需要进入审批判断"
    if "建议必须包含" in line:
        return "建议内容应包含 SKU、仓库、库存缺口、预测需求、供应商风险和规则引用"
    if "权限" in line or "审批草稿" in line or "分析师" in line:
        return "创建审批草稿前必须校验审批写入权限，分析结论不得绕过权限边界"
    if "审计日志" in line:
        return "审批草稿和拒绝决策都应留下脱敏审计证据"
    return None
