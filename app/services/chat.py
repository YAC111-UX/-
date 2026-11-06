# -*- coding: utf-8 -*-
"""
聊天编排服务：把"会话管理 → 检索 → Agent 工具循环 → 流式回答 → 落库"串成 SSE 事件流。
阶段 6 改造：插入 Agent 执行循环（≤5 轮 Function Calling），
  工具调用经 SSE tool_call / tool_result 事件推送前端，全程写 agent_tool_logs 审计。
红线：冲突判定走 rules/ 纯代码；提示词实时读库；风险提问落库前打标；登录用户落 user_id。
"""

import json
import time
import uuid

from app.config import Config
from app.core.errors import biz_error, BizError
from app.core.llm import chat_with_tools, chat_stream_messages
from app.agent.tools import TOOL_SCHEMAS, TOOL_STATUS_TEXT, execute_tool, log_tool_call
from app.db import database as db
from app.db.seed import DEFAULT_PROMPT
from app.rag import pipeline as rag

# Agent 循环最大轮数（架构设计约定：最多 5 轮，超过强制转人工收尾）
MAX_TOOL_ROUNDS = 5

# 高风险关键词表（docs/04-管理后台设计.md 第五节）
RISK_KEYWORDS = {
    "绕过先修":     ["绕过", "跳过", "免修", "不用修先修", "直接选"],
    "规避冲突/场地": ["强行排", "硬排", "规避冲突", "加座", "超容量", "塞进去"],
    "突破权限":     ["自动选课", "帮我选课", "改课表", "替我改排课"],
}


