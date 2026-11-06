# -*- coding: utf-8 -*-
"""
新旧课程代码官方映射表（只读，来源教务处发布）。【★核心模块·可复用】
试点期内置字典 + 读取 mock 数据的映射段；真实映射表下发后仅替换 load_mapping 数据来源。
"""

import json

from app.config import Config

# 内置兜底映射（mock 数据文件缺失时仍可用）
_BUILTIN_MAPPING = {
    "C101": "SW101",
    "C202": "SW202",
}

_mapping_cache = None


def load_mapping() -> dict:
    """读取官方映射表（mock 数据里的 course_mapping 段），带缓存。"""
    global _mapping_cache
    if _mapping_cache is None:
        mapping = dict(_BUILTIN_MAPPING)
        try:
            data = json.loads(
                (Config.PROJECT_ROOT / "data" / "mock" / "academic.json")
                .read_text(encoding="utf-8")
            )
            mapping.update(data.get("course_mapping", {}))
        except (OSError, json.JSONDecodeError):
            pass                                 # 文件缺失用内置兜底
        _mapping_cache = mapping
    return _mapping_cache


def normalize_code(code: str) -> tuple:
    """
    课程代码归一化：旧代码 → 新代码。
    返回 (标准代码, mapping_note)；无映射时原样返回，note 为空串。
    """
    code = (code or "").strip().upper()
    mapping = load_mapping()
    if code in mapping:
        new_code = mapping[code]
        return new_code, f"旧代码 {code} 已按官方映射表换算为 {new_code}"
    return code, ""
