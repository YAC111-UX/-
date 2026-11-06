# -*- coding: utf-8 -*-
"""
先修冲突校验（纯规则引擎，不依赖 LLM）。【★核心模块·可复用】
输入输出皆结构化，可被 tests 单测直接覆盖——识别率 100% 靠它保证。
"""

from app.rules.course_mapping import normalize_code


def check_prerequisite(course_code: str, completed_courses: list,
                       courses_index: dict) -> dict:
    """
    判定已修课程集合是否满足目标课程的先修要求。

    参数:
        course_code:      目标课程代码（新旧均可，内部归一化）
        completed_courses: 学生已修课程代码列表（旧代码会归一化）
        courses_index:     课程索引 {"SW301": {"code","name","prerequisites":[...]}}
    返回（结构化判定结果，LLM 只负责向用户解释本结果）:
        { "found": 是否找到该课程, "course_code", "course_name",
          "passed", "missing": [{"required_course","reason"}], "mapping_note" }
    """
    normalized, mapping_note = normalize_code(course_code)

    course = courses_index.get(normalized)
    if not course:
        return {"found": False, "course_code": normalized,
                "mapping_note": mapping_note,
                "passed": False, "missing": [],
                "reason_note": "课程库中不存在该课程代码，请核对后重试"}

    # 已修课程也统一归一化（学生旧成绩单上的 C202 = SW202）
    completed = set()
    for c in completed_courses or []:
        completed.add(normalize_code(c)[0])

    missing = []
    for pre in course.get("prerequisites", []):
        if pre not in completed:
            pre_name = courses_index.get(pre, {}).get("name", "")
            missing.append({
                "required_course": f"{pre} {pre_name}".strip(),
                "reason": "未修",
            })

    return {
        "found": True,
        "course_code": normalized,
        "course_name": course.get("name", ""),
        "passed": len(missing) == 0,
        "missing": missing,
        "mapping_note": mapping_note,
    }
