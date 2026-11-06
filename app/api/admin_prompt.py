# -*- coding: utf-8 -*-
"""
提示词模板管理路由（/api/admin/prompt）。
GET：排课专员 / 教学质量专员 / 教务主任 可读（前三者只读，主任可写）。
PUT：仅教务主任(director) 可编辑；模板必须保留 {context} 占位符，否则报 1009。
模板存 app_settings 表 key=default_prompt，聊天服务每次请求实时读库 → 保存即生效。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.errors import biz_error
from app.core.security import require_roles
from app.db import database as db

router = APIRouter(prefix="/api/admin/prompt")


class PromptBody(BaseModel):
    template: str


@router.get("")
def get_prompt(user=Depends(require_roles("scheduler", "quality", "director"))):
    """返回当前提示词模板正文。"""
    rows = db.query_all("SELECT value, updated_at FROM app_settings WHERE key = 'default_prompt'")
    if not rows:
        raise biz_error("5000")          # 模板缺失属系统异常（seed 应已写入）
    return {
        "code": 0,
        "message": "ok",
        "data": {"template": rows[0]["value"], "updated_at": rows[0]["updated_at"]},
    }


@router.put("")
def update_prompt(body: PromptBody,
                  user=Depends(require_roles("director"))):
    """
    保存新模板（仅教务主任）：
      1. 校验 {context} 占位符必须保留（1009）
      2. UPSERT 到 app_settings → 下一次提问立即生效（无缓存、无需重启）
    """
    template = body.template.strip()
    if not template:
        raise biz_error("1001")
    if "{context}" not in template:
        raise biz_error("1009")
    db.execute(
        "INSERT INTO app_settings (key, value, updated_by, updated_at) "
        "VALUES ('default_prompt', ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (template, user["id"], db.now()),
    )
    return {"code": 0, "message": "模板已保存，立即生效"}
