# -*- coding: utf-8 -*-
"""
人工协作工作流路由：调课草稿确认 + 交接单认领/完成。
权限（docs/06-协作工作流设计.md 第二节）：
  草稿：列表 scheduler/director，确认与驳回仅 scheduler
  交接单：scheduler/director；认领与完成仅被派人人本人（服务层校验 assignee_id）
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.errors import biz_error
from app.core.security import require_roles
from app.services import workflow

router = APIRouter(prefix="/api/admin")


class RejectBody(BaseModel):
    reason: str = ""


class CompleteBody(BaseModel):
    resolution: str = ""


# ==================== 调课草稿 ====================

@router.get("/drafts")
def list_drafts(status: str = None,
                user=Depends(require_roles("scheduler", "director"))):
    """草稿列表（director 只读）。"""
    return {"code": 0, "message": "ok",
            "data": {"items": workflow.list_drafts(status)}}


@router.post("/drafts/{draft_id}/confirm")
def confirm_draft(draft_id: int, user=Depends(require_roles("scheduler"))):
    """确认草稿（仅排课专员）→ 终态 + 回填回放记录签核人。"""
    ok, err = workflow.confirm_draft(draft_id, user["id"])
    if not ok:
        raise biz_error(err)
    return {"code": 0, "message": "草稿已确认并生效，签核信息已写入审计记录"}


@router.post("/drafts/{draft_id}/reject")
def reject_draft(draft_id: int, body: RejectBody,
                 user=Depends(require_roles("scheduler"))):
    """驳回草稿（仅排课专员），理由必填。"""
    ok, err = workflow.reject_draft(draft_id, user["id"], body.reason)
    if not ok:
        raise biz_error(err)
    return {"code": 0, "message": "草稿已驳回"}


# ==================== 交接单据 ====================

@router.get("/handover")
def list_handover(status: str = None, keyword: str = None, mine: bool = False,
                  user=Depends(require_roles("scheduler", "director"))):
    """交接单列表；查询前先做超时升级惰性检查（到期单自动改派教务主任）。"""
    escalated = workflow.sweep_escalations()
    items = workflow.list_handover(status, keyword, mine, user["id"])
    return {"code": 0, "message": "ok",
            "data": {"items": items, "escalated_now": escalated}}


@router.post("/handover/{task_id}/accept")
def accept_task(task_id: int, user=Depends(require_roles("scheduler", "director"))):
    """认领交接单（仅被派人本人）。"""
    ok, err = workflow.accept_task(task_id, user["id"])
    if not ok:
        raise biz_error(err)
    return {"code": 0, "message": "已认领，请及时处理"}


@router.post("/handover/{task_id}/complete")
def complete_task(task_id: int, body: CompleteBody,
                  user=Depends(require_roles("scheduler", "director"))):
    """完成交接单（仅被派人本人），处理结果必填。"""
    ok, err = workflow.complete_task(task_id, user["id"], body.resolution)
    if not ok:
        raise biz_error(err)
    return {"code": 0, "message": "单据已完成，处理结果已留痕"}
