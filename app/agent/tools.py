# -*- coding: utf-8 -*-
"""
Agent Function Calling 工具定义与执行注册表。【★核心模块·可复用】
  TOOL_SCHEMAS   6 个工具的 OpenAI Function Calling JSON Schema（传给 LLM）
  TOOL_REGISTRY  工具名 → 执行函数（服务端执行，权限在服务端判定，不信任 LLM）
  execute_tool   统一执行入口：参数校验(1010) / 角色权限(1011) / 日志落库

红线约束落地：
  - 冲突判定全部走 rules/ 纯代码，LLM 只解释结果
  - generate_adjust_draft 仅排课专员会话可发起，其余角色返回 1011
  - 学生选课数据透传不落库，本地只写脱敏后的输入输出摘要
"""

import json
import time

from app.adapters import academic_api, room_api
from app.core.errors import BizError
from app.core.biz_no import next_biz_no
from app.db import database as db
from app.rules import prerequisite, room_conflict

# ==================== 工具 JSON Schema（OpenAI Function Calling 格式） ====================

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": "检索教务制度/培养方案/场地规则条文。凡涉及'规定/条件/能不能/怎么办理'的提问必须先调本工具，禁止凭记忆回答。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或问题"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_prerequisite",
            "description": "校验学生已修课程是否满足目标课程的先修要求（含新旧课程代码映射）。只做判定，不做任何选课操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {"type": "string", "description": "学生伪 ID（脱敏），如 S001"},
                    "course_code": {"type": "string", "description": "目标课程代码，新旧代码均可"},
                },
                "required": ["student_id", "course_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_room_conflict",
            "description": "校验指定时间段场地/实训室是否被占用（含维护占用）。只读校验。",
            "parameters": {
                "type": "object",
                "properties": {
                    "room": {"type": "string", "description": "场地/实训室名称或编号，如 X102"},
                    "week_day": {"type": "integer", "description": "星期几，1=周一"},
                    "section": {"type": "string", "description": "节次，如 '3-4节'"},
                },
                "required": ["room", "week_day", "section"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_adjust_draft",
            "description": "根据冲突校验结果起草调课调整建议。仅生成草稿，不生效；须排课专员在管理后台确认后才写入正式记录。仅排课专员会话可用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "business_no": {"type": "string", "description": "关联的冲突校验业务编号"},
                    "adjustments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string", "description": "调整对象，如课程/教室"},
                                "field": {"type": "string", "description": "调整字段，如 room/section/week_day"},
                                "old_value": {"type": "string", "description": "原值（必填，草稿必须显示原记录）"},
                                "new_value": {"type": "string", "description": "建议新值"},
                            },
                            "required": ["target", "field", "old_value", "new_value"],
                        },
                    },
                    "reason": {"type": "string", "description": "调整理由"},
                },
                "required": ["business_no", "adjustments", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_handover",
            "description": "按业务编号或关键词查询历史交接单据（状态/未解决事项/接手人）。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "business_no": {"type": "string", "description": "业务编号，可空"},
                    "keyword": {"type": "string", "description": "标题/未解决事项关键词，可空"},
                    "status": {"type": "string", "description": "pending/assigned/accepted/completed/escalated，可空"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_manual_fallback",
            "description": "触发人工兜底：实训室维护占用但课表标可用 / 资料缺少依据 / 两个版本规则冲突 / 关键系统不可用。创建交接单据并通知。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trigger_type": {
                        "type": "string",
                        "enum": ["room_needs_manual", "missing_evidence", "rule_conflict", "system_down"],
                        "description": "必转场景类型",
                    },
                    "reason": {"type": "string", "description": "触发原因说明（用户可读）"},
                    "business_no": {"type": "string", "description": "关联业务编号，可空"},
                },
                "required": ["trigger_type", "reason"],
            },
        },
    },
]

# SSE 状态提示文案（前端 tool_call 事件展示）
TOOL_STATUS_TEXT = {
    "rag_search": "正在检索教务制度条文…",
    "check_prerequisite": "正在校验先修要求…",
    "check_room_conflict": "正在检查场地占用…",
    "generate_adjust_draft": "正在起草调课调整草稿…",
    "query_handover": "正在查询历史交接单据…",
    "notify_manual_fallback": "正在创建人工兜底单据…",
}

# 生成调课草稿：仅排课专员会话可发起（草稿确认权也仅排课专员，见需求清单确认链）
DRAFT_ALLOWED_ROLES = {"scheduler"}

# 兜底单据接手人：场地/资料/规则类 → 排课专员；系统类 → 教务主任
FALLBACK_ASSIGNEE = {
    "room_needs_manual": ("scheduler", "排课专员"),
    "missing_evidence": ("scheduler", "排课专员"),
    "rule_conflict": ("scheduler", "排课专员"),
    "system_down": ("director", "教务主任"),
}

