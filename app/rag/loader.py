# -*- coding: utf-8 -*-
"""文档读取：加载知识库目录下全部 markdown 制度文档。【★核心模块·可复用】"""

from pathlib import Path

from app.core.errors import biz_error


def load_markdown_files(kb_dir: Path) -> list:
    """
    读取目录下全部 .md 制度文档。
    返回: [{"filename": 文件名, "content": 全文}, ...]
    知识库为空时抛 BizError(2003)，前端显示"知识库正在维护"。
    """
    if not kb_dir.exists():
        raise biz_error("2003")

    docs = []
    for md_file in sorted(kb_dir.glob("*.md")):
        docs.append({
            "filename": md_file.name,
            "content": md_file.read_text(encoding="utf-8"),
        })
    if not docs:
        raise biz_error("2003")
    return docs
