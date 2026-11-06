# -*- coding: utf-8 -*-
"""
人工协作工作流：调课草稿确认流 + 交接单据闭环。【★核心模块·可复用】
状态迁移全部为纯函数，返回 (ok, err_code)，路由层负责转 JSON 响应：
  草稿:   pending_confirm ──confirm/reject(仅排课专员)──▶ confirmed / rejected（终态）
  交接单: pending ──accept(被派人本人)──▶ accepted ──complete(填处理结果)──▶ completed
          pending/accepted 到期未处理 ──sweep──▶ escalated（改派教务主任重新待处理）
"""

import json

from app.db import database as db

# 草稿/交接单的可迁移状态判断表（终态单据一律拒绝操作 → 1012）
DRAFT_OPEN_STATUS = {"pending_confirm"}
TASK_OPEN_STATUS = {"pending", "escalated"}       # 可认领的状态


# ==================== 调课草稿 ====================

def confirm_draft(draft_id: int, user_id: int) -> tuple:
    """
    确认草稿：status=confirmed + 签核人落库，
    并回填 conflict_replay_records.draft_id / confirmed_by（审计链闭环）。
    """
    rows = db.query_all("SELECT * FROM drafts WHERE id = ?", (draft_id,))
    if not rows:
        return False, "1001"
    draft = rows[0]
    if draft["status"] not in DRAFT_OPEN_STATUS:
        return False, "1012"                        # 终态不可再操作
    with db._lock:
        conn = db.get_conn()
        conn.execute(
            "UPDATE drafts SET status = 'confirmed', confirmed_by = ?, confirmed_at = ? "
            "WHERE id = ?", (user_id, db.now(), draft_id),
        )
        # business_no 在回放记录表唯一，定向回填签核信息
        conn.execute(
            "UPDATE conflict_replay_records SET draft_id = ?, confirmed_by = ? "
            "WHERE business_no = ?", (draft_id, user_id, draft["business_no"]),
        )
        conn.commit()
    return True, None


def reject_draft(draft_id: int, user_id: int, reason: str) -> tuple:
    """驳回草稿：status=rejected，理由必填并落库。"""
    if not (reason or "").strip():
        return False, "1001"                        # 驳回必须说明理由
    rows = db.query_all("SELECT * FROM drafts WHERE id = ?", (draft_id,))
    if not rows:
        return False, "1001"
    if rows[0]["status"] not in DRAFT_OPEN_STATUS:
        return False, "1012"
    db.execute(
        "UPDATE drafts SET status = 'rejected', confirmed_by = ?, confirmed_at = ?, "
        "reject_reason = ? WHERE id = ?",
        (user_id, db.now(), reason.strip(), draft_id),
    )
    return True, None


# ==================== 交接单据 ====================

def sweep_escalations() -> int:
    """
    超时升级（惰性触发：每次查交接单列表前调用）：
    escalate_at 已到期且仍处待处理状态的单据 → status=escalated、escalated=1、
    改派教务主任（试点期取第一个在职 director），重新进入待认领。
    返回本次升级的单据数。
    """
    director = db.query_all(
        "SELECT id FROM users WHERE role = 'director' AND is_active = 1 ORDER BY id LIMIT 1"
    )
    director_id = director[0]["id"] if director else None
    rows = db.query_all(
        "SELECT id FROM handover_tasks WHERE escalated = 0 "
        "AND status IN ('pending', 'assigned') AND escalate_at <= ?",
        (db.now(),),
    )
    for r in rows:
        db.execute(
            "UPDATE handover_tasks SET status = 'escalated', escalated = 1, assignee_id = ? "
            "WHERE id = ?", (director_id, r["id"]),
        )
    return len(rows)