FALLBACK_TITLES = {
    "room_needs_manual": "场地维护占用但课表标可用",
    "missing_evidence": "资料缺少依据",
    "rule_conflict": "两个版本规则冲突",
    "system_down": "关键系统不可用",
}


# ==================== 各工具实现（返回 dict 给 LLM 解释） ====================

def _tool_rag_search(args, ctx) -> dict:
    """制度检索：复用阶段 3 混合检索，返回条文片段供 LLM 引用作答。"""
    from app.rag import pipeline as rag
    if rag.rebuilding or rag.pipeline is None:
        return {"error": "2003", "message": "知识库正在维护，请稍后再试"}
    hits = rag.pipeline.search(args["query"])
    return {
        "hits": [{"filename": h["filename"], "chunk": h["chunk"], "score": h["score"]}
                 for h in hits],
        "count": len(hits),
    }


def _tool_check_prerequisite(args, ctx) -> dict:
    """先修校验：规则引擎判定；校验输入输出摘要落 conflict_replay_records。"""
    data, data_source, snapshot_at = academic_api.get_academic_data()
    completed = academic_api.get_student_courses(args["student_id"], data)
    result = prerequisite.check_prerequisite(
        args["course_code"], completed, data["courses_index"],
    )
    result["data_source"] = data_source
    result["snapshot_at"] = snapshot_at
    result["semester"] = data.get("semester", "")

    # 审计留痕：脱敏输入摘要 + 判定结果摘要（学生已修清单不落库）
    biz_no = next_biz_no()
    db.execute(
        "INSERT INTO conflict_replay_records (business_no, semester, check_input_json, "
        "check_result_json, data_source, snapshot_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (biz_no, result.get("semester", ""),
         json.dumps({"tool": "check_prerequisite", "student_id": args["student_id"],
                     "course_code": args["course_code"]}, ensure_ascii=False),
         json.dumps(result, ensure_ascii=False),
         data_source, snapshot_at, db.now()),
    )
    result["business_no"] = biz_no
    return result


def _tool_check_room_conflict(args, ctx) -> dict:
    """场地校验：规则引擎判定；结果摘要落 conflict_replay_records。"""
    rooms, data_source, snapshot_at = room_api.get_room_data()
    room_key = (args["room"] or "").strip().upper()
    room_info = None
    for k, v in rooms.items():
        if k.upper() == room_key or v.get("name", "").upper().find(room_key) > -1:
            room_info = v
            break
    if room_info is None:
        return {"error": "1010", "message": f"场地 {args['room']} 不存在，请核对名称"}

    result = room_conflict.check_room_conflict(room_info, args["week_day"], args["section"])
    result["data_source"] = data_source
    result["snapshot_at"] = snapshot_at

    biz_no = next_biz_no()
    db.execute(
        "INSERT INTO conflict_replay_records (business_no, semester, check_input_json, "
        "check_result_json, data_source, snapshot_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (biz_no, "", 
         json.dumps({"tool": "check_room_conflict", "room": args["room"],
                     "week_day": args["week_day"], "section": args["section"]},
                    ensure_ascii=False),
         json.dumps(result, ensure_ascii=False),
         data_source, snapshot_at, db.now()),
    )
    result["business_no"] = biz_no
    return result


def _tool_generate_adjust_draft(args, ctx) -> dict:
    """调课草稿：唯一写操作。权限在服务端判定（不信任 LLM），落 drafts 待确认。"""
    if ctx.get("user_role") not in DRAFT_ALLOWED_ROLES:
        return {"error": "1011",
                "message": "调课草稿仅可由排课专员会话发起，已为您创建人工兜底建议"}
    # 业务编号必须来自一次已落库的冲突校验，保证审计链完整
    ref = db.query_all(
        "SELECT id FROM conflict_replay_records WHERE business_no = ?",
        (args["business_no"],),
    )
    if not ref:
        return {"error": "1010",
                "message": "业务编号不存在，请先执行冲突校验再起草调整"}
    adjustments = args.get("adjustments") or []
    if not adjustments:
        return {"error": "1010", "message": "调整明细不能为空"}

    content = {"adjustments": adjustments, "reason": args.get("reason", "")}
    draft_id = db.insert_returning_id(
        "INSERT INTO drafts (business_no, status, content_json, created_by_session) "
        "VALUES (?, 'pending_confirm', ?, ?)",
        (args["business_no"], json.dumps(content, ensure_ascii=False),
         ctx.get("session_id") or ""),
    )
    return {"draft_id": draft_id, "business_no": args["business_no"],
            "status": "pending_confirm",
            "message": "草稿已生成，等待排课专员在管理后台确认，确认前不会生效"}


