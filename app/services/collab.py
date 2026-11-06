# -*- coding: utf-8 -*-
"""
协作角色业务（阶段 8）：辅导员转报备/现场异常登记 + 专业负责人培养方案核校/验收。
设计要点（docs/07-协作角色功能设计.md）：
  - 报备即交接单：不新建状态机，直接生成 handover_tasks，复用阶段 7 认领/完成/超时升级闭环
  - 转报备的提问原文由服务端从 messages 表取最后一条用户消息（不信任前端传参，防篡改审计链）
  - 核校与验收只留痕不判定：落 co_verifications，发现问题走既有交接单流转
状态迁移返回 (ok, err_code)，路由层负责转 JSON 响应。
"""

import time

from app.adapters import academic_api
from app.core.biz_no import next_biz_no
from app.db import database as db

# 报备单超时时限：24h 未处理自动升级教务主任（与 AI 兜底单据一致）
REPORT_ESCALATE_HOURS = 24


# ==================== 辅导员：报备 ====================

def create_report(user_id: int, kind: str, session_id: str = "",
                  title: str = "", detail: str = "") -> tuple:
    """
    新建报备单（kind: transfer=转报备 / incident=现场异常登记）。
    转报备取该会话最后一条用户提问原文做标题主体；现场异常用表单标题。
    统一生成 handover_task：creator=辅导员，assignee=排课专员，24h 超时升级。
    返回 (ok, err_code_or_None, result_dict)。
    """
    if kind not in ("transfer", "incident"):
        return False, "1001", None
    detail = (detail or "").strip()

    # ---- 组装标题/描述（服务端取证，不信任前端） ----
    if kind == "transfer":
        if not (session_id or "").strip():
            return False, "1001", None
        rows = db.query_all(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if not rows:
            return False, "1001", None          # 会话无提问记录，无审计链可挂
        question = rows[0]["content"]
        title = f"[学生转报备] {question[:60]}"
        unresolved = detail or "辅导员转报备学生咨询问题，请排课专员核实处理"
    else:
        if not (title or "").strip() or not detail:
            return False, "1001", None
        title = f"[现场异常] {title.strip()}"
        unresolved = detail

    # ---- 派给排课专员（第一个在职账号，同 AI 兜底逻辑） ----
    assignee = db.query_all(
        "SELECT id FROM users WHERE role = 'scheduler' AND is_active = 1 ORDER BY id LIMIT 1"
    )
    biz_no = next_biz_no()
    escalate_at = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + REPORT_ESCALATE_HOURS * 3600)
    )
    db.execute(
        "INSERT INTO handover_tasks (business_no, version, status, title, unresolved_items, "
        "creator_id, assignee_id, escalate_at, created_at) "
        "VALUES (?, 1, 'pending', ?, ?, ?, ?, ?, ?)",
        (biz_no, title, unresolved, user_id,
         assignee[0]["id"] if assignee else None, escalate_at, db.now()),
    )
    return True, None, {
        "business_no": biz_no,
        "escalate_at": escalate_at,
        "message": "报备单已生成，已派给排课专员，24小时内未处理将自动升级教务主任",
    }


def list_my_reports(user_id: int) -> list:
    """我的报备单列表（creator_id=我），联接手人姓名与最新验收标记。"""
    rows = db.query_all(
        """SELECT h.id, h.business_no, h.status, h.title, h.unresolved_items,
                  h.resolution, h.created_at, h.accepted_at, h.escalated,
                  u.username AS assignee_name,
                  (SELECT v.verdict FROM co_verifications v
                    WHERE v.target_type = 'handover' AND v.target_ref = CAST(h.id AS TEXT)
                    ORDER BY v.id DESC LIMIT 1) AS verify_verdict
            FROM handover_tasks h
            LEFT JOIN users u ON h.assignee_id = u.id
            WHERE h.creator_id = ?
            ORDER BY h.id DESC LIMIT 50""",
        (user_id,),
    )
    return [dict(r) for r in rows]


# ==================== 专业负责人：培养方案核校 ====================

def get_program() -> dict:
    """培养方案只读聚合：课程清单 + 先修映射 + 新旧代码映射（带数据源/快照时间）。"""
    data, data_source, snapshot_at = academic_api.get_academic_data()
    courses = []
    for code, c in data["courses_index"].items():
        pres = []
        for pre in c.get("prerequisites", []):
            pres.append({"code": pre,
                         "name": data["courses_index"].get(pre, {}).get("name", "")})
        courses.append({
            "code": code, "name": c.get("name", ""),
            "room": c.get("room", ""), "prerequisites": pres,
        })
    courses.sort(key=lambda x: x["code"])
    return {
        "semester": data.get("semester", ""),
        "courses": courses,
        "mapping": data.get("mapping", {}),
        "data_source": data_source,
        "snapshot_at": snapshot_at,
    }


def save_verify(user_id: int, target_type: str, target_ref: str,
                verdict: str, issues: str = "") -> tuple:
    """
    提交核校/验收意见，落 co_verifications 留痕（只留痕不判定，可多次追加）。
    target_type: program(核校) / handover(验收)；verdict: pass / issue。
    返回 (ok, err_code_or_None)。
    """
    if target_type not in ("program", "handover") or verdict not in ("pass", "issue"):
        return False, "1001"
    issues = (issues or "").strip()
    if verdict == "issue" and not issues:
        return False, "1001"                        # 提出问题必须写明问题内容
    if target_type == "handover":
        if not (str(target_ref) or "").isdigit():
            return False, "1001"
        if not db.query_all("SELECT id FROM handover_tasks WHERE id = ?", (int(target_ref),)):
            return False, "1001"                    # 交接单不存在
    db.execute(
        "INSERT INTO co_verifications (target_type, target_ref, verdict, issues, "
        "verifier_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (target_type, str(target_ref or ""), verdict, issues, user_id, db.now()),
    )
    return True, None


def list_verify_history(user_id: int) -> list:
    """我的核校/验收历史（最新在前），联对象标题便于阅读。"""
    rows = db.query_all(
        """SELECT v.id, v.target_type, v.target_ref, v.verdict, v.issues, v.created_at,
                  h.title AS handover_title, h.business_no AS handover_biz_no
            FROM co_verifications v
            LEFT JOIN handover_tasks h
                   ON v.target_type = 'handover' AND v.target_ref = CAST(h.id AS TEXT)
            WHERE v.verifier_id = ?
            ORDER BY v.id DESC LIMIT 100""",
        (user_id,),
    )
    return [dict(r) for r in rows]


# ==================== 专业负责人：验收列表 ====================

def list_acceptance() -> list:
    """已完成交接单列表 + 最新验收状态（供专业负责人逐单验收）。"""
    rows = db.query_all(
        """SELECT h.id, h.business_no, h.title, h.unresolved_items, h.resolution,
                  h.created_at, h.accepted_at, u.username AS assignee_name,
                  (SELECT v.verdict FROM co_verifications v
                    WHERE v.target_type = 'handover' AND v.target_ref = CAST(h.id AS TEXT)
                    ORDER BY v.id DESC LIMIT 1) AS verify_verdict,
                  (SELECT v.issues FROM co_verifications v
                    WHERE v.target_type = 'handover' AND v.target_ref = CAST(h.id AS TEXT)
                    ORDER BY v.id DESC LIMIT 1) AS verify_issues
            FROM handover_tasks h
            LEFT JOIN users u ON h.assignee_id = u.id
            WHERE h.status = 'completed'
            ORDER BY h.id DESC LIMIT 50""",
    )
    return [dict(r) for r in rows]
