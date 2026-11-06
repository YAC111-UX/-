# -*- coding: utf-8 -*-
"""
场地/实训室冲突校验（纯规则引擎，不依赖 LLM）。【★核心模块·可复用】
两路数据源取并集：课表排课占用 + 维护占用；
维护占用但课表仍标可用 → needs_manual=true（需求清单必转场景 1）。
"""


def check_room_conflict(room_info: dict, week_day: int, section: str) -> dict:
    """
    参数:
        room_info: 场地档案 {"name","capacity","schedule":[...],"maintenance":[...]}
        week_day:  星期几（1=周一）
        section:   节次（如 "3-4节"）
    返回:
        { "room", "room_name", "time_slot", "available",
          "conflicts": [{"type","detail","source"}], "needs_manual" }
    """
    time_slot = f"周{'一二三四五六日'[week_day - 1]} {section}"
    conflicts = []

    # 通道 1：课表排课占用
    for item in room_info.get("schedule", []):
        if item.get("week_day") == week_day and item.get("section") == section:
            conflicts.append({
                "type": "course",
                "detail": f"已排课程：{item.get('course', '未知课程')}",
                "source": "排课表",
            })

    # 通道 2：维护占用
    for item in room_info.get("maintenance", []):
        if item.get("week_day") == week_day and item.get("section") == section:
            conflicts.append({
                "type": "maintenance",
                "detail": f"维护占用：{item.get('reason', '未知原因')}"
                          + (f"（至 {item['until']}）" if item.get("until") else ""),
                "source": "场地预约系统",
            })

    return {
        "room": room_info.get("name", ""),
        "time_slot": time_slot,
        "available": len(conflicts) == 0,
        "conflicts": conflicts,
        # 维护占用即转人工核对（课表与场地系统数据可能不同步）
        "needs_manual": any(c["type"] == "maintenance" for c in conflicts),
    }