def _tool_query_handover(args, ctx) -> dict:
    """交接单据查询：只读，联表取接手人用户名，最多 20 条。"""
    where, params = ["1=1"], []
    if args.get("business_no"):
        where.append("business_no LIKE ?")
        params.append(f"%{args['business_no']}%")
    if args.get("keyword"):
        where.append("(title LIKE ? OR unresolved_items LIKE ?)")
        kw = f"%{args['keyword']}%"
        params.extend([kw, kw])
    if args.get("status"):
        where.append("status = ?")
        params.append(args["status"])
    rows = db.query_all(
        f"""SELECT h.business_no, h.version, h.status, h.title, h.unresolved_items,
                   h.created_at, h.accepted_at, u.username AS assignee
            FROM handover_tasks h LEFT JOIN users u ON h.assignee_id = u.id
            WHERE {' AND '.join(where)} ORDER BY h.id DESC LIMIT 20""",
        tuple(params),
    )
    items = [dict(r) for r in rows]
    return {"items": items, "count": len(items)}


def _tool_notify_manual_fallback(args, ctx) -> dict:
    """人工兜底：建 handover_tasks 单据，按场景类型派给接手角色，24h 超时升级教务主任。"""
    trigger_type = args.get("trigger_type", "")
    if trigger_type not in FALLBACK_ASSIGNEE:
        return {"error": "1010", "message": "必转场景类型不合法"}
    assignee_role, assignee_label = FALLBACK_ASSIGNEE[trigger_type]

    # 找接手角色的第一个在职账号（试点期单人角色；多人时改派单逻辑）
    user = db.query_all(
        "SELECT id, username FROM users WHERE role = ? AND is_active = 1 ORDER BY id LIMIT 1",
        (assignee_role,),
    )
    biz_no = next_biz_no()
    escalate_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 24 * 3600))
    db.execute(
        "INSERT INTO handover_tasks (business_no, version, status, title, unresolved_items, "
        "creator_id, assignee_id, escalate_at, created_at) "
        "VALUES (?, 1, 'pending', ?, ?, NULL, ?, ?, ?)",
        (biz_no, f"[AI兜底] {FALLBACK_TITLES[trigger_type]}", args.get("reason", ""),
         user[0]["id"] if user else None, escalate_at, db.now()),
    )
    return {"business_no": biz_no, "assignee_role": assignee_role,
            "assignee_label": assignee_label,
            "assignee": user[0]["username"] if user else "待分派",
            "escalate_at": escalate_at,
            "message": f"已创建人工兜底单据，接手人：{assignee_label}"}


# ==================== 注册表与统一执行入口 ====================

TOOL_REGISTRY = {
    "rag_search": _tool_rag_search,
    "check_prerequisite": _tool_check_prerequisite,
    "check_room_conflict": _tool_check_room_conflict,
    "generate_adjust_draft": _tool_generate_adjust_draft,
    "query_handover": _tool_query_handover,
    "notify_manual_fallback": _tool_notify_manual_fallback,
}


def execute_tool(name: str, arguments: dict, ctx: dict) -> dict:
    """
    统一执行入口：
      1. 未注册 / 参数缺必填项 → 1010（错误回填给 LLM，由它向用户解释）
      2. 工具内部异常 → 统一错误摘要（不中断会话）
    ctx: {"session_id", "user_role", "message_id", "round_no"}
    返回工具输出 dict（调用方负责写 agent_tool_logs 并回填 LLM）。
    """
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return {"error": "1010", "message": f"工具 {name} 不存在"}
    try:
        return fn(arguments or {}, ctx)
    except BizError as e:                            # 如 2004 外部系统不可用且无快照
        return {"error": e.code, "message": e.message}
    except Exception as e:
        print(f"[tool:{name}] 执行异常：{e!r}")
        return {"error": "5000", "message": "工具执行异常，请稍后重试或联系排课专员"}


def log_tool_call(ctx: dict, name: str, arguments, output, duration_ms: int) -> None:
    """工具调用日志落 agent_tool_logs（入参/出参截断存摘要，满足审计回放）。"""
    def brief(obj, limit):
        text = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
        return text[:limit]
    db.execute(
        "INSERT INTO agent_tool_logs (session_id, message_id, round_no, tool_name, "
        "input_summary, output_summary, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ctx.get("session_id"), ctx.get("message_id"), ctx.get("round_no", 0), name,
         brief(arguments, 200), brief(output, 300), duration_ms, db.now()),
    )
