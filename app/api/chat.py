# -*- coding: utf-8 -*-
"""聊天相关路由：POST /api/chat（SSE）、GET /api/session/new。
阶段 5：两个接口都带"可选登录"——带头且令牌有效则记录 user_id，
匿名学生（不带令牌 / 令牌无效）照常使用，不影响学生问答入口。"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.errors import BizError, ERROR_CODES
from app.db import database as db
from app.services import chat as chat_service

router = APIRouter(prefix="/api")


def optional_user_id(request: Request):
    """
    可选鉴权：请求头带合法 Bearer 令牌则返回 users.id，否则返回 None。
    与管理后台的 get_current_user 不同：这里不做强制校验、不抛错。
    """
    from app.core.security import decode_token
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        payload = decode_token(auth[len("Bearer "):].strip())
    except BizError:
        return None                       # 令牌过期/无效 → 按匿名处理
    try:
        return int(payload.get("sub"))    # JWT sub 为字符串，转回整数用户 id
    except (TypeError, ValueError):
        return None


@router.get("/session/new")
def new_session(request: Request):
    """创建新会话。统一返回 {code, message, data} 结构。"""
    session_id = chat_service.create_session(optional_user_id(request))
    return {"code": 0, "message": "ok", "data": {"session_id": session_id}}


@router.post("/chat")
def chat(body: dict, request: Request):
    """
    提问接口，返回 SSE 事件流（text/event-stream）。
    入参: {"session_id": "可空", "question": "提问"}
    参数/会话校验在开流前完成（返回 JSON 业务错误）；
    开流后的异常通过 SSE error 事件推送（见 chat_stream_safe）。
    """
    session_id = (body.get("session_id") or "").strip()
    question = (body.get("question") or "").strip()

    # ---- 参数校验（1001/1003） ----
    if not question:
        raise HTTPException(status_code=200, detail={
            "code": "1001", "message": ERROR_CODES["1001"]})
    if len(question) > 500:
        raise HTTPException(status_code=200, detail={
            "code": "1003", "message": ERROR_CODES["1003"]})
    # 会话存在性校验（1002）：空 session_id 合法（服务端自动创建）
    if session_id:
        from app.db import database as db
        if not db.query_all("SELECT id FROM sessions WHERE id = ?", (session_id,)):
            raise HTTPException(status_code=200, detail={
                "code": "1002", "message": ERROR_CODES["1002"]})

    # ---- 返回 SSE 流（异常在流内兜底，前端必有收尾） ----
    return StreamingResponse(
        chat_service.chat_stream_safe(session_id, question, optional_user_id(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用代理缓冲，保证打字机流畅
        },
    )
