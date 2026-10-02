"""Agent API（规划 §4.3）。

**对外**（前端直接调，与其它接口一样本地单用户无鉴权）：`/agent/ask` 转发 SSE、
`/agent/conversations*` 会话 CRUD、`/agent/skills` 已加载的 skill、`/agent/status` 服务状态。

**对内**（只认内部令牌）：`/agent/ping` `/agent/model-config` 与 `/agent/papers/...` 四个
只读投影，加上 `/agent/internal/messages` 落库。这几条是"agent 能看到什么"的实际边界 ——
agent 服务没有数据库凭据，它要什么都得从这里过。
"""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import (APIRouter, Body, File, Header, HTTPException, Query, Request,
                     UploadFile)
from fastapi.responses import FileResponse, StreamingResponse

from backend.api.response import envelope
from backend.core.config import get_settings
from backend.core.errors import ProviderError, ValidationError
from backend.services.agent import (attachments, bridge, conversations, model_config,
                                    project)
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
        # 设置页「问答」卡片要把这些口径说清楚（都是配置项，不是临时值）
        "limits": {
            "max_rounds": s.agent_max_rounds,
            "search_budget": s.agent_search_budget,
            "context_budget": s.agent_context_budget,
            "auto_compress_percent": s.agent_auto_compress_percent,
            "deadline_seconds": s.agent_deadline_seconds,
            "attachment_inline_limit": s.agent_attachment_inline_limit,
            "evidence_floor": s.agent_evidence_floor,
        },
    }, request=request)


@router.get("/agent/skills")
async def agent_skills(request: Request):
    return envelope(await bridge.agent_get("/v1/skills"), request=request)


@router.post("/agent/ask")
async def agent_ask(payload: dict = Body(...)):
    """一轮问答（SSE 直通）。

    请求体：``{paper_id, question?, conversation_id?, selection?: {text, row_id?, page?},
    attachments?: [id], regenerate_from?, edit_of?}``。

    ``selection`` 就是**划词解释 / 划词追问**：给了它，``question`` 可以为空
    （默认"解释这段内容"）。``attachments`` 是图片附件的 id 列表（见 `/agent/attachments`）。

    两种**分叉**写法：

    - ``regenerate_from`` = 某条助手消息 id：重新生成一条回答；
    - ``edit_of`` = 某条用户消息 id：改掉那条提问。

    两者都默认**就地**（就地重答 / 就地改正文 —— 会作废它们之后的问答，会话仍是线性一条）；
    带上 ``branch=true`` 才是**开新分支**：新内容与原内容成为兄弟，两条都留在树里
    （前端那枚 git 分支图标走的就是这条）。

    这里做四件事：校验、把用户消息落库、必要时自动压缩、把请求转给 agent 进程。
    **不解释 agent 的事件**（§4.2）—— 只转发字节，例外只有两处：连不上时合成 `error`、
    自动压缩时先补一帧 `stage`。
    """
    paper_id = str(payload.get("paper_id") or "").strip()
    if not paper_id:
        raise ValidationError("paper_id 必填")
    paper_service.get_paper(paper_id)          # 论文不存在就别往下走（错误信息更清楚）

    question = str(payload.get("question") or "").strip()
    selection = payload.get("selection") or None
    selected_text = str((selection or {}).get("text") or "").strip()
    attachment_ids = [str(a) for a in (payload.get("attachments") or []) if a]
    attachment_meta = attachments.describe(attachment_ids)
    regenerate_from = str(payload.get("regenerate_from") or "").strip()
    edit_of = str(payload.get("edit_of") or "").strip()
    # `branch=true` = **开新分支**（兄弟，原来的保留）；默认就地改/就地重答（用户口径 2026-10-03）
    branch = bool(payload.get("branch"))
    if not (question or selected_text or attachment_meta or regenerate_from or edit_of):
        raise ValidationError("question / selection.text / attachments 至少要有一个")

    conversation_id = conversations.ensure_conversation(
        payload.get("conversation_id"), paper_id,
        title=(question or ("划词解释" if selected_text else "图片提问")))
    if not conversation_id:                    # 理论上不会发生，兜住更安全
        raise ValidationError("conversation_id 缺失")

    if regenerate_from:
        question, selection, attachment_meta, parent_id, history = _branch_regenerate(
            conversation_id, regenerate_from, branch=branch)
    elif edit_of:
        parent_id, history = _branch_edit(conversation_id, edit_of, question,
                                         attachment_meta, branch=branch)
    else:
        content = question or "解释这段内容"
        if selected_text:
            quote = selected_text[:400].replace("\n", " ")
            # 位置写进消息正文（用户口径 2026-10-03："加入对话的内容要包含文本位置"）：
            # 前端给 `where`（"第 9 页 · Markdown" / "第 4 页 · PDF"），没有就退回页号。
            # 落库的正文与前端乐观回显必须是同一个格式，否则刷新前后两副样子。
            where = str((selection or {}).get("where") or "").strip()
            if not where:
                page_no = int((selection or {}).get("page") or 0)
                where = f"第 {page_no} 页" if page_no else ""
            head = f"〔{where}〕" if where else ""
            content = f"> {head}{quote}\n\n{content}"
        elif attachment_meta and not question:
            content = f"（图片 ×{len(attachment_meta)}）"
        user_message = conversations.append_message(
            conversation_id, role="user", content=content,
            attachments=attachment_meta or None)
        # 历史里含刚写进去的这条用户消息，去掉它（agent 自己会把当前问题作为 prompt 传）
        history = conversations.history_for_agent(conversation_id)[:-1]
        parent_id = user_message["message_id"]

    body = {
        "paper_id": paper_id, "question": question,
        "conversation_id": conversation_id, "parent_id": parent_id,
        "history": history,
        "model_id": payload.get("model_id"),
        "selection": selection if selected_text else None,
        "attachments": attachment_meta,
    }
    return StreamingResponse(_ask_stream(body, conversation_id),
                             media_type="text/event-stream", headers=SSE_HEADERS)


