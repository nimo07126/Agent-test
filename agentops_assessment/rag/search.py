from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from agentops_assessment.backend import database
from agentops_assessment.rag.security import scrub_untrusted_text


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


def phrase_score(query: str, document: str) -> float:
    phrases = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9-]{3,}", query.lower()))
    if not phrases:
        return 0.0
    doc_lower = document.lower()
    return sum(1.0 for phrase in phrases if phrase in doc_lower) / len(phrases)


class KnowledgeIndex:
    """轻量级本地检索索引。"""

    def search(
        self,
        query: str,
        user_permissions: list[str],
        top_k: int = 3,
    ) -> dict[str, Any]:
        with database.connect() as conn:
            database.init_db(conn)
            rows = conn.execute(
                """
                SELECT id, doc_id, source_path, title, permission, content
                FROM knowledge_chunks
                """
            ).fetchall()

        allowed_rows = []
        filtered_doc_ids: set[str] = set()
        permissions = set(user_permissions)
        for row in rows:
            required_permission = row["permission"]
            if required_permission != "knowledge:read" and required_permission not in permissions:
                filtered_doc_ids.add(row["doc_id"])
                continue
            allowed_rows.append(row)

        query_tokens = tokenize(scrub_untrusted_text(query))
        scored = []
        for row in allowed_rows:
            doc_text = f"{row['title']}\n{row['content']}"
            safe_doc_text = scrub_untrusted_text(doc_text)
            score = cosine_score(query_tokens, tokenize(safe_doc_text)) + phrase_score(query, safe_doc_text)
            if row["title"] and row["title"] in query:
                score += 1.0
            if score > 0:
                scored.append((score, row))

        if not scored and allowed_rows:
            scored = [(0.0, row) for row in allowed_rows]

        ranked = [row for _, row in sorted(scored, key=lambda item: (-item[0], item[1]["id"]))[:top_k]]
        citations = [
            {
                "doc_id": row["doc_id"],
                "title": row["title"],
                "source_path": row["source_path"],
                "chunk_id": row["id"],
            }
            for row in ranked
        ]

        if ranked:
            titles = "、".join(dict.fromkeys(row["title"] for row in ranked))
            answer = (
                f"已根据可访问知识库检索到 {len(ranked)} 条相关规则：{titles}。"
                "建议关注库存缺口、未来 14 天预测需求、供应商风险和审批阈值；"
                "请以引用中的规则为准，受限文档仅报告过滤结果，不返回正文。"
            )
        else:
            answer = "未在当前权限范围内检索到可引用的知识库规则。"

        return {
            "answer": answer,
            "citations": citations,
            "filtered_doc_ids": sorted(filtered_doc_ids),
        }
