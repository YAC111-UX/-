# -*- coding: utf-8 -*-
"""
首启初始化种子数据：
  1. 创建 5 个默认管理账号（首次启动才写入，上线检查清单要求强制改密）
  2. 写入默认系统提示词模板到 app_settings（管理后台可在线编辑）

角色说明（见 docs/01-需求清单.md 角色表）：
  scheduler       排课专员      → 知识库管理 + 提示词只读
  quality         教学质量专员  → 对话审计 + 提示词只读（试点上线否决权在阶段 9）
  director        教务主任      → 全部后台功能（提示词唯一编辑者）
  counselor       辅导员        → 学生转报备/答疑转办（阶段 8 启用，本阶段先建号）
  program_leader  专业负责人    → 培养方案核校（阶段 8 启用，本阶段先建号）
"""

from app.core.security import hash_password
from app.db import database as db

# 默认提示词模板：{context} 为检索条文注入位，管理后台编辑时必须保留
DEFAULT_PROMPT = (
    "你是知行职业学院排课选课智能助手，只能依据下面提供的教务制度、培养方案、"
    "场地规则条文回答用户。\n"
    "如果知识库没有对应内容，明确回复："
    "【制度中未找到对应说明，请联系排课专员人工核实】\n"
    "回答末尾必须附上引用文档来源；严禁编造先修条件、教室容量、实训室占用规则；"
    "不可以自动选课、修改排课计划。\n\n"
    "===== 检索到的制度条文 =====\n"
    "{context}\n"
    "===== 制度条文结束 ====="
)

# 5 个默认管理账号：用户名 = 角色名，初始密码统一（首次启动打印到控制台）
INITIAL_PASSWORD = "Zx@123456"
SEED_USERS = [
    ("scheduler", "scheduler"),        # (用户名, 角色)
    ("quality", "quality"),
    ("director", "director"),
    ("counselor", "counselor"),
    ("program_leader", "program_leader"),
]


def seed() -> None:
    """幂等执行：已存在的账号 / 模板不覆盖，只在缺失时补建。"""
    # ---- 1. 默认管理账号 ----
    created = []
    for username, role in SEED_USERS:
        exists = db.query_all("SELECT id FROM users WHERE username = ?", (username,))
        if not exists:
            db.execute(
                "INSERT INTO users (username, password_hash, role, is_active, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (username, hash_password(INITIAL_PASSWORD), role, db.now()),
            )
            created.append(username)
    if created:
        print(f"[Seed] 已创建默认管理账号：{', '.join(created)}"
              f"（初始密码：{INITIAL_PASSWORD}，上线前必须修改）")

    # ---- 2. 默认提示词模板 ----
    exists = db.query_all("SELECT key FROM app_settings WHERE key = 'default_prompt'")
    if not exists:
        db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("default_prompt", DEFAULT_PROMPT, db.now()),
        )
        print("[Seed] 已写入默认提示词模板（app_settings.default_prompt）")
