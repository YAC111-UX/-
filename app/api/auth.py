# -*- coding: utf-8 -*-
"""登录鉴权路由：POST /api/auth/login → 校验账密，签发 JWT。"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.errors import biz_error
from app.core.security import create_token, verify_password
from app.db import database as db

router = APIRouter(prefix="/api/auth")


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginBody):
    """
    登录：校验用户名密码 → 签发 JWT（12 小时）。
    统一返回 {code:0, data:{token, username, role}}；
    用户不存在 / 密码错 / 账号停用，统一报 1006（不区分具体原因，防账号枚举）。
    """
    rows = db.query_all("SELECT * FROM users WHERE username = ?", (body.username,))
    if not rows:
        raise biz_error("1006")
    user = rows[0]
    if not user["is_active"] or not verify_password(body.password, user["password_hash"]):
        raise biz_error("1006")
    token = create_token(user["id"], user["username"], user["role"])
    return {
        "code": 0,
        "message": "ok",
        "data": {"token": token, "username": user["username"], "role": user["role"]},
    }