def sse_event(event: str, data: dict) -> str:
    """格式化一条 SSE 报文（前端按 event 字段分发处理）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_session(user_id: int = None) -> str:
    """创建新会话并落库（登录用户记录 user_id，匿名传 None）。"""
    session_id = str(uuid.uuid4())
    ts = db.now()
    db.execute(
        "INSERT INTO sessions (id, user_id, created_at, last_active_at) VALUES (?, ?, ?, ?)",
        (session_id, user_id, ts, ts),
    )
    return session_id


def detect_risk(question: str):
    """
    风险关键词检测：遍历三类命中词，返回命中描述字符串；未命中返回 None。
    示例返回："绕过先修（绕过）；突破权限（帮我选课）"
    """
    hits = []
    for category, words in RISK_KEYWORDS.items():
        matched = [w for w in words if w in question]
        if matched:
            hits.append(f"{category}（{'、'.join(matched)}）")
    return "；".join(hits) if hits else None


def get_recent_history(session_id: str, rounds: int = Config.HISTORY_ROUNDS) -> list:
    """
    读取该会话最近 rounds 轮（每轮 user+assistant 共 2 条）消息，
    转成 LLM 的 messages 格式。上下文窗口按业务约定固定 6 轮。
    """
    rows = db.query_all(
        "SELECT role, content FROM messages WHERE session_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (session_id, rounds * 2),
    )
    # 倒序取出后反转为时间正序，供 LLM 按对话顺序理解
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def get_prompt_template() -> str:
    """
    实时读取系统提示词模板（app_settings key=default_prompt，无缓存）。
    管理后台保存新模板后，下一次提问立即生效；库中无记录时兜底用代码默认值。
    """
    rows = db.query_all("SELECT value FROM app_settings WHERE key = 'default_prompt'")
    return rows[0]["value"] if rows else DEFAULT_PROMPT


def build_system_prompt(hits: list) -> str:
    """
    拼装系统提示词：从数据库取模板，把检索到的制度条文替换进 {context} 占位符。
    追加 Agent 工具使用约定（检索已预置在上下文，工具用于补充校验类问题）。
    """
    context = "\n\n".join(
        f"【来源 {n}】《{h['filename']}》节选：\n{h['chunk']}"
        for n, h in enumerate(hits, start=1)
    )
    if not context:
        context = "（本次检索未命中任何制度条文，可调用 rag_search 换关键词再查）"
    return get_prompt_template().replace("{context}", context)


def get_user_role(user_id) -> str:
    """按 user_id 查角色；匿名返回 guest（工具权限按此判定）。"""
    if not user_id:
        return "guest"
    rows = db.query_all("SELECT role FROM users WHERE id = ?", (user_id,))
    return rows[0]["role"] if rows else "guest"


def run_tool_rounds(system_prompt, messages, ctx):
    """
    Agent 执行循环生成器（yield SSE 事件）：
      每轮非流式调用 LLM 决策 → 有 tool_calls 则执行工具并回填 → 继续下一轮；
      无 tool_calls 或达 MAX_TOOL_ROUNDS 时结束。
    返回（通过生成器最后的 StopIteration.value 传出）:
        (rounds_used, pending_tool_calls)  pending 非空表示达到轮数上限仍要调工具
    """
    rounds_used = 0
    while rounds_used < MAX_TOOL_ROUNDS:
        decision = chat_with_tools(system_prompt, messages, TOOL_SCHEMAS)
        if not decision["tool_calls"]:
            return rounds_used, []                    # LLM 已决定直接回答

        rounds_used += 1
        # assistant 消息带原始 tool_calls 结构回填（OpenAI 协议要求）
        messages.append({
            "role": "assistant",
            "content": decision["content"] or "",
            "tool_calls": [tc["_raw"] for tc in decision["tool_calls"]],
        })
        for tc in decision["tool_calls"]:
            yield sse_event("tool_call", {
                "tool": tc["name"], "round": rounds_used,
                "text": TOOL_STATUS_TEXT.get(tc["name"], "正在调用工具…"),
            })
            t0 = time.time()
            output = execute_tool(tc["name"], tc["arguments"], {**ctx, "round_no": rounds_used})
            log_tool_call({**ctx, "round_no": rounds_used}, tc["name"], tc["arguments"], output,
                          int((time.time() - t0) * 1000))
            # 前端展示的简短结果摘要
            if output.get("error"):
                summary = f"[{output['error']}] {output.get('message', '')}"
            elif output.get("passed") is not None:
                summary = "先修校验通过" if output["passed"] else \
                    f"缺少先修：{'、'.join(m['required_course'] for m in output.get('missing', []))}"
            elif "conflicts" in output:
                summary = "场地可用" if output.get("available") else \
                    "；".join(c["detail"] for c in output["conflicts"])
            elif output.get("draft_id"):
                summary = f"草稿 {output['draft_id']} 已生成待确认"
            elif output.get("hits") is not None:
                summary = f"命中 {output['count']} 条制度条文"
            elif output.get("items") is not None:
                summary = f"查到 {output['count']} 张交接单据"
            elif output.get("business_no"):
                summary = f"兜底单据 {output['business_no']} 已创建"
            else:
                summary = "完成"
            yield sse_event("tool_result", {"tool": tc["name"], "summary": summary[:80]})
            # 工具结果以 tool 消息回填（OpenAI 协议）
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(output, ensure_ascii=False),
            })
    return rounds_used, [{"name": "…"}]               # 达上限仍要调工具 → 强制收尾标记


def chat_stream(session_id: str, question: str, user_id: int = None):
    """
    聊天主流程生成器：每一步 yield 一条 SSE 事件。
    事件顺序：session → status → (tool_call → tool_result)* → delta* → citations → done。
    """
    # ---- 1. 会话校验 / 创建 ----
    if session_id:
        exists = db.query_all("SELECT id FROM sessions WHERE id = ?", (session_id,))
        if not exists:
            raise biz_error("1002")
        if user_id:                                   # 已有会话补记登录用户
            db.execute("UPDATE sessions SET user_id = ? WHERE id = ?",
                       (user_id, session_id))
    else:
        session_id = create_session(user_id)
    yield sse_event("session", {"session_id": session_id})

    # ---- 2. 预检索知识库（制度问答主路径，结果注入系统提示词并作引用来源） ----
    yield sse_event("status", {"stage": "retrieving", "text": "正在检索教务制度与排课规则…"})
    if rag.rebuilding or rag.pipeline is None:
        raise biz_error("2003")
    hits = rag.pipeline.search(question)
    citations = [
        {"filename": h["filename"],
         "excerpt": h["chunk"][:Config.CITATION_EXCERPT_LEN]}
        for h in hits
    ]

    # ---- 3. 用户消息落库（落库前风险关键词检测，命中写标记） ----
    risk_reason = detect_risk(question)
    message_id = db.insert_returning_id(
        "INSERT INTO messages (session_id, role, content, risk_flag, risk_reason, created_at) "
        "VALUES (?, 'user', ?, ?, ?, ?)",
        (session_id, question, 1 if risk_reason else 0, risk_reason, db.now()),
    )

    # ---- 4. Agent 工具循环（≤5 轮，全程 SSE 推送 + 日志落库） ----
    user_role = get_user_role(user_id)
    ctx = {"session_id": session_id, "user_role": user_role, "message_id": message_id}
    messages = get_recent_history(session_id) + [{"role": "user", "content": question}]
    system_prompt = build_system_prompt(hits)

    rounds_used, pending = yield from run_tool_rounds(system_prompt, messages, ctx)

    # ---- 5. 最终回答（流式打字机；达轮数上限则强制转人工收尾） ----
    full_answer = []
    if pending:                                       # 5 轮仍要调工具 → 不再调 LLM
        fallback_text = ("这个问题涉及多个系统环节，暂时无法自动完成，"
                         "请联系排课专员人工核实。")
        full_answer.append(fallback_text)
        yield sse_event("delta", {"text": fallback_text})
    else:
        final_messages = [{"role": "system", "content": system_prompt}] + messages
        for piece in chat_stream_messages(final_messages):
            full_answer.append(piece)
            yield sse_event("delta", {"text": piece})

    # ---- 6. 助手消息落库（含引用来源 JSON，供审计与回放） ----
    answer_text = "".join(full_answer)
    if not answer_text:
        raise biz_error("2002")                       # LLM 返回空内容按服务异常处理
    db.execute(
        "INSERT INTO messages (session_id, role, content, citations_json, created_at) "
        "VALUES (?, 'assistant', ?, ?, ?)",
        (session_id, answer_text, json.dumps(citations, ensure_ascii=False), db.now()),
    )
    db.execute("UPDATE sessions SET last_active_at = ? WHERE id = ?",
               (db.now(), session_id))

    # ---- 7. 推送引用来源与结束信号 ----
    yield sse_event("citations", {"items": citations})
    yield sse_event("done", {"session_id": session_id, "tool_rounds": rounds_used})


def chat_stream_safe(session_id: str, question: str, user_id: int = None):
    """
    带异常兜底的版本：流程中任何 BizError / 未知异常都转成 SSE error 事件，
    保证前端总能收到明确收尾、不会白屏挂起。
    """
    try:
        yield from chat_stream(session_id, question, user_id)
    except BizError as e:
        yield sse_event("error", {"code": e.code, "message": e.message})
    except Exception as e:  # 未知异常统一 5000，日志打印便于排查
        print(f"[chat] 未知异常：{e!r}")
        yield sse_event("error", {"code": "5000", "message": "系统开小差了，请稍后重试"})
