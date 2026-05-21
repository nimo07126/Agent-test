from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from agentops_assessment.backend import database
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

        visible_rows = [
            row
            for row in rows
            if row["permission"] in user_permissions or row["permission"] == "knowledge:read"
        ]
        filtered_doc_ids = sorted(
            {
                row["doc_id"]
                for row in rows
                if row["permission"] not in user_permissions and row["permission"] != "knowledge:read"
            }
        )

        ranked = sorted(
            (
                (
                    cosine_score(query_tokens, tokenize(row["title"] + "\n" + row["content"])),
                    row,
                )
                for row in visible_rows
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        selected = [row for score, row in ranked if score > 0][:top_k]
        if not selected:
            selected = visible_rows[:top_k]

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
            f"{row['title']}：{_safe_summary(row['content'])}"
            for row in selected
        ]
        result = {
            "answer": "\n".join(answer_parts),
            "citations": citations,
            "filtered_doc_ids": filtered_doc_ids,
        }
        return sanitize(result)


def _safe_summary(content: str) -> str:
    safe_lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "忽略" in line or "泄露" in line or "secret" in line.lower():
            continue
        safe_lines.append(line)
        if len("".join(safe_lines)) > 180:
            break
    summary = " ".join(safe_lines)
    return summary[:240]
