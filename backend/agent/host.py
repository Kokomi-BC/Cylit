"""主后端内部调用客户端（agent → 主后端，全部走 HTTP）。

分工很清楚：**只有主后端碰数据库与产物目录**，agent 想要什么数据都得问它。
好处是"agent 能看到什么"只有一个审批点 —— 加一个工具就要在主后端加一条受控投影，
不存在"agent 顺手读了张表"这种越权。

约定：主后端的响应一律是 ``{data, error, trace_id}``（规划 §20），本模块负责拆封，
`error` 非空就抛 `HostError`（工具层把它转成给模型看的一句失败说明，不抛栈）。
"""

from __future__ import annotations

import httpx

from backend.agent.config import AgentConfig


class HostError(RuntimeError):
    """主后端明确报错（4xx/5xx 或 ``error`` 非空）。"""

    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class HostClient:
    """带内部令牌的 HTTP 客户端（连接复用，进程内单例即可）。"""

    def __init__(self, cfg: AgentConfig) -> None:
        self._cfg = cfg
        self._client: httpx.AsyncClient | None = None

    # ---------------- 生命周期 ----------------
    def _ensure(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._cfg.host_api,
                headers={"X-Internal-Token": self._cfg.token},
                timeout=self._cfg.host_timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ---------------- 基础 ----------------
    @staticmethod
    def _unwrap(resp: httpx.Response) -> object:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            raise HostError(f"主后端返回了非 JSON（HTTP {resp.status_code}）",
                            status=resp.status_code) from None
        if isinstance(body, dict) and body.get("error"):
            err = body["error"] or {}
            raise HostError(str(err.get("message") or "主后端报错"),
                            status=resp.status_code, code=err.get("code"))
        if resp.status_code >= 400:
            raise HostError(f"主后端 HTTP {resp.status_code}", status=resp.status_code)
        return body.get("data") if isinstance(body, dict) and "data" in body else body

    async def get(self, path: str, params: dict | None = None) -> object:
        resp = await self._ensure().get(path, params=params)
        return self._unwrap(resp)

    async def post(self, path: str, payload: dict | None = None) -> object:
        resp = await self._ensure().post(path, json=payload or {})
        return self._unwrap(resp)

    async def put(self, path: str, payload: dict | None = None) -> object:
        resp = await self._ensure().put(path, json=payload or {})
        return self._unwrap(resp)

    async def delete(self, path: str) -> object:
        resp = await self._ensure().delete(path)
        return self._unwrap(resp)

    async def get_bytes(self, path: str, params: dict | None = None
                        ) -> tuple[bytes, str]:
        """取二进制（页图 / 插图裁图）。**不拆封**：这些接口直接返回图片流。"""
        resp = await self._ensure().get(path, params=params)
        if resp.status_code >= 400:
            raise HostError(f"取图失败（HTTP {resp.status_code}）", status=resp.status_code)
        return resp.content, resp.headers.get("content-type", "image/png")

    # ---------------- 拓扑用到的具体接口 ----------------
    async def model_config(self) -> dict:
        """模型配置（含已解析的 key）。agent **只**从这一条路拿模型与密钥。"""
        data = await self.get("/agent/model-config")
        return dict(data or {})

    async def style(self) -> dict:
        """问答**风格**（提示词里可改的那一段，设置页维护）。

        **每轮都取**、不缓存：它拼在 system 最前面，用户改完就该下一句生效。
        返回 ``{style, custom, default}``；失败抛 `HostError`，调用方
        （`AgentRuntime._fetch_style`）会**沿用上一次取到的值**，不回落默认 ——
        风格在 system 最前面，两种值来回跳会把每一轮的 `system + 全部历史` 都打成未命中。
        """
        return dict(await self.get("/agent/style") or {})

    async def archive(self, paper_id: str) -> dict:
        """论文档案 + 大纲 + 规模信息（第一轮之前预注入，§9）。"""
        return dict(await self.get(f"/agent/papers/{paper_id}/archive") or {})

    async def pages(self, paper_id: str, pages: str, lang: str = "both") -> dict:
        """按页取块级正文（``page_markdown_get`` / ``translate_get`` 的后端）。"""
        params = {"pages": pages}
        if lang != "both":
            params["lang"] = lang
        return dict(await self.get(f"/agent/papers/{paper_id}/pages", params) or {})

    async def formulas(self, paper_id: str, *, page: int | None = None,
                       query: str | None = None) -> dict:
        params: dict = {}
        if page:
            params["page"] = int(page)
        if query:
            params["query"] = query
        return dict(await self.get(f"/agent/papers/{paper_id}/formulas", params or None) or {})

    async def kb_search(self, query: str, *, top_k: int) -> dict:
        """**跨库**检索（``kb_search`` 的后端）：不限论文，返回里带 paper_id 与标题。"""
        return dict(await self.post("/agent/kb/search",
                                    {"query": query, "top_k": top_k}) or {})

    async def library(self, *, q: str | None = None, status: str | None = None) -> dict:
        params = {k: v for k, v in (("q", q), ("status", status)) if v}
        return dict(await self.get("/agent/library", params or None) or {})

    async def notes(self, *, query: str | None = None, paper_id: str | None = None,
                    kind: str = "all") -> dict:
        params = {k: v for k, v in (("query", query), ("paper_id", paper_id),
                                    ("kind", kind)) if v}
        return dict(await self.get("/agent/notes", params or None) or {})

    async def search(self, paper_id: str, query: str, *, top_k: int,
                     chunk_types: list[str] | None = None) -> dict:
        """**本论文内**检索（宿主固定 ``scope=paper``、``mode=qa``，见 §7.1）。"""
        return dict(await self.post(f"/agent/papers/{paper_id}/search", {
            "query": query, "top_k": top_k, "chunk_types": chunk_types,
        }) or {})

    async def figures(self, paper_id: str, page: int | None = None) -> dict:
        return dict(await self.get(f"/agent/papers/{paper_id}/figures",
                                   {"page": page} if page else None) or {})

    async def block(self, paper_id: str, block_id: str) -> dict:
        return dict(await self.get(f"/papers/{paper_id}/blocks/{block_id}") or {})

    async def row(self, paper_id: str, page: int, row_id: str) -> dict:
        """页级产物（转录/翻译）里的**一行**（`block_get` 展开转录段落用）。

        行 id 就是阅读器正文的 `data-row-id`（``p009-00`` 这种位置编号）——
        所以"按 ref 展开"与"引用跳转"落在同一行上。
        """
        return dict(await self.get(f"/agent/papers/{paper_id}/rows/{int(page)}/{row_id}") or {})

    # ---------------- 高亮 / 笔记（《修改计划》第六步） ----------------
    async def highlights(self, paper_id: str, *, highlight_id: str = "", note_id: str = "",
                         query: str = "") -> dict:
        """高亮列表；给了 `highlight_id` / `note_id` 就是**定位信息**（挂在哪一面、第几页）。"""
        params = {k: v for k, v in (("highlight_id", highlight_id), ("note_id", note_id),
                                    ("query", query)) if v}
        return dict(await self.get(f"/agent/papers/{paper_id}/highlights", params or None) or {})

    async def highlight_create(self, paper_id: str, payload: dict) -> dict:
        """按**文字**建高亮。拒绝也是正常返回（``ok=false`` + 最近的原文）。"""
        return dict(await self.post(f"/agent/papers/{paper_id}/highlights", payload or {}) or {})

    async def highlight_update(self, highlight_id: str, payload: dict) -> dict:
        return dict(await self.put(f"/agent/highlights/{highlight_id}", payload) or {})

    async def highlight_delete(self, highlight_id: str) -> dict:
        """删高亮（连带它挂着的笔记）。"""
        return dict(await self.delete(f"/agent/highlights/{highlight_id}") or {})

    async def notes(self, paper_id: str, *, note_id: str = "", query: str = "") -> dict:
        params = {k: v for k, v in (("note_id", note_id), ("query", query)) if v}
        return dict(await self.get(f"/agent/papers/{paper_id}/notes", params or None) or {})

    async def note_create(self, paper_id: str, payload: dict) -> dict:
        return dict(await self.post(f"/agent/papers/{paper_id}/notes", payload or {}) or {})

    async def note_update(self, note_id: str, payload: dict) -> dict:
        return dict(await self.put(f"/agent/notes/{note_id}", payload) or {})

    async def note_delete(self, note_id: str) -> dict:
        return dict(await self.delete(f"/agent/notes/{note_id}") or {})

    # ---------------- 写：论文元数据 ----------------    async def meta_write(self, paper_id: str, payload: dict) -> dict:
        """写论文元数据（`paper_meta_write` 工具的后端）。

        与页产物纠错同一口径：**拒绝也是正常返回**（``ok=false`` + 原因），不抛 HostError ——
        "值不合法"要让模型看到并改，而不是让整轮崩掉。
        """
        return dict(await self.post(f"/agent/papers/{paper_id}/metadata", payload or {}) or {})

    # ---------------- 写：页级产物纠错 ----------------
    async def page_product(self, paper_id: str, page: int, side: str) -> dict:
        """页级产物（A 路转录 / B 路译文）的**原文视图**（`page_markdown_edit` 的 view）。"""
        return dict(await self.get(f"/agent/papers/{paper_id}/pages/{int(page)}/product",
                                   {"side": side}) or {})

    async def page_edit(self, paper_id: str, page: int, side: str, find: str,
                        replace: str, *, reason: str = "", reindex: bool = True) -> dict:
        """改一处产物文本。**拒绝也是正常返回**（``ok=false`` + 下一步提示），不抛 HostError。"""
        return dict(await self.post(f"/agent/papers/{paper_id}/pages/{int(page)}/product", {
            "side": side, "find": find, "replace": replace,
            "reason": reason, "reindex": bool(reindex),
        }) or {})

    async def page_undo(self, paper_id: str, page: int, side: str, *,
                        reindex: bool = True) -> dict:
        """撤回这一页最后一次编辑。"""
        return dict(await self.post(
            f"/agent/papers/{paper_id}/pages/{int(page)}/product/undo",
            {"side": side, "reindex": bool(reindex)}) or {})

    async def page_image(self, paper_id: str, page_no: int, dpi: int
                         ) -> tuple[bytes, str]:
        """页面图。**dpi 一定要传**：阅读器那张是 150dpi，公式上下标常常看不清；
        而放大低 dpi 的图只是插值，信息量不会增加。"""
        return await self.get_bytes(
            f"/agent/papers/{paper_id}/pages/{page_no}/image", {"dpi": dpi})

    async def figure_image(self, paper_id: str, page_no: int, index: int,
                           *, card: bool = False) -> tuple[bytes, str]:
        params = {"card": 1} if card else None
        return await self.get_bytes(
            f"/papers/{paper_id}/figures/region/{page_no}/{index}", params)

    async def attachment(self, attachment_id: str) -> tuple[bytes, str]:
        """用户在对话里上传的图片（多模态直接喂给问答模型，见 `runtime._user_prompt`）。"""
        return await self.get_bytes(f"/agent/attachments/{attachment_id}")

    async def persist_message(self, payload: dict) -> dict:
        """把这一轮的助手消息落库（**在发 `done` 之前**，见 §11 硬规则②）。"""
        return dict(await self.post("/agent/internal/messages", payload) or {})

    async def ready(self) -> dict:
        """主后端是否可达（agent 的 `/readyz` 会带上这一项）。"""
        try:
            resp = await self._ensure().get("/agent/ping")
            return {"ok": resp.status_code < 400, "status": resp.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
