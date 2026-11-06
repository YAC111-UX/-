# -*- coding: utf-8 -*-
"""
知识库存取层：制度文档 → 切片 → 落库（kb_documents + kb_chunks 两张表）。【★核心模块·可复用】
阶段 5 起，RAG 检索数据源从"直接读 md 文件"切换为 kb_chunks 表（enabled=1），
管理员上传 / 停用 / 删除文档都通过本模块落库，再由 pipeline.rebuild() 重建索引。
"""

from pathlib import Path

from app.config import Config
from app.core.errors import biz_error
from app.db import database as db
from app.rag.chunker import chunk_text


def extract_text(filename: str, raw: bytes) -> str:
    """
    从上传文件字节流中提取纯文本。
      .md  → 按 UTF-8 解码
      .pdf → pypdf 逐页提取
    格式不支持抛 1007，解析失败 / 内容为空抛 1008。
    """
    name = filename.lower()
    if name.endswith(".md") or name.endswith(".markdown"):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise biz_error("1008")          # 编码坏 → 解析失败
    elif name.endswith(".pdf"):
        try:
            from io import BytesIO
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            raise biz_error("1008")          # pypdf 解不开 → 解析失败
    else:
        raise biz_error("1007")              # 仅支持 md / pdf
    if not text.strip():
        raise biz_error("1008")              # 提取不到任何文字（如纯扫描件）
    return text


def add_document(filename: str, content: str, doc_type: str,
                 uploaded_by: int = None) -> int:
    """
    新文档入库：切片 → 写 kb_documents + kb_chunks，返回文档 id。
    切片规则与阶段 3 保持一致（500 字符 + 50 重叠）。
    """
    doc_type = doc_type or ("pdf" if filename.lower().endswith(".pdf") else "md")
    cur_id = None
    pieces = chunk_text(content)
    with db._lock:                           # 文档 + 切片同事务写入，保证一致性
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO kb_documents (filename, doc_type, version, status, chunk_count, "
            "uploaded_by, created_at) VALUES (?, ?, 'v1', 'active', ?, ?, ?)",
            (filename, doc_type, len(pieces), uploaded_by, db.now()),
        )
        cur_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for i, piece in enumerate(pieces):
            conn.execute(
                "INSERT INTO kb_chunks (document_id, chunk_index, content, enabled) "
                "VALUES (?, ?, ?, 1)",
                (cur_id, i, piece),
            )
        conn.commit()
    return cur_id


def import_markdown_dir_if_empty() -> int:
    """
    首启无缝升级：kb_documents 表为空时，把 data/kb_source/ 现有 md 全部导入。
    返回本次导入的文档数（表非空返回 0，跳过导入）。
    """
    count = db.query_all("SELECT COUNT(*) AS n FROM kb_documents")[0]["n"]
    if count > 0:
        return 0
    if not Config.KB_DIR.exists():
        return 0
    imported = 0
    for md_file in sorted(Config.KB_DIR.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        add_document(md_file.name, content, "md")
        imported += 1
    if imported:
        print(f"[KB] 首启自动导入 {imported} 份制度文档到 kb_documents/kb_chunks 表")
    return imported


def set_doc_status(doc_id: int, status: str) -> bool:
    """
    停用 / 启用文档：
      disabled → 文档 status=disabled 且切片 enabled=0（检索立即剔除）
      active   → 文档 status=active 且切片 enabled=1
    返回是否确实更新了文档（doc 不存在返回 False）。
    """
    enabled = 1 if status == "active" else 0
    with db._lock:
        conn = db.get_conn()
        cur = conn.execute(
            "UPDATE kb_documents SET status = ?, disabled_at = ? WHERE id = ?",
            (status, db.now() if status == "disabled" else None, doc_id),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE kb_chunks SET enabled = ? WHERE document_id = ?", (enabled, doc_id)
        )
        conn.commit()
    return True


def delete_document(doc_id: int) -> bool:
    """
    删除文档：kb_documents.status 标记 deleted（保留审计痕迹），
    kb_chunks 物理删除（检索立即剔除）。返回文档是否存在。
    """
    with db._lock:
        conn = db.get_conn()
        cur = conn.execute(
            "UPDATE kb_documents SET status = 'deleted' WHERE id = ?", (doc_id,)
        )
        if cur.rowcount == 0:
            return False
        conn.execute("DELETE FROM kb_chunks WHERE document_id = ?", (doc_id,))
        conn.commit()
    return True


def load_active_chunks() -> list:
    """
    读取全部启用切片（RAG 索引数据源），联表取文档名供引用来源展示。
    返回: [{"content": 切片原文, "filename": 文档名}, ...]
    """
    rows = db.query_all(
        "SELECT c.content, d.filename FROM kb_chunks c "
        "JOIN kb_documents d ON c.document_id = d.id "
        "WHERE c.enabled = 1 AND d.status = 'active' "
        "ORDER BY d.id, c.chunk_index"
    )
    return [{"content": r["content"], "filename": r["filename"]} for r in rows]
