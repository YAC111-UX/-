# -*- coding: utf-8 -*-
"""
协作角色路由（阶段 8）：辅导员报备 + 专业负责人核校/验收。
权限（docs/07-协作角色功能设计.md 第三节）：
  /api/co/reports       仅 counselor
  /api/co/program*      仅 program_leader
  /api/co/acceptance*   仅 program_leader
  /api/co/verify/history 仅 program_leader
其余角色一律 1005（复用 require_roles）。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.errors import biz_error
from app.core.security import require_roles
from app.services import collab

router = APIRouter(prefix="/api/co")


class ReportBody(BaseModel):
    kind: str = "transfer"          # transfer=转报备 / incident=现场异常登记
    session_id: str = ""            # 转报备必填（服务端取最后一条提问原文）
    title: str = ""                 # 现场异常必填
    detail: str = ""


class VerifyBody(BaseModel):
    target_type: str = "program"    # program=核校 / handover=验收
    target_ref: str = "current"     # program 固定 current；handover 传交接单 id
    verdict: str = "pass"           # pass / issue
    issues: str = ""


class AcceptBody(BaseModel):
    verdict: str = "pass"           # pass / issue
    comment: str = ""


# ==================== 辅导员：报备 ====================

@router.post("/reports")
def create_report(body: ReportBody, user=Depends(require_roles("counselor"))):
    """新建报备（转报备/现场异常）→ 生成 handover_task，返回业务编号。"""
    ok, err, result = collab.create_report(
        user["id"], body.kind, body.session_id, body.title, body.detail,
    )
    if not ok:
        raise biz_error(err)
    return {"code": 0, "message": result["message"], "data": result}


@router.get("/reports")
def list_my_reports(user=Depends(require_roles("counselor"))):
    """我的报备单列表（带状态/接手人/验收标记）。"""
    return {"code": 0, "message": "ok",
            "data": {"items": collab.list_my_reports(user["id"])}}


# ==================== 专业负责人：培养方案核校 ====================

@router.get("/program")
def get_program(user=Depends(require_roles("program_leader"))):
    """培养方案课程清单 + 先修映射（只读聚合）。"""
    return {"code": 0, "message": "ok", "data": collab.get_program()}


@router.post("/program/verify")
def verify_program(body: VerifyBody, user=Depends(require_roles("program_leader"))):
    """提交培养方案核校意见（只留痕不判定）。"""
    ok, err = collab.save_verify(
        user["id"], "program", "current", body.verdict, body.issues,
    )
    if not ok:
        raise biz_error(err)
    return {"code": 0, "message": "核校意见已留痕"}


@router.get("/verify/history")
def verify_history(user=Depends(require_roles("program_leader"))):
    """我的核校/验收历史（最新在前）。"""
    return {"code": 0, "message": "ok",
            "data": {"items": collab.list_verify_history(user["id"])}}


# ==================== 专业负责人：交接单验收 ====================

@router.get("/acceptance")
def list_acceptance(user=Depends(require_roles("program_leader"))):
    """已完成交接单列表 + 最新验收状态。"""
    return {"code": 0, "message": "ok",
            "data": {"items": collab.list_acceptance()}}


@router.post("/acceptance/{task_id}")
def accept_task(task_id: int, body: AcceptBody,
                user=Depends(require_roles("program_leader"))):
    """验收已完成交接单（verdict + 意见，落痕）。"""
    ok, err = collab.save_verify(
        user["id"], "handover", str(task_id), body.verdict, body.comment,
    )
    if not ok:
        raise biz_error(err)
    return {"code": 0, "message": "验收意见已留痕"}