def _branch_regenerate(conversation_id: str, assistant_id: str, *,
                       branch: bool) -> tuple:
    """重新生成：``(question, selection, attachments, parent, history)``。

    ``branch=False``（默认，用户口径 2026-10-03）= **就地重答**：把这条回答连它后面的
    问答一起作废、再问一遍 —— 会话仍然是"一问一答"的一条线；
    ``branch=True`` = 旧行为：同一个提问下再挂一条**兄弟**回答，两条都留着（◀ 1/2 ▶ 切）。
    """
    prev = conversations.message_of(conversation_id, assistant_id)
    if prev["role"] != "assistant":
        raise ValidationError("只能重新生成助手的回答")
    parent = prev.get("parent_id")
    if not parent:
        raise ValidationError("这条回答没有对应的提问，无法重新生成")
    ask = conversations.message_of(conversation_id, parent)
    if ask["role"] != "user":
        raise ValidationError("这条回答的上一环不是提问，无法重新生成")
    if branch:
        history = conversations.history_from_chain(
            conversations.branch_history(conversation_id, ask["message_id"]))
    else:
        # 就地：先把这条回答（及其后续）删掉，再把历史收到它的父提问之前
        conversations.drop_subtree(conversation_id, assistant_id, keep=False)
        history = conversations.history_from_chain(
            conversations.branch_history(conversation_id, ask["message_id"]))
    return (ask["content"], None, ask.get("attachments") or [], ask["message_id"], history)


def _branch_edit(conversation_id: str, user_id: str, question: str,
                 attachment_meta: list, *, branch: bool) -> tuple:
    """编辑提问：``(parent, history)``。

    ``branch=False``（默认）= **就地改**：改写这条提问的正文，把它之后的问答作废，
    新答案挂回这条提问下面；
    ``branch=True`` = 旧行为：新提问挂到原提问的**父**下面，与它成为兄弟（原来的保留）。
    """
    if not question:
        raise ValidationError("编辑提问时不能把问题清空")
    origin = conversations.message_of(conversation_id, user_id)
    if origin["role"] != "user":
        raise ValidationError("只能编辑自己的提问")
    if branch:
        user_message = conversations.append_message(
            conversation_id, role="user", content=question,
            parent_id=origin.get("parent_id"),
            # 编辑的是**第一句**时原提问没有父 → 新提问要成为根上的兄弟，不能接到末尾去
            root=origin.get("parent_id") is None,
            attachments=attachment_meta or None)
    else:
        # 就地：改写正文（附件沿用这条的）+ 丢掉它之后的问答，head 回到它身上
        conversations.update_message_content(conversation_id, user_id, question)
        conversations.drop_subtree(conversation_id, user_id, keep=True)
        user_message = {"message_id": user_id}
    history = conversations.history_from_chain(
        conversations.branch_history(conversation_id, user_message["message_id"]))
    return user_message["message_id"], history


