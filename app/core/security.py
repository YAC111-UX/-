# -*- coding: utf-8 -*-
"""
安全模块：密码哈希 + JWT 签发校验 + RBAC 权限依赖。【★核心模块·可复用】
  密码：PBKDF2-HMAC-SHA256 加盐哈希（标准库实现，数据库不存明文口令）
  JWT：HS256 签名，payload 含用户 id / 用户名 / 角色，有效期 12 小时
  RBAC：FastAPI 依赖注入 require_roles("scheduler", ...) 装饰在路由上，
        逐接口校验角色，越权统一抛业务码 1005
"""

import os
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

import jwt                      # PyJWT
from fastapi import Request

from app.core.errors import biz_error
from app.db import database as db

# JWT 签名密钥：生产环境必须通过 .env 配置 JWT_SECRET，代码里只留开发默认值（≥32 字节）
JWT_SECRET = os.getenv("JWT_SECRET", "zhixing-course-agent-dev-secret-2026")
JWT_ALGORITHM = "HS256"         # 对称签名算法
JWT_EXPIRE_HOURS = 12           # 令牌有效期：12 小时（设计文档约定）

# PBKDF2 迭代次数：防暴力破解，值越大越慢越安全（原型取够用的量级）
PBKDF2_ITERATIONS = 120_000


# ==================== 密码哈希 ====================

def hash_password(password: str) -> str:
    """
    生成加盐 PBKDF2 哈希，存储格式：salt_hex$hash_hex
    每次调用生成新的随机盐，即使两人密码相同，库里存的串也不同。
    """
    salt = secrets.token_bytes(16)                       # 16 字节随机盐
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """
    校验密码：用存储串里的盐重新计算哈希，与存储值做恒定时间比较。
    hmac.compare_digest 防止"逐字节短路比较"造成的时序侧信道。
    """
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), PBKDF2_ITERATIONS,
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False   # 存储串格式损坏一律按校验失败处理


# ==================== JWT 签发与校验 ====================

def create_token(user_id: int, username: str, role: str) -> str:
    """登录成功后签发 JWT，有效期 12 小时。"""
    payload = {
        "sub": str(user_id),                             # 用户 id（JWT 标准规定 sub 为字符串）
        "username": username,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """
    解析 JWT。过期 / 签名不对 / 格式坏，统一抛业务码 1005（无权限），
    提示前端跳转登录页，避免暴露"令牌过期"等细节给潜在攻击者。
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.PyJWTError:
        raise biz_error("1005")


# ==================== FastAPI 依赖注入 ====================

def get_current_user(request: Request):
    """
    从请求头 Authorization: Bearer <token> 解析出当前登录用户（sqlite3.Row）。
    缺令牌 / 令牌无效 / 账号停用，一律抛 1005。管理后台接口通用前置校验。
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise biz_error("1005")
    payload = decode_token(auth[len("Bearer "):].strip())
    try:
        user_id = int(payload.get("sub"))                # sub 在 JWT 里是字符串，转回整数主键
    except (TypeError, ValueError):
        raise biz_error("1005")
    rows = db.query_all(
        "SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)
    )
    if not rows:
        raise biz_error("1005")   # 用户已被删除或停用
    return rows[0]


def require_roles(*allowed_roles):
    """
    RBAC 依赖工厂：require_roles("scheduler", "director") 生成一个依赖函数，
    挂在路由上后，只有列表内角色能通过，其余角色抛 1005（无权限）。
    用法：
        @router.get("/documents", dependencies=[Depends(require_roles("scheduler"))])
        # 或需要在函数里拿用户信息时：
        def handler(user = Depends(require_roles("director"))): ...
    """
    def dependency(request: Request):
        user = get_current_user(request)
        if user["role"] not in allowed_roles:
            raise biz_error("1005")
        return user
    return dependency
