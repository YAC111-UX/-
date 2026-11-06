# -*- coding: utf-8 -*-
"""
知识库管理路由（/api/admin/kb）：文档列表 / 上传 / 启停 / 删除。
权限：排课专员(scheduler)、教务主任(director)。
操作流程统一为：改库（kb_documents/kb_chunks）→ rag.pipeline.rebuild() 全量重建索引。
"""

from fastapi import APIRouter, Depends, UploadFile, File

from app.config import Config
from app.core.errors import biz_error
from app.core.security import require_roles
from app.db import database as db
from app.rag import pipeline as rag
from app.rag import kb_store

router = APIRouter(prefix="/api/admin/kb")

# 允许的上传后缀（设计文档：仅 md / pdf）
ALLOWED_SUFFIX = (".md", ".markdown", ".pdf")


@router.get("/documents")
def list_documents(user=Depends(require_roles("scheduler", "director"))):
    """文档列表（不含已删除）：id、文件名、类型、版本、状态、切片数、上传时间。"""
    rows = db.query_all(
        "SELECT id, filename, doc_type, version, status, chunk_count, created_at, disabled_at "
        "FROM kb_documents WHERE status != 'deleted' ORDER BY id DESC"
    )
    return {"code": 0, "message": "ok", "data": {"items": [dict(r) for r in rows]}}


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    user=Depends(require_roles("scheduler", "director")),
):
    """
    上传制度文档（multipart，字段名 file）：
    校验格式 → 提取文本 → 原文存档 data/kb_source/ → 切片落库 → 重建索引。
    """
    filename = file.filename or ""
    if not filename.lower().endswith(ALLOWED_SUFFIX):
        raise biz_error("1007")

    raw = await file.read()
    # 提取纯文本（格式 1007 / 解析失败 1008 在函数内抛出）
    text = kb_store.extract_text(filename, raw)

    # 原文存档到 data/kb_source/（同名覆盖，便于追溯管理员传了什么）
    Config.KB_DIR.mkdir(parents=True, exist_ok=True)
    (Config.KB_DIR / filename).write_bytes(raw)

    # 切片落库 + 重建索引（重建期间问答返回 2003）
    doc_id = kb_store.add_document(filename, text, None, uploaded_by=user["id"])
    rag.rebuild()

    doc = db.query_all("SELECT * FROM kb_documents WHERE id = ?", (doc_id,))[0]
    return {"code": 0, "message": "上传成功，索引已重建", "data": dict(doc)}


@router.post("/documents/{doc_id}/disable")
def disable_document(doc_id: int, user=Depends(require_roles("scheduler", "director"))):
    """停用文档：status=disabled、切片 enabled=0 → 重建索引（该文档立即退出检索）。"""
    if not kb_store.set_doc_status(doc_id, "disabled"):
        raise biz_error("1001")          # 文档不存在按参数错误处理
    rag.rebuild()
    return {"code": 0, "message": "文档已停用，索引已重建"}


@router.post("/documents/{doc_id}/enable")
def enable_document(doc_id: int, user=Depends(require_roles("scheduler", "director"))):
    """重新启用文档：status=active、切片 enabled=1 → 重建索引。"""
    if not kb_store.set_doc_status(doc_id, "active"):
        raise biz_error("1001")
    rag.rebuild()
    return {"code": 0, "message": "文档已启用，索引已重建"}


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int, user=Depends(require_roles("scheduler", "director"))):
    """删除文档：标记 deleted + 物理删除切片 → 重建索引。"""
    if not kb_store.delete_document(doc_id):
        raise biz_error("1001")
    rag.rebuild()
    return {"code": 0, "message": "文档已删除，索引已重建"}
