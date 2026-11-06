# -*- coding: utf-8 -*-
"""
场地预约系统只读适配器：场地档案（课表占用 + 维护占用）。
试点期读 data/mock/rooms.json；接口对齐真实场地预约 API，阶段 9 只换实现。
"""

import json

from app.adapters.snapshot import ExternalDown, call_with_snapshot
from app.config import Config

MOCK_FILE = Config.PROJECT_ROOT / "data" / "mock" / "rooms.json"
SNAPSHOT_NAME = "rooms"


def _load_mock() -> dict:
    try:
        return json.loads(MOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ExternalDown(f"mock 数据不可读：{e}") from e


def get_room_data() -> tuple:
    """
    拉取场地基础数据（全部场地档案），带快照降级。
    返回: (rooms_dict, data_source, snapshot_at)
    """
    def fetch():
        return _load_mock().get("rooms", {})

    return call_with_snapshot(fetch, SNAPSHOT_NAME)