async def _ask_stream(body: dict, conversation_id: str) -> AsyncIterator[bytes]:
    """问答的 SSE：自动压缩提示 → 透传 agent 字节。

    **没有追问建议**：那一轮要用另一套 instructions，与主对话的缓存前缀毫无重叠，
    等于每轮多打一次冷启动的 API；而"接下来问什么"本来就能靠接着聊解决
    （阅读器还有目录与引用跳转）。为一个锦上添花的功能付这个代价不值。
    """
    try:
        if await _auto_compress(conversation_id):
            yield bridge.frame("stage", stage="compressing",
                               message="上下文接近预算，已自动压成要点，接着聊…")
        async for chunk in bridge.stream_ask(body):
            yield chunk
    except ProviderError as exc:
        yield bridge.frame("error", code=exc.code or "PROVIDER_ERROR",
                           message=exc.message, retryable=bool(exc.retryable))


async def _auto_compress(conversation_id: str) -> bool:
    """超预算自动压缩（§10.2 的自动版）。

    为什么放在**提问前**：压缩要花一次模型调用，放在回答之后等于让用户多等一次；
    而放在提问前只是把这一轮的延迟拉长一点，用户看到的圆环也不会先冲到 100 再归零。
    """
    s = get_settings()
    try:
        usage = conversations.conversation_usage(conversation_id)
        threshold = float(s.agent_auto_compress_percent)
        if threshold <= 0 or not usage.get("percent"):
            return False
        if float(usage["percent"]) < threshold or int(usage.get("active") or 0) < 4:
            return False
        transcript, count = conversations.transcript_for_compress(conversation_id)
        if count < 2:
            return False
        result = await bridge.agent_post("/v1/compress",
                                         {"paper_id": None, "transcript": transcript})
        summary = str(result.get("summary") or "").strip()
        if not summary:
            return False
        conversations.append_message(conversation_id, role="summary", content=summary)
        return True
    except Exception as exc:  # noqa: BLE001 —— 压缩失败不能把这一轮问答也拖下来
        import logging
        logging.getLogger(__name__).warning("自动压缩失败（继续正常问答）：%s", exc)
        return False


# --------------------------------------------------------------------------
# 图片附件
# --------------------------------------------------------------------------


@router.post("/agent/attachments")
async def upload_attachments(request: Request, files: list[UploadFile] = File(...)):
    """上传图片（**张数不限**，一张一张过闸：类型 + 单张字节数）。

    前端把选中的文件与粘贴进来的图片都塞进同一个 `files` 字段，
    所以这里不需要区分来源。
    """
    if not files:
        raise ValidationError("没有收到文件")
    saved = []
    for item in files:
        data = await item.read()
        saved.append(attachments.save(data, filename=item.filename or "",
                                      mime=item.content_type or ""))
    return envelope({"items": saved, "count": len(saved)}, request=request)


@router.get("/agent/attachments/{attachment_id}")
async def get_attachment(attachment_id: str):
    """取图片本身（前端缩略图 / 回看历史里的图）。"""
    found = attachments.locate(attachment_id)
    if found is None:
        raise HTTPException(status_code=404, detail="图片附件不存在")
    path, meta = found
    return FileResponse(str(path), media_type=str(meta.get("mime") or "image/png"),
                        headers={"Cache-Control": "private, max-age=86400"})


@router.post("/agent/conversations/{conversation_id}/compress")
async def compress_conversation(conversation_id: str, request: Request):
    """压缩这一段对话（**用户点圆环触发**，见规划 §10.2）。

    做法就是"多一轮会话"：让问答模型把已有对话读一遍、压成要点，存成一条 ``summary``
    消息。之后发给模型的 `history` 从这条摘要开始算 —— 内容没删（用户还能往回翻），
    只是模型不再逐条看到它们。
    """
    conversation = conversations.get_conversation(conversation_id)   # 不存在会 404
    transcript, count = conversations.transcript_for_compress(conversation_id)
    if count < 2:
        raise ValidationError("至少要有一次完整问答才能压缩")
    result = await bridge.agent_post("/v1/compress", {
        "paper_id": conversation.get("paper_id"), "transcript": transcript,
    })
    summary = str(result.get("summary") or "").strip()
    if not summary:
        raise ProviderError("压缩没有产出内容，请重试")
    saved = conversations.append_message(conversation_id, role="summary", content=summary)
    return envelope({
        "conversation_id": conversation_id, "summary": summary,
        "compressed": count, "message_id": saved["message_id"],
        "model": result.get("model"),
        "usage": conversations.conversation_usage(conversation_id),
    }, request=request)


