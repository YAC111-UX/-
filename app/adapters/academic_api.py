# -*- coding: utf-8 -*-
"""
教务系统只读适配器：学生已修课程 / 课程先修关系与课程索引。
试点期读 data/mock/academic.json；接口签名与真实 API 对齐，
阶段 9 联调仅替换内部实现（HTTP 调用 + 快照同步），调用方不改。
学生选课数据实时透传，不落本地库（数据最小化红线）。
"""

import json
import random

from app.adapters.snapshot import ExternalDown, call_with_snapshot
from app.config import Config

MOCK_FILE = Config.PROJECT_ROOT / "data" / "mock" / "academic.json"
SNAPSHOT_NAME = "academic"


def _load_mock() -> dict:
    """读取 Mock 数据文件；缺失视为外部系统故障（走快照降级）。"""
    try:
        return json.loads(MOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ExternalDown(f"mock 数据不可读：{e}") from e


def get_academic_data() -> tuple:
    """
    拉取教务基础数据（课程索引 + 学生已修集合 + 映射表），带快照降级。
    返回: (data, data_source, snapshot_at)
      data = {"semester", "courses_index", "students", "mapping"}
    """
    def fetch():
        mock = _load_mock()
        return {
            "semester": mock.get("semester", ""),
            "courses_index": {c["code"]: c for c in mock.get("courses", [])},
            "students": mock.get("students", {}),
            "mapping": mock.get("course_mapping", {}),
        }

    return call_with_snapshot(fetch, SNAPSHOT_NAME)


def get_student_courses(student_id: str, academic_data: dict) -> list:
    """取学生已修课程代码列表（伪 ID 查不到返回空表，由规则层按'无已修记录'处理）。"""
    student = academic_data.get("students", {}).get(student_id)
    return student.get("completed", []) if student else []
