"""Agent API（规划 §4.3）。

**对外**（前端直接调，与其它接口一样本地单用户无鉴权）：`/agent/ask` 转发 SSE、
`/agent/conversations*` 会话 CRUD、`/agent/skills` 已加载的 skill、`/agent/status` 服务状态。

**对内**（只认内部令牌）：`/agent/ping` `/agent/model-config` 与 `/agent/papers/...` 四个
只读投影，加上 `/agent/internal/messages` 落库。这几条是"agent 能看到什么"的实际边界 ——
agent 服务没有数据库凭据，它要什么都得从这里过。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from backend.api.response import envelope
from backend.core.config import get_settings
from backend.core.errors import ValidationError
from backend.services.agent import bridge, conversations, model_config, project
from backend.services.paper import paper_service

router = APIRouter(tags=["agent"])

#: SSE 响应的公共头（`X-Accel-Buffering` 是给将来可能加的反向代理看的）
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}


def _require_internal(x_internal_token: str | None = Header(None)) -> None:
    """内部令牌校验：agent 服务与主后端共享 `data/secrets/agent.token`。"""
    token = get_settings().agent_token()
    if not token or x_internal_token != token:
        raise HTTPException(status_code=401, detail="内部令牌不正确")


# --------------------------------------------------------------------------
# 前端接口
# --------------------------------------------------------------------------


@router.get("/agent/status")
async def agent_status(request: Request):
    """问答服务是否在跑 + 用哪个模型（**不含密钥明文**）。"""
    s = get_settings()
    return envelope({
        "enabled": bool(s.agent_enabled),
        "running": await bridge.agent_up() if s.agent_enabled else False,
        "endpoint": f"http://{s.agent_host}:{s.agent_port}",
        "model": model_config.config_for_frontend(),
    }, request=request)


@router.get("/agent/skills")
async def agent_skills(request: Request):
    return envelope(await bridge.agent_get("/v1/skills"), request=request)


@router.post("/agent/ask")
async def agent_ask(payload: dict = Body(...)):
    """一轮问答（SSE 直通）。

    请求体：``{paper_id, question, conversation_id?, history?}``。
    这里做三件事：校验、把用户消息落库、把请求转给 agent 进程。
    **不解释 agent 的事件**（§4.2）—— 只转发字节，唯一的例外是连不上时合成一个 `error` 终态。
    """
    question = str(payload.get("question") or "").strip()
    paper_id = str(payload.get("paper_id") or "").strip()
    if not paper_id or not question:
        raise ValidationError("paper_id 与 question 都是必填")
    paper_service.get_paper(paper_id)          # 论文不存在就别往下走（错误信息更清楚）

    conversation_id = conversations.ensure_conversation(
        payload.get("conversation_id"), paper_id, title=question)
    user_message = conversations.append_message(conversation_id, role="user",
                                                content=question)
    # 历史里含刚写进去的这条用户消息，去掉它（agent 自己会把当前问题作为 prompt 传）
    history = conversations.history_for_agent(conversation_id)[:-1]
    body = {
        "paper_id": paper_id, "question": question,
        "conversation_id": conversation_id,
        "parent_id": user_message["message_id"],
        "history": history,
        "model_id": payload.get("model_id"),
    }
    return StreamingResponse(bridge.stream_ask(body), media_type="text/event-stream",
                             headers=SSE_HEADERS)


@router.get("/agent/conversations")
async def list_conversations(request: Request,
                             paper_id: str | None = Query(None),
                             limit: int = Query(50, ge=1, le=200)):
    return envelope({"items": conversations.list_conversations(paper_id, limit=limit)},
                    request=request)


@router.get("/agent/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request):
    return envelope(conversations.get_conversation(conversation_id), request=request)


@router.put("/agent/conversations/{conversation_id}")
async def rename_conversation(conversation_id: str, request: Request,
                              payload: dict = Body(...)):
    return envelope(conversations.rename_conversation(
        conversation_id, str(payload.get("title") or "")), request=request)


@router.delete("/agent/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request):
    return envelope(conversations.delete_conversation(conversation_id), request=request)


# --------------------------------------------------------------------------
# 内部接口（agent 服务专用）
# --------------------------------------------------------------------------


@router.get("/agent/ping")
async def agent_ping(request: Request,
                     x_internal_token: str | None = Header(None)):
    _require_internal(x_internal_token)
    return envelope({"pong": True, "app": get_settings().app_name}, request=request)


@router.get("/agent/model-config")
async def agent_model_config(request: Request,
                             x_internal_token: str | None = Header(None)):
    """问答模型配置（含已解析的密钥）。**只回环 + 内部令牌可读。**"""
    _require_internal(x_internal_token)
    return envelope(model_config.resolve(), request=request)


@router.get("/agent/papers/{paper_id}/archive")
async def agent_archive(paper_id: str, request: Request,
                        x_internal_token: str | None = Header(None)):
    """论文档案 + 大纲 + 规模（每轮问答前预注入，§9.3）。"""
    _require_internal(x_internal_token)
    return envelope(project.archive(paper_id), request=request)


@router.get("/agent/papers/{paper_id}/pages")
async def agent_pages(paper_id: str, request: Request,
                      pages: str = Query(..., description='"3" / "3-8" / "all"'),
                      x_internal_token: str | None = Header(None)):
    """按页返回块级正文 + 行锚点（工具 `page_markdown_get` 的后端）。"""
    _require_internal(x_internal_token)
    return envelope(project.pages(paper_id, pages), request=request)


@router.post("/agent/papers/{paper_id}/search")
async def agent_search(paper_id: str, request: Request, payload: dict = Body(default={}),
                       x_internal_token: str | None = Header(None)):
    """**本论文内**检索（固定 ``mode="qa"``；见 `project.search` 的 docstring）。"""
    _require_internal(x_internal_token)
    return envelope(project.search(
        paper_id, str(payload.get("query") or ""),
        top_k=int(payload.get("top_k") or 8),
        chunk_types=payload.get("chunk_types") or None,
    ), request=request)


@router.get("/agent/papers/{paper_id}/figures")
async def agent_figures(paper_id: str, request: Request,
                        page: int | None = Query(None),
                        x_internal_token: str | None = Header(None)):
    _require_internal(x_internal_token)
    return envelope(project.figures(paper_id, page), request=request)


@router.get("/agent/papers/{paper_id}/pages/{page_no}/image")
async def agent_page_image(paper_id: str, page_no: int,
                           dpi: int | None = Query(None, ge=72, le=600),
                           x_internal_token: str | None = Header(None)):
    """页面图（给视觉模型看；``dpi`` 触发按需渲染，见 `resolve_page_image`）。"""
    _require_internal(x_internal_token)
    path = paper_service.resolve_page_image(paper_id, page_no, dpi)
    return FileResponse(str(path), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.post("/agent/internal/messages")
async def agent_persist_message(request: Request, payload: dict = Body(...),
                                x_internal_token: str | None = Header(None)):
    """agent 落一条助手消息（**在发 `done` 之前**，§11 硬规则②）。"""
    _require_internal(x_internal_token)
    conversation_id = str(payload.get("conversation_id") or "")
    if not conversation_id:
        raise ValidationError("conversation_id 必填")
    return envelope(conversations.append_message(
        conversation_id,
        role=str(payload.get("role") or "assistant"),
        content=str(payload.get("content") or ""),
        parent_id=payload.get("parent_id"),
        citations=payload.get("citations") or [],
        tool_trace=payload.get("tool_trace") or [],
        finish_reason=payload.get("finish_reason"),
    ), request=request)
