# -*- coding: utf-8 -*-
"""
SQLite 数据库连接与建表。【★核心模块·可复用】
试点期数据量小，用标准库 sqlite3 + 全局锁即可，无需引入额外依赖；
阶段 9 上线检查若发现并发瓶颈，再评估换 aiosqlite/MySQL。
"""

import sqlite3
import threading
from datetime import datetime

from app.config import Config

# SQLite 不支持多线程共享连接：全局锁串行化写入（原型规模足够）
_lock = threading.Lock()
_conn: sqlite3.Connection = None


def get_conn() -> sqlite3.Connection:
    """获取全局数据库连接（惰性初始化）。"""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(Config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row          # 查询结果可按列名取值
        _conn.execute("PRAGMA foreign_keys = ON")
    return _conn


def execute(sql: str, params: tuple = ()) -> None:
    """写操作（INSERT/UPDATE/DELETE），自动提交。"""
    with _lock:
        get_conn().execute(sql, params)
        get_conn().commit()


def insert_returning_id(sql: str, params: tuple = ()) -> int:
    """写操作并返回自增主键（消息/单据落库后需拿 id 关联日志时用）。"""
    with _lock:
        cur = get_conn().execute(sql, params)
        get_conn().commit()
        return cur.lastrowid


def query_all(sql: str, params: tuple = ()) -> list:
    """读操作，返回行列表（sqlite3.Row）。"""
    with _lock:
        return get_conn().execute(sql, params).fetchall()


def now() -> str:
    """当前时间字符串（本地时区），统一存库格式。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    """
    按 02-架构设计.md 建 10 张表。
    阶段 4 只用 sessions/messages，但一次性建全表，避免后续迁移。
    """
    Config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    ddl = [
        # 1. 用户账号（阶段 4 匿名会话暂不写入；阶段 5 JWT RBAC 启用）
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,                -- student/counselor/scheduler/quality/director/program_leader
            is_active INTEGER DEFAULT 1,
            created_at DATETIME
        )""",
        # 2. 会话
        """CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,               -- UUID
            user_id INTEGER,                   -- 阶段 5 关联 users
            created_at DATETIME,
            last_active_at DATETIME
        )""",
        # 3. 对话消息
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,                -- user / assistant
            content TEXT NOT NULL,
            citations_json TEXT,               -- 引用来源 JSON（助手消息）
            risk_flag INTEGER DEFAULT 0,       -- 高风险标记（阶段 5 审计启用）
            risk_reason TEXT,
            created_at DATETIME
        )""",
        # 4. 知识库文档
        """CREATE TABLE IF NOT EXISTS kb_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            doc_type TEXT,
            version TEXT,
            status TEXT DEFAULT 'active',      -- active/disabled/deleted
            chunk_count INTEGER DEFAULT 0,
            uploaded_by INTEGER,
            created_at DATETIME,
            disabled_at DATETIME
        )""",
        # 5. 知识库切片
        """CREATE TABLE IF NOT EXISTS kb_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            vector_id TEXT,
            enabled INTEGER DEFAULT 1
        )""",
        # 6. 选课冲突回放记录
        """CREATE TABLE IF NOT EXISTS conflict_replay_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_no TEXT UNIQUE,
            semester TEXT,
            check_input_json TEXT,
            check_result_json TEXT,
            data_source TEXT,                  -- realtime / snapshot
            snapshot_at DATETIME,
            draft_id INTEGER,
            confirmed_by INTEGER,
            created_at DATETIME
        )""",
        # 7. 交接任务单据
        """CREATE TABLE IF NOT EXISTS handover_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_no TEXT UNIQUE,
            version INTEGER DEFAULT 1,
            status TEXT DEFAULT 'pending',     -- pending/accepted/completed/escalated
            title TEXT,
            unresolved_items TEXT,
            resolution TEXT,                   -- 处理结果（完成时必填，阶段 7）
            creator_id INTEGER,
            assignee_id INTEGER,
            accepted_at DATETIME,
            escalate_at DATETIME,
            escalated INTEGER DEFAULT 0,
            created_at DATETIME
        )""",
        # 8. Agent 工具调用日志（阶段 7 启用）
        """CREATE TABLE IF NOT EXISTS agent_tool_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            message_id INTEGER,
            round_no INTEGER,
            tool_name TEXT,
            input_summary TEXT,
            output_summary TEXT,
            duration_ms INTEGER,
            created_at DATETIME
        )""",
        # 9. 调课调整草稿（阶段 6/7 启用）
        """CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_no TEXT,
            status TEXT DEFAULT 'pending_confirm',  -- pending_confirm/confirmed/rejected
            content_json TEXT,
            created_by_session TEXT,
            confirmed_by INTEGER,
            confirmed_at DATETIME,
            created_at DATETIME,                -- 草稿创建时间（列表展示/排序用）
            reject_reason TEXT                  -- 驳回理由（阶段 7）
        )""",
        # 10. 系统配置（提示词模板 + 试点开关，阶段 5 启用）
        """CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_by INTEGER,
            updated_at DATETIME
        )""",
        # 11. 协作角色核校/验收留痕（阶段 8：专业负责人提交，只留痕不判定）
        """CREATE TABLE IF NOT EXISTS co_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_type TEXT NOT NULL,         -- program(培养方案核校) / handover(交接单验收)
            target_ref TEXT,                   -- program 固定 'current'；handover 存交接单 id
            verdict TEXT NOT NULL,             -- pass(通过) / issue(有问题)
            issues TEXT,                       -- 发现问题说明（verdict=issue 时必填）
            verifier_id INTEGER NOT NULL,      -- 提交人（专业负责人）
            created_at DATETIME
        )""",
    ]
    with _lock:
        for stmt in ddl:
            get_conn().execute(stmt)
        get_conn().commit()
