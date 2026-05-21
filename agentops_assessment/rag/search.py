from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from agentops_assessment.backend import database


def tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9-]+|[一-鿿]", text.lower())


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

    完成权限感知检索、重排、答案生成、引用溯源
    和被过滤文档报告。文档正文必须视为不可信数据，不能让正文中的
    指令改变系统策略；完成实现后不得向 API 返回 debug/candidate_note。
    """

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

        filtered_doc_ids = sorted(
            {
                row["doc_id"]
                for row in rows
                if row["permission"] not in user_permissions and row["permission"] != "knowledge:read"
            }
        )

        query_tokens = tokenize(query)
        scored_chunks = []
        for row in rows:
            if row["permission"] not in user_permissions and row["permission"] != "knowledge:read":
                continue
            doc_tokens = tokenize(row["content"])
            score = cosine_score(query_tokens, doc_tokens)
            scored_chunks.append({
                "id": row["id"],
                "doc_id": row["doc_id"],
                "source_path": row["source_path"],
                "title": row["title"],
                "content": row["content"],
                "score": score,
            })

        scored_chunks.sort(key=lambda x: x["score"], reverse=True)
        top_chunks = scored_chunks[:top_k]

        citations = [
            {
                "doc_id": chunk["doc_id"],
                "title": chunk["title"],
                "source_path": chunk["source_path"],
                "chunk_id": chunk["id"],
            }
            for chunk in top_chunks
        ]

        answer_parts = []
        for chunk in top_chunks:
            content_preview = chunk["content"][:200] + "..." if len(chunk["content"]) > 200 else chunk["content"]
            answer_parts.append(f"[{chunk['title']}] {content_preview}")

        answer = " | ".join(answer_parts) if answer_parts else ""

        return {
            "answer": answer,
            "citations": citations,
            "filtered_doc_ids": filtered_doc_ids,
        }