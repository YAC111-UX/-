# -*- coding: utf-8 -*-
"""
快照降级封装。【★核心模块·可复用】
策略（评审结论 2）：实时查询失败 → 读 data/snapshots/ 带时间戳快照照常参与校验，
结果标注 data_source="snapshot" + snapshot_at；连快照也没有 → 抛业务码 2004（必转场景 4）。
"""

import json
import time

from app.config import Config
from app.core.errors import biz_error

# 模拟外部系统故障的开关（E2E 测试用；真实环境不置位）
OFFLINE = False

SNAPSHOT_DIR = Config.PROJECT_ROOT / "data" / "snapshots"


class ExternalDown(Exception):
    """外部系统不可用（网络失败 / 服务 5xx），由快照层接管降级。"""


def call_with_snapshot(fetch, snapshot_name: str) -> tuple:
    """
    执行外部查询并带快照降级。
    参数:
        fetch: 无参函数，返回数据 dict；外部不可用时抛 ExternalDown
        snapshot_name: 快照文件名（不含扩展名），如 "academic"
    返回:
        (data, data_source, snapshot_at)  data_source ∈ realtime / snapshot
    """
    if not OFFLINE:
        try:
            return fetch(), "realtime", None
        except ExternalDown:
            pass                                  # 落入快照降级

    # ---- 读快照 ----
    snap_file = SNAPSHOT_DIR / f"{snapshot_name}.json"
    if snap_file.exists():
        try:
            snap = json.loads(snap_file.read_text(encoding="utf-8"))
            return snap["data"], "snapshot", snap["snapshot_at"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass                                  # 快照文件损坏视为没有

    # 实时与快照都不可用 → 必转场景 4（关键系统不可用）
    raise biz_error("2004")


def write_snapshot(snapshot_name: str, data: dict) -> None:
    """把一份实时数据写成带时间戳快照（阶段 9 的 15 分钟增量同步会定时调用）。"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "snapshot_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data": data,
    }
    (SNAPSHOT_DIR / f"{snapshot_name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
