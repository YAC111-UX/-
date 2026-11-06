# -*- coding: utf-8 -*-
"""
LLM 调用封装（OpenAI 兼容接口）。【★核心模块·可复用】
换模型供应商只需改 .env 的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL。
阶段 6 新增 Function Calling 支持：
  chat_with_tools      非流式决策轮（带 tools，解析 tool_calls）
  chat_stream_messages 流式输出（最终回答打字机用）
  chat_stream_llm      阶段 4 老接口（组装 messages 后走 chat_stream_messages，保持兼容）
"""

import json


def _build_headers_and_url():
    """从配置组装请求头与接口地址；未配 Key 视为服务不可用。"""
    import requests

    from app.config import Config
    from app.core.errors import biz_error

    if not Config.LLM_API_KEY:
        raise biz_error("2002")  # 未配置 Key 视为服务不可用
    url = f"{Config.LLM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {Config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    return requests, Config, biz_error, url, headers


def chat_with_tools(system_prompt: str, messages: list, tools: list) -> dict:
    """
    非流式决策轮：带工具定义调 LLM，返回结构化决策。
    参数:
        system_prompt: 系统提示词
        messages:      完整对话消息（含历史 / assistant tool_calls / tool 结果）
        tools:         OpenAI Function Calling 工具 Schema 列表
    返回:
        {"content": 文本或 None,
         "tool_calls": [{"id", "name", "arguments"(dict)} 或空列表]}
    异常: BizError(2001) 超时 / BizError(2002) 接口错误
    """
    requests, Config, biz_error, url, headers = _build_headers_and_url()
    payload = {
        "model": Config.LLM_MODEL,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "temperature": 0.1,
        "tools": tools,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=Config.LLM_TIMEOUT)
    except requests.RequestException as e:
        if isinstance(e, requests.Timeout):
            raise biz_error("2001") from e
        raise biz_error("2002") from e
    if resp.status_code != 200:
        raise biz_error("2002")

    try:
        message = resp.json()["choices"][0]["message"]
    except (json.JSONDecodeError, KeyError, IndexError):
        raise biz_error("2002")

    tool_calls = []
    for tc in message.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}                     # 参数 JSON 坏 → 空参，由工具层报 1010
        tool_calls.append({
            "id": tc.get("id", ""),
            "name": tc["function"]["name"],
            "arguments": args,
            "_raw": tc,                   # 原始 tool_calls 结构（回填 assistant 消息用）
        })
    return {"content": message.get("content"), "tool_calls": tool_calls}


def chat_stream_messages(messages: list):
    """
    流式调用（无工具参数）：逐段 yield 增量文本，用于最终回答的打字机效果。
    messages 为完整对话（含 system / tool 结果回填）。
    """
    requests, Config, biz_error, url, headers = _build_headers_and_url()
    payload = {
        "model": Config.LLM_MODEL,
        "messages": messages,
        "temperature": 0.1,   # 教务问答要求严谨，低温减少自由发挥
        "stream": True,       # 流式输出，前端打字机效果
    }
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=Config.LLM_TIMEOUT, stream=True)
    except requests.RequestException as e:
        if isinstance(e, requests.Timeout):
            raise biz_error("2001") from e
        raise biz_error("2002") from e
    if resp.status_code != 200:
        raise biz_error("2002")

    # 解析 SSE 数据流：每行形如 data: {...}，结束标志为 data: [DONE]
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            piece = json.loads(data)["choices"][0]["delta"].get("content")
        except (json.JSONDecodeError, KeyError, IndexError):
            continue  # 跳过无法解析的残缺报文，不让单行错误中断整流
        if piece:
            yield piece


def chat_stream_llm(system_prompt: str, history: list, question: str):
    """
    流式调用大模型（阶段 4 老接口，保持兼容）：组装 messages 后走 chat_stream_messages。
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})
    yield from chat_stream_messages(messages)