def accept_task(task_id: int, user_id: int) -> tuple:
    """认领交接单：仅被派人人本人可操作，落 accepted_at（需求要求的确认时间）。"""
    rows = db.query_all("SELECT * FROM handover_tasks WHERE id = ?", (task_id,))
    if not rows:
        return False, "1001"
    task = rows[0]
    if task["status"] not in TASK_OPEN_STATUS:
        return False, "1012"
    if task["assignee_id"] != user_id:
        return False, "1005"                        # 非本人不可认领
    db.execute(
        "UPDATE handover_tasks SET status = 'accepted', accepted_at = ? WHERE id = ?",
        (db.now(), task_id),
    )
    return True, None


def complete_task(task_id: int, user_id: int, resolution: str) -> tuple:
    """完成交接单：仅被派人本人；处理结果必填（审计要求留痕处理过程）。"""
    if not (resolution or "").strip():
        return False, "1001"
    rows = db.query_all("SELECT * FROM handover_tasks WHERE id = ?", (task_id,))
    if not rows:
        return False, "1001"
    task = rows[0]
    if task["status"] != "accepted":
        return False, "1012"                        # 只有处理中（已认领）可完成
    if task["assignee_id"] != user_id:
        return False, "1005"
    db.execute(
        "UPDATE handover_tasks SET status = 'completed', resolution = ? WHERE id = ?",
        (resolution.strip(), task_id),
    )
    return True, None


# ==================== 查询（供路由层使用） ====================

def list_drafts(status: str = None) -> list:
    """草稿列表（新→旧），联回放记录带出业务编号；content_json 解析成结构化明细。"""
    where, params = ["1=1"], []
    if status:
        where.append("d.status = ?")
        params.append(status)
    rows = db.query_all(
        f"""SELECT d.id, d.business_no, d.status, d.content_json, d.created_by_session,
                   d.confirmed_by, d.confirmed_at, d.reject_reason, d.created_at,
                   u.username AS confirmed_by_name, r.data_source, r.snapshot_at
            FROM drafts d
            LEFT JOIN users u ON d.confirmed_by = u.id
            LEFT JOIN conflict_replay_records r ON d.business_no = r.business_no
            WHERE {' AND '.join(where)} ORDER BY d.id DESC LIMIT 100""",
        tuple(params),
    )
    items = []
    for r in rows:
        try:
            content = json.loads(r["content_json"] or "{}")
        except json.JSONDecodeError:
            content = {}
        items.append({
            "id": r["id"], "business_no": r["business_no"], "status": r["status"],
            "adjustments": content.get("adjustments", []),
            "reason": content.get("reason", ""),
            "confirmed_by_name": r["confirmed_by_name"],
            "confirmed_at": r["confirmed_at"], "reject_reason": r["reject_reason"],
            "data_source": r["data_source"], "snapshot_at": r["snapshot_at"],
            "created_at": r["created_at"],
        })
    return items


def list_handover(status: str = None, keyword: str = None, mine: bool = None,
                  user_id: int = None) -> list:
    """交接单列表（新→旧），联表带接手人姓名；支持状态/关键词/仅我的筛选。
    阶段 8：LEFT JOIN co_verifications 带最新验收标记（已验收·通过/有问题）。"""
    where, params = ["1=1"], []
    if status:
        where.append("h.status = ?")
        params.append(status)
    if keyword:
        where.append("(h.business_no LIKE ? OR h.title LIKE ? OR h.unresolved_items LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    if mine and user_id:
        where.append("h.assignee_id = ?")
        params.append(user_id)
    rows = db.query_all(
        f"""SELECT h.*, u.username AS assignee_name,
                   (SELECT v.verdict FROM co_verifications v
                     WHERE v.target_type = 'handover' AND v.target_ref = CAST(h.id AS TEXT)
                     ORDER BY v.id DESC LIMIT 1) AS verify_verdict
            FROM handover_tasks h
            LEFT JOIN users u ON h.assignee_id = u.id
            WHERE {' AND '.join(where)} ORDER BY h.id DESC LIMIT 100""",
        tuple(params),
    )
    return [dict(r) for r in rows]
