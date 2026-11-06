# -*- coding: utf-8 -*-
"""
对话审计路由（/api/admin/audit/messages）。
权限：教学质量专员(quality)、教务主任(director)。
筛选：role（提问人角色）、start_time / end_time（提问时间）、risk_only（仅看高风险）、
page / page_size 分页。数据来源：messages 联表 sessions/users，
匿名学生（无 user_id）提问人角色显示"游客"。
"""

from fastapi import APIRouter, Depends, Query

from app.core.security import require_roles
from app.db import database as db

router = APIRouter(prefix="/api/admin/audit")

# 全量角色选项（前端筛选下拉用）；匿名提问固定显示"游客"
ROLE_LABELS = {
    "student": "学生",
    "counselor": "辅导员",
    "scheduler": "排课专员",
    "quality": "教学质量专员",
    "director": "教务主任",
    "program_leader": "专业负责人",
    "guest": "游客",
}


@router.get("/messages")
def audit_messages(
    role: str = Query(None, description="按提问人角色筛选，guest=游客"),
    start_time: str = Query(None, description="起始时间 YYYY-MM-DD HH:MM:SS"),
    end_time: str = Query(None, description="截止时间 YYYY-MM-DD HH:MM:SS"),
    risk_only: bool = Query(False, description="仅看高风险问答"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(require_roles("quality", "director")),
):
    """
    审计列表：返回每轮问答的提问内容、回答内容、提问人角色、风险标记与命中原因。
    SQL 思路：以 user 消息为主行，用子查询挂上"同一会话中紧随其后的 assistant 回答"。
    """
    # ---- 1. 动态拼 WHERE 条件（参数化占位，防注入） ----
    where = ["m.role = 'user'"]
    params = []
    if role:
        if role == "guest":
            where.append("(s.user_id IS NULL OR u.id IS NULL)")
        else:
            where.append("u.role = ?")
            params.append(role)
    if start_time:
        where.append("m.created_at >= ?")
        params.append(start_time)
    if end_time:
        where.append("m.created_at <= ?")
        params.append(end_time)
    if risk_only:
        where.append("m.risk_flag = 1")
    where_sql = " AND ".join(where)

    # ---- 2. 总数（分页用） ----
    total = db.query_all(
        f"""SELECT COUNT(*) AS n FROM messages m
            LEFT JOIN sessions s ON m.session_id = s.id
            LEFT JOIN users u ON s.user_id = u.id
            WHERE {where_sql}""",
        tuple(params),
    )[0]["n"]

    # ---- 3. 当前页数据（先查提问行，再按 id 补齐各自的回答） ----
    rows = db.query_all(
        f"""SELECT m.id, m.session_id, m.content, m.risk_flag, m.risk_reason,
                   m.created_at, u.username, u.role AS user_role
            FROM messages m
            LEFT JOIN sessions s ON m.session_id = s.id
            LEFT JOIN users u ON s.user_id = u.id
            WHERE {where_sql}
            ORDER BY m.id DESC
            LIMIT ? OFFSET ?""",
        tuple(params + [page_size, (page - 1) * page_size]),
    )
    items = []
    for r in rows:
        # 该提问对应的回答：同会话、role=assistant、id 比提问大的最小一条
        answer = db.query_all(
            "SELECT content, citations_json FROM messages "
            "WHERE session_id = ? AND role = 'assistant' AND id > ? "
            "ORDER BY id LIMIT 1",
            (r["session_id"], r["id"]),
        )
        asker_role = r["user_role"] if r["user_role"] else "guest"
        items.append({
            "id": r["id"],
            "session_id": r["session_id"],
            "asker": r["username"] or "游客",
            "asker_role": asker_role,
            "asker_role_label": ROLE_LABELS.get(asker_role, asker_role),
            "question": r["content"],
            "answer": answer[0]["content"] if answer else "",
            "risk_flag": r["risk_flag"],
            "risk_reason": r["risk_reason"],
            "created_at": r["created_at"],
        })

    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "role_labels": ROLE_LABELS,
        },
    }
