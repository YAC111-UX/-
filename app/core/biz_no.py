# -*- coding: utf-8 -*-
"""
业务编号生成器：BUS + 日期 + 当日 4 位序号，如 BUS20261106-0007。【★核心模块·可复用】
冲突回放 / 调课草稿 / 交接单据三类单据共用，审计据此串联全链路：
"冲突校验 → 草稿生成 → 人工确认 → 兜底交接"。
"""

from app.db import database as db

# 编号所在的三张表（生成时扫全部表取当日最大序号，避免跨表撞号）
_SEQ_TABLES = ["conflict_replay_records", "drafts", "handover_tasks"]


def next_biz_no() -> str:
    """
    生成下一个业务编号。扫描三张表当日已有编号取最大序号 +1，
    试点期单机低并发下不会撞号；多实例部署时需换成数据库序列。
    """
    today = db.now()[:10].replace("-", "")          # YYYYMMDD
    prefix = f"BUS{today}-"
    max_seq = 0
    for table in _SEQ_TABLES:
        rows = db.query_all(
            f"SELECT business_no FROM {table} WHERE business_no LIKE ?",
            (prefix + "%",),
        )
        for r in rows:
            try:
                max_seq = max(max_seq, int(r["business_no"][len(prefix):]))
            except ValueError:
                continue                             # 跳过格式异常的历史数据
    return f"{prefix}{max_seq + 1:04d}"
