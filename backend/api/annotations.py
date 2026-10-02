"""高亮 / 笔记 / 批注 / 划词解释 API（规划 §20.6）。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request

from backend.api.response import envelope
from backend.services.annotation import annotation_service as svc

router = APIRouter(tags=["annotations"])


# ---------------- 高亮 ----------------
@router.post("/highlights")
async def create_highlight(request: Request, payload: dict = Body(...)):
    return envelope(svc.create_highlight(payload), request=request)


@router.get("/highlights")
async def list_highlights(request: Request, paper_id: str = Query(...)):
    return envelope(svc.list_highlights(paper_id), request=request)


@router.delete("/highlights/{highlight_id}")
async def delete_highlight(highlight_id: str, request: Request):
    return envelope(svc.delete_highlight(highlight_id), request=request)


# ---------------- 笔记 ----------------
@router.post("/notes")
async def create_note(request: Request, payload: dict = Body(...)):
    return envelope(svc.create_note(payload), request=request)


@router.get("/notes")
async def list_notes(request: Request, paper_id: str | None = Query(None),
                     q: str | None = Query(None)):
    return envelope(svc.list_notes(paper_id, q=q), request=request)


@router.put("/notes/{note_id}")
async def update_note(note_id: str, request: Request, payload: dict = Body(...)):
    return envelope(svc.update_note(note_id, payload), request=request)


@router.delete("/notes/{note_id}")
async def delete_note(note_id: str, request: Request):
    return envelope(svc.delete_note(note_id), request=request)


@router.get("/notes/overview")
async def notes_overview(request: Request):
    """跨论文笔记/高亮/批注聚合（/notes 页面）。"""
    return envelope(svc.notes_overview(), request=request)


# ---------------- 批注 ----------------
@router.get("/annotations")
async def list_annotations(request: Request, paper_id: str = Query(...),
                           type: str | None = Query(None, alias="type")):
    return envelope(svc.list_annotations(paper_id, type_=type), request=request)


@router.post("/annotations")
async def create_annotation(request: Request, payload: dict = Body(...)):
    return envelope(svc.create_annotation(payload), request=request)


@router.put("/annotations/{annotation_id}")
async def update_annotation(annotation_id: str, request: Request,
                            payload: dict = Body(...)):
    return envelope(svc.update_annotation(annotation_id, payload), request=request)


@router.delete("/annotations/{annotation_id}")
async def delete_annotation(annotation_id: str, request: Request):
    return envelope(svc.delete_annotation(annotation_id), request=request)


@router.post("/annotations/save")
async def save_annotation(request: Request, payload: dict = Body(...)):
    """把划词解释存为批注（**解释本身由问答 agent 产出**，见 `/api/v1/agent/ask`）。"""
    return envelope(svc.save_explanation_as_annotation(payload), request=request)
