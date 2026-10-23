"""Agent 服务的 HTTP 面（默认 :8711）。

**只有主后端能调它**：每个请求都要带 `X-Internal-Token`，令牌与主后端共享
（``data/secrets/agent.token``）。这不是"本地应用不用鉴权"的问题 —— 端口开着就意味着
同一台机器上的任何进程都能让 agent 去读你的论文，令牌是那条边界唯一的东西。

SSE 的帧格式在 `runtime.format_sse`，本模块只负责"把事件流发出去"，
业务编排一行都不在这里。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.agent.config import AgentConfigError, limits_payload, load
from backend.agent.host import HostClient
from backend.agent.runtime import AgentRuntime, AskRequest, format_sse
from backend.core.config import get_settings
from backend.core.logging import get_logger, setup_logging

log = get_logger(__name__)


def create_app() -> FastAPI:
    setup_logging()
    cfg = load()                      # 缺令牌在这里就失败，不带病启动
    host = HostClient(cfg)
    runtime = AgentRuntime(cfg, host)

    app = FastAPI(title="Cylit Agent", version="0.1.0", docs_url="/docs",
                  openapi_url="/openapi.json")
    app.state.cfg = cfg
    app.state.host = host
    app.state.runtime = runtime

    def _auth(token: str | None) -> None:
        if not token or token != cfg.token:
            raise HTTPException(status_code=401, detail="内部令牌不正确")

    @app.on_event("startup")
    async def _record_limits() -> None:
        """把本进程**实际生效**的五项限额记进 `data/agent_limits.applied.json`。

        这是 `GET /agent/limits` 里 `restart_required` 的另一半：保存时间比这个
        时间戳新，就说明"设置页改完还没重启问答服务"。写失败只记日志 ——
        它只影响一个界面提示，不该把启动拖下来。
        """
        actual = limits_payload(cfg)
        log.info("运行限额：%s", actual)
        try:
            get_settings().mark_agent_limits_applied(actual)
        except Exception as exc:  # noqa: BLE001
            log.warning("限额生效时间戳写入失败（不影响问答）：%s", exc)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await host.aclose()

    # ---------------- 运维 ----------------
    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"ok": True, "app": "cylit-agent"}

    @app.get("/readyz")
    async def readyz(x_internal_token: str | None = Header(None)) -> dict:
        """自检：主后端可达 + 模型配置可取 + skills 目录可读。"""
        _auth(x_internal_token)
        report: dict[str, Any] = {"ok": True}
        report["main_backend"] = await host.ready()
        try:
            cfg_model = await runtime.model_config(force=True)
            report["model"] = {"ok": True, "model_name": cfg_model.get("model_name"),
                               "provider": cfg_model.get("provider"),
                               "has_key": bool(cfg_model.get("api_key"))}
        except Exception as exc:  # noqa: BLE001
            report["model"] = {"ok": False, "error": str(exc)[:200]}
        skills = await runtime.skill_list()
        report["skills"] = {"ok": bool(skills), "count": len(skills),
                            "names": [s["name"] for s in skills]}
        report["ok"] = bool(report["main_backend"].get("ok")
                            and report["model"].get("ok") and skills)
        return report

    # ---------------- skills ----------------
    @app.get("/v1/skills")
    async def list_skills(x_internal_token: str | None = Header(None)) -> dict:
        _auth(x_internal_token)
        return {"skills": await runtime.skill_list(), "dir": str(cfg.skills_dir)}

    @app.post("/v1/skills/reload")
    async def reload_skills(x_internal_token: str | None = Header(None)) -> dict:
        _auth(x_internal_token)
        runtime.reload_skills()
        skills = await runtime.skill_list()
        log.info("skills 已重载：%s", [s["name"] for s in skills])
        return {"reloaded": True, "skills": skills}

    # ---------------- 压缩 ----------------
    @app.post("/v1/compress")
    async def compress_endpoint(payload: dict = Body(...),
                                x_internal_token: str | None = Header(None)) -> dict:
        """把一段对话记录压成要点（主后端落成 `summary` 消息；见 `compress.py`）。"""
        _auth(x_internal_token)
        from backend.agent.compress import compress

        try:
            return await compress(cfg, host, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("压缩失败：%s", exc)
            raise HTTPException(status_code=502, detail=f"压缩失败：{exc}"[:200]) from exc

    # ---------------- 元数据填写 ----------------
    @app.post("/v1/meta-fill")
    async def meta_fill(payload: dict = Body(...),
                        x_internal_token: str | None = Header(None)) -> dict:
        """主后端的「模型补全论文信息」按钮：让 agent **读全文**把元数据填准。

        不流式、不落消息（用户口径：完整 agent 功能，不用显示细节）—— 跑完只回报
        "填了哪些字段"。agent 内部通过宿主接口把值写回论文行，所以这里不改数据库。
        """
        _auth(x_internal_token)
        paper_id = str(payload.get("paper_id") or "").strip()
        if not paper_id:
            raise HTTPException(status_code=422, detail="paper_id 必填")
        try:
            return await runtime.meta_fill(paper_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("元数据填写失败：%s", exc)
            raise HTTPException(status_code=502,
                                detail=f"元数据填写失败：{exc}"[:200]) from exc

    # ---------------- 问答 ----------------
    @app.post("/v1/ask")
    async def ask(request: Request, payload: dict = Body(...),
                  x_internal_token: str | None = Header(None)):
        _auth(x_internal_token)
        question = str(payload.get("question") or "").strip()
        paper_id = str(payload.get("paper_id") or "").strip()
        selection = payload.get("selection") or None
        attachments = [a for a in (payload.get("attachments") or []) if a]
        # 划词提问时 `question` 可以为空（默认"解释这段内容"）；只发图片也允许
        if not paper_id or not (question or (selection and selection.get("text"))
                                or attachments):
            raise HTTPException(
                status_code=422,
                detail="paper_id 必填，且 question / selection / attachments 至少要有一个")
        req = AskRequest(
            paper_id=paper_id, question=question,
            conversation_id=str(payload.get("conversation_id") or ""),
            parent_id=payload.get("parent_id"),
            history=list(payload.get("history") or []),
            style=str(payload.get("agent_style") or ""),
            style_preset=str(payload.get("agent_style_preset") or ""),
            reply_length=str(payload.get("agent_reply_length") or ""),
            model_id=payload.get("model_id"),
            reasoning_effort=str(payload.get("reasoning_effort") or ""),
            selection=selection,
            attachments=list(attachments),
        )

        async def _sse():
            try:
                async for event in runtime.ask(req):
                    yield format_sse(event)
            except asyncio.CancelledError:
                # 客户端断开：尽力发一个终态（§11 硬规则①），然后让它取消下去
                log.info("问答被取消：conversation=%s", req.conversation_id)
                yield format_sse({"type": "cancelled"})
                raise

        return StreamingResponse(_sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    return app


def main() -> int:
    """``python -m backend.agent`` 的入口，见 `__main__.py`。"""
    import uvicorn

    try:
        cfg = load()
    except AgentConfigError as exc:
        print(f"启动失败：{exc}")
        return 2
    print(f"Agent 服务：http://{cfg.main_base.split('//')[-1]} 是主后端；"
          f"skills={cfg.skills_dir}")
    uvicorn.run("backend.agent.service:create_app", host="127.0.0.1",
                port=_port(), factory=True, log_level="info")
    return 0


def _port() -> int:
    from backend.core.config import get_settings

    return int(get_settings().agent_port)
