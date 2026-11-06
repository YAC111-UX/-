# -*- coding: utf-8 -*-
"""文本切片：500 字符滑动窗口切片。【★核心模块·可复用】"""

from app.config import Config


def chunk_text(text: str, size: int = Config.CHUNK_SIZE,
               overlap: int = Config.CHUNK_OVERLAP) -> list:
    """
    滑动窗口切片：每片最长 size 字符，相邻切片重叠 overlap 字符。
    重叠的作用：制度条文常在切片边界被截断，重叠片段保证
    至少有一个切片包含完整条文，提升检索命中率。
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        piece = text[start:end].strip()
        if piece:                      # 跳过纯空白切片
            chunks.append(piece)
        if end >= len(text):
            break
        start = end - overlap          # 下一片回退 overlap 字符
    return chunks