@router.get("/agent/conversations")
async def list_conversations(request: Request,
                             paper_id: str | None = Query(None),
                             q: str | None = Query(None),
                             kind: str = Query("all", pattern="^(all|selection|normal)$"),
                             limit: int = Query(80, ge=1, le=200)):
    """会话列表（一级页：搜索 + 分类 + 批量管理的取数口）。

    ``q`` 同时匹配标题与消息正文；``kind`` 按"有没有划词消息"自动分类，见
    `conversations.list_conversations`。
    """
    return envelope({"items": conversations.list_conversations(
        paper_id, limit=limit, query=q or "", kind=kind)}, request=request)


@router.post("/agent/conversations/batch-delete")
async def batch_delete_conversations(request: Request, payload: dict = Body(...)):
    """批量删会话（**一级页的批量管理**）。"""
    ids = payload.get("conversation_ids") or payload.get("ids") or []
    if not isinstance(ids, list):
        raise ValidationError("conversation_ids 必须是数组")
    return envelope(conversations.delete_conversations(ids), request=request)


@router.post("/agent/conversations/{conversation_id}/branch")
async def branch_conversation(conversation_id: str, request: Request,
                              payload: dict = Body(...)):
    """切换可见分支（分叉导航 ◀ 2/3 ▶）：把 head 挪到某条兄弟消息上。"""
    message_id = str(payload.get("message_id") or "").strip()
    if not message_id:
        raise ValidationError("message_id 必填")
    conversations.set_head(conversation_id, message_id)
    return envelope(conversations.get_conversation(conversation_id), request=request)


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
                      lang: str = Query("both", pattern="^(both|en|zh)$"),
                      x_internal_token: str | None = Header(None)):
    """按页返回块级正文 + 行锚点（工具 `page_markdown_get` / `translate_get` 的后端）。"""
    _require_internal(x_internal_token)
    return envelope(project.pages(paper_id, pages, lang), request=request)


@router.get("/agent/papers/{paper_id}/rows/{page}/{row_id}")
async def agent_page_row(paper_id: str, page: int, row_id: str, request: Request,
                         x_internal_token: str | None = Header(None)):
    """页级产物（转录/翻译）里的**一行** —— 工具 `block_get` 展开转录段落的后端。"""
    _require_internal(x_internal_token)
    return envelope(project.row(paper_id, page, row_id), request=request)


@router.get("/agent/papers/{paper_id}/formulas")
async def agent_formulas(paper_id: str, request: Request,
                         page: int | None = Query(None),
                         query: str | None = Query(None),
                         x_internal_token: str | None = Header(None)):
    """公式清单（工具 `formula_get` 的后端）。"""
    _require_internal(x_internal_token)
    return envelope(project.formulas(paper_id, page=page, query=query), request=request)


@router.post("/agent/kb/search")
async def agent_kb_search(request: Request, payload: dict = Body(default={}),
                          x_internal_token: str | None = Header(None)):
    """**跨库**检索（工具 `kb_search` 的后端）；与单篇检索同链路、只差 scope。"""
    _require_internal(x_internal_token)
    return envelope(project.kb_search(str(payload.get("query") or ""),
                                     top_k=int(payload.get("top_k") or 8)),
                    request=request)


@router.get("/agent/library")
async def agent_library(request: Request, q: str | None = Query(None),
                        status: str | None = Query(None),
                        x_internal_token: str | None = Header(None)):
    """书架检索（工具 `library_list` 的后端）。"""
    _require_internal(x_internal_token)
    return envelope(project.library(q=q, status=status), request=request)


@router.get("/agent/notes")
async def agent_notes(request: Request, query: str | None = Query(None),
                      paper_id: str | None = Query(None),
                      kind: str = Query("all", pattern="^(all|notes|highlights|annotations)$"),
                      x_internal_token: str | None = Header(None)):
    """笔记 / 高亮 / 批注（工具 `notes_search` 的后端）。"""
    _require_internal(x_internal_token)
    return envelope(project.notes(query=query, paper_id=paper_id, kind=kind),
                    request=request)


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
        thinking=str(payload.get("thinking") or ""),
        thinking_ms=int(payload.get("thinking_ms") or 0),
        finish_reason=payload.get("finish_reason"),
    ), request=request)
