# -*- coding: utf-8 -*-
"""
FastAPI 应用入口。
职责：初始化数据库与种子数据 → 构建 RAG 流水线 → 注册路由 → 提供聊天页与管理后台页面。
启动命令（项目根目录）：uvicorn app.main:app --port 8000
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.core.errors import BizError
from app.db.database import init_db
from app.db.seed import seed
from app.rag import pipeline as rag
from app.api.chat import router as chat_router
from app.api.auth import router as auth_router
from app.api.admin_kb import router as admin_kb_router
from app.api.admin_prompt import router as admin_prompt_router
from app.api.admin_audit import router as admin_audit_router
from app.api.admin_workflow import router as admin_workflow_router
from app.api.co import router as co_router

app = FastAPI(title="知行职业学院排课选课助手", version="0.4.0")

# ---- 注册路由：学生问答 + 管理后台接口 + 协作角色接口 ----
app.include_router(chat_router)
app.include_router(auth_router)
app.include_router(admin_kb_router)
app.include_router(admin_prompt_router)
app.include_router(admin_audit_router)
app.include_router(admin_workflow_router)
app.include_router(co_router)


@app.exception_handler(BizError)
def biz_error_handler(request: Request, exc: BizError):
    """全局业务异常处理器：管理后台接口抛出的 BizError 统一转 {code, message} JSON。"""
    return JSONResponse(status_code=200, content={"code": exc.code, "message": exc.message})


# 页面路径
WEB_DIR = Path(__file__).resolve().parent / "web"
ADMIN_DIR = WEB_DIR / "admin"


@app.on_event("startup")
def startup():
    """
    应用启动流程（顺序有讲究）：
      1. 建表 → 2. 种子数据（默认账号/默认提示词）→ 3. 首启导入历史 md 到 kb 表
      → 4. 构建 RAG 索引（数据源为 kb_chunks 表，只构建一次，全局复用）
    """
    init_db()
    seed()
    from app.rag import kb_store
    kb_store.import_markdown_dir_if_empty()
    rag.pipeline = rag.RagPipeline()


# ---- 学生 / 辅导员聊天页面 ----
@app.get("/", include_in_schema=False)
def index():
    """聊天页面（单页 HTML + 原生 JS）。"""
    return FileResponse(WEB_DIR / "index.html")


# ---- 管理后台页面（多页应用，页面本身不鉴权，数据接口逐一校验 JWT + 角色） ----
@app.get("/admin/login", include_in_schema=False)
def admin_login_page():
    return FileResponse(ADMIN_DIR / "login.html")


@app.get("/admin/kb", include_in_schema=False)
def admin_kb_page():
    return FileResponse(ADMIN_DIR / "kb.html")


@app.get("/admin/prompt", include_in_schema=False)
def admin_prompt_page():
    return FileResponse(ADMIN_DIR / "prompt.html")


@app.get("/admin/audit", include_in_schema=False)
def admin_audit_page():
    return FileResponse(ADMIN_DIR / "audit.html")


# ---- 阶段 7：人工协作工作流页面 ----
@app.get("/admin/drafts", include_in_schema=False)
def admin_drafts_page():
    return FileResponse(ADMIN_DIR / "drafts.html")


@app.get("/admin/handover", include_in_schema=False)
def admin_handover_page():
    return FileResponse(ADMIN_DIR / "handover.html")


# ---- 阶段 8：协作角色工作台（辅导员/专业负责人，与管理后台共用 JWT） ----
@app.get("/workbench", include_in_schema=False)
def workbench_page():
    return FileResponse(WEB_DIR / "workbench.html")
