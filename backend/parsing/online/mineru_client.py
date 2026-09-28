"""MinerU 精准解析 API v4 的 HTTP 客户端（提交 → 轮询 → 下载 → 安全解包）。

**只依赖 httpx 与标准库，不碰数据库、不碰服务层**（与 ``parsing.pipeline`` 同一条
约束）：CLI、任务处理器、单测共用同一份实现，也才可能用假传输层离线跑通。

接口口径（一手来源：https://mineru.net/doc/docs/index.html 、
https://mineru.net/apiManage/docs ，并对照官方 SDK opendatalab/MinerU-Ecosystem）：

1. ``POST {base}/file-urls/batch`` 申请签名上传链接 → ``data.batch_id`` +
   ``data.file_urls[]``（与请求里的 ``files`` 顺序一一对应）；
2. ``PUT <签名 URL>`` 上传本地 PDF —— **文档明确「无须设置 Content-Type」**，
   上传完成后系统自动扫描并提交任务，不需要再调一次提交接口；
3. ``GET {base}/extract-results/batch/{batch_id}`` 轮询 → ``data.extract_result[]``，
   每项 ``state`` ∈ pending/running/converting/uploading/waiting-file/done/failed；
4. ``GET <full_zip_url>`` 下载结果 zip（CDN 直链，**不需要鉴权**），再安全解包。

三层防护都按"远程返回的东西不可信"来写：响应外层统一 ``code/msg/data`` 包装
（成功码是整数 ``0``），zip 一律走 :func:`safe_extract_zip`（拒绝绝对路径、``..``、
符号链接，并对条目数/总解压体积设上限）。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Protocol

from backend.core.errors import ParseError
from backend.core.logging import get_logger

log = get_logger(__name__)

#: 官方 API 根（``/api/v4`` 已含在这里，调用点不要再拼一遍）
DEFAULT_BASE_URL = "https://mineru.net/api/v4"
#: 上传链接申请、单个任务查询这类"短请求"的超时
DEFAULT_TIMEOUT = 60.0
#: 上传本地 PDF 的超时：200MB 上限，国内链路按 15 分钟留够
DEFAULT_UPLOAD_TIMEOUT = 900.0
#: 结果 zip 下载超时
DEFAULT_DOWNLOAD_TIMEOUT = 600.0
#: 轮询：起步 2s，退避到 30s 为止；整篇总时长上限 30 分钟（官方 SDK 对批量取 1800s）
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_POLL_MAX_INTERVAL = 30.0
DEFAULT_POLL_DEADLINE = 1800.0
#: 结果 zip 解包上限（声明条目数 / 解压后总体积），防止"恶意 zip 撑爆磁盘"
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

#: 服务端错误码 → 一句**可执行**的中文提示。
#:
#: 口径：只写给用户"接下来该做什么"，不复述服务端英文原文（原文进日志与 detail）。
#: ``A02xx`` 是网关鉴权层（响应形态是 ``{"success":false,"msgCode":"A0202",...}``，
#: 与业务层的 ``{"code":0,...}`` 不是一套），所以两套都要认。
ERROR_HINTS: dict[str, str] = {
    "A0202": "MinerU token 无效（A0202）：请到 https://mineru.net/apiManage/token 复制一个正确的 token",
    "A0211": "MinerU token 已过期（A0211）：token 有效期约 90 天且没有续期接口，请重新生成一个",
    "-500": "MinerU 参数错误（-500）：请求体里有服务端不认识的字段",
    "-10001": "MinerU 服务内部错误（-10001）：稍后重试",
    "-10002": "MinerU 参数错误（-10002）",
    "-60002": "文件格式不支持（-60002）：只支持 PDF / 图片 / Office 文档",
    "-60003": "源文件读取失败（-60003）",
    "-60004": "文件为空（-60004）",
    "-60005": "文件超过 200MB（-60005）",
    "-60006": "页数超过上限（-60006）",
    "-60008": "源文件读取超时（-60008）：MinerU 拉不到这个 URL",
    "-60010": "MinerU 解析失败（-60010）",
    "-60012": "任务不存在（-60012）",
    "-60013": "无权访问该任务（-60013）",
    "-60015": "文件转换失败（-60015）",
    "-60016": "格式转换失败（-60016）",
    "-60018": "今日最高优先级额度已用完（-60018）：超出部分会降优先级，可稍后再试",
    "-60019": "配额不足（-60019）",
    "-30001": "文件超过 Agent 轻量接口的 10MB 上限（-30001）",
    "-30002": "Agent 轻量接口不支持该文件类型（-30002）",
}


class MineruError(ParseError):
    """MinerU 在线解析失败（任务处理器据此把任务标成失败并给出人话原因）。"""

    code = "MINERU_ERROR"


class MineruAuthError(MineruError):
    """token 无效 / 已过期。单独一类是因为它的处置动作完全不同：重新申请 token。"""

    code = "MINERU_AUTH"
    http_status = 401


class MineruQuotaError(MineruError):
    code = "MINERU_QUOTA"
    http_status = 429


class MineruTimeoutError(MineruError):
    code = "MINERU_TIMEOUT"
    http_status = 504


class MineruCancelled(MineruError):
    """用户取消（或暂停）了任务：服务端的解析**不会**被撤销，只是我们不再等它。"""

    code = "MINERU_CANCELLED"


def mask_token(token: str | None) -> str | None:
    """token 掩码（前 4 + 后 4）。日志与接口回显都只能看到这个形态。"""
    t = (token or "").strip()
    if not t:
        return None
    if len(t) <= 12:
        return t[:2] + "…" + t[-2:]
    return f"{t[:4]}…{t[-4:]}"


def hint_for(code: Any, msg: str = "") -> str:
    """错误码 → 人话；没有对应条目时把服务端原文带上，绝不吞掉。"""
    text = ERROR_HINTS.get(str(code))
    if text:
        return f"{text}（服务端：{msg}）" if msg else text
    return f"MinerU 返回错误（code={code}）：{msg or '服务端未给出原因'}"


def is_auth_code(code: Any) -> bool:
    return str(code) in ("A0202", "A0211")


# --------------------------------------------------------------------------
# 传输层：默认 httpx，测试注入假实现
# --------------------------------------------------------------------------


class HttpTransport(Protocol):
    """客户端用到的 4 个动作。窄接口是为了让单测注入一个几十行的假传输层。"""

    def post_json(self, url: str, body: dict, headers: dict,
                  timeout: float) -> tuple[int, dict]: ...

    def get_json(self, url: str, headers: dict,
                 timeout: float) -> tuple[int, dict]: ...

    def put_file(self, url: str, path: Path, headers: dict,
                 timeout: float) -> int: ...

    def download(self, url: str, dest: Path, timeout: float) -> int: ...


class HttpxTransport:
    """默认传输层（httpx 已在 requirements.txt 里，不引新依赖）。"""

    def __init__(self, *, max_zip_bytes: int = MAX_ZIP_TOTAL_BYTES) -> None:
        self.max_zip_bytes = int(max_zip_bytes)

    def _client(self, timeout: float):
        import httpx

        return httpx.Client(timeout=timeout, follow_redirects=True)

    @staticmethod
    def _json_or_empty(resp) -> dict:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 —— 非 JSON 响应（网关 html/空体）不该炸在解析上
            return {}
        return data if isinstance(data, dict) else {}

    def post_json(self, url, body, headers, timeout):
        with self._client(timeout) as c:
            r = c.post(url, json=body, headers=headers)
            return r.status_code, self._json_or_empty(r)

    def get_json(self, url, headers, timeout):
        with self._client(timeout) as c:
            r = c.get(url, headers=headers)
            return r.status_code, self._json_or_empty(r)

    def put_file(self, url, path, headers, timeout):
        # 流式上传：200MB 的 PDF 读进内存再发会白白多占一份峰值内存
        with self._client(timeout) as c, open(path, "rb") as fh:
            r = c.put(url, content=fh, headers=headers)
            return r.status_code

    def download(self, url, dest, timeout):
        written = 0
        with self._client(timeout) as c, c.stream("GET", url) as r:
            if r.status_code != 200:
                raise MineruError(f"下载 MinerU 结果失败：HTTP {r.status_code}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as out:
                for chunk in r.iter_bytes(1024 * 256):
                    written += len(chunk)
                    if written > self.max_zip_bytes:
                        # 先中断再删半成品：留下半个 zip 只会让后面的报错更难懂
                        out.close()
                        dest.unlink(missing_ok=True)
                        raise MineruError(
                            f"MinerU 结果包超过 {self.max_zip_bytes // 1048576}MB 上限，已中断")
                    out.write(chunk)
        return written


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class MineruClient:
    """一次"论文 → MinerU 结果包"的完整流程。

    无状态（除了配置），可以按任务构造；所有等待都走 :meth:`_sleep`，
    单测替换 ``sleep_fn`` 即可做到"零等待"跑完轮询逻辑。
    """

    def __init__(
        self,
        token: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: HttpTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        upload_timeout: float = DEFAULT_UPLOAD_TIMEOUT,
        download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        poll_max_interval: float = DEFAULT_POLL_MAX_INTERVAL,
        poll_deadline: float = DEFAULT_POLL_DEADLINE,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.token = (token or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.transport = transport or HttpxTransport()
        self.timeout = float(timeout)
        self.upload_timeout = float(upload_timeout)
        self.download_timeout = float(download_timeout)
        self.poll_interval = max(0.2, float(poll_interval))
        self.poll_max_interval = max(self.poll_interval, float(poll_max_interval))
        # 下限 1s：配置里写 0 会退化成"发请求不等待"的死循环；测试用 `clock_fn` 假时钟
        # 把"超时"这条路径跑成零等待，而不是靠把这个下限调小
        self.poll_deadline = max(1.0, float(poll_deadline))
        self._sleep = sleep_fn
        self._clock = clock_fn

    # ---------------- 基础 ----------------
    @property
    def token_masked(self) -> str | None:
        return mask_token(self.token)

    def _headers(self) -> dict:
        if not self.token:
            raise MineruAuthError(
                "还没有配置 MinerU token：请到「设置 → 在线解析（MinerU）」填入并验证")
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}"}

    def _raise_for_business(self, status: int, payload: dict, what: str) -> None:
        """把三套失败形态（HTTP 状态 / 网关 ``success:false`` / 业务 ``code``）收成一种异常。

        ⚠️ **必须先看网关那一套**：``A0202``/``A0211`` 走的是
        ``{"success": false, "msgCode": "A0202", "msg": "user authenticate failed"}``，
        与业务层的 ``code`` 不是同一个字段；只看 ``code`` 会把"token 过期"当成成功。
        """
        if isinstance(payload, dict) and payload.get("success") is False:
            code = payload.get("msgCode") or payload.get("msg_code") or ""
            msg = str(payload.get("msg") or "")
            if is_auth_code(code) or status in (401, 403):
                raise MineruAuthError(hint_for(code or status, msg))
            raise MineruError(f"{what}失败：{hint_for(code or status, msg)}")
        if status in (401, 403):
            raise MineruAuthError(
                f"{what}失败：HTTP {status}（{hint_for('A0202')}）")
        if status == 404:
            raise MineruError(f"{what}失败：HTTP 404 —— 接口地址不对（base_url={self.base_url}）")
        if status != 200:
            raise MineruError(f"{what}失败：HTTP {status}")
        code = payload.get("code") if isinstance(payload, dict) else None
        if code in (0, "0", None):
            return
        msg = str(payload.get("msg") or "")
        if is_auth_code(code):
            raise MineruAuthError(hint_for(code, msg))
        if str(code) in ("-60018", "-60019"):
            raise MineruQuotaError(hint_for(code, msg))
        raise MineruError(f"{what}失败：{hint_for(code, msg)}")

    def verify(self) -> dict:
        """校验 token 是否可用 —— **不创建任务、不消耗配额**。

        做法：拿一个随机 uuid 去查一个必然不存在的任务。判据是"鉴权有没有通过"，
        而不是"任务存不存在"：

        - HTTP 401/403 或 ``A0202``/``A0211`` → token 无效 / 已过期；
        - 只要服务端返回了**业务码**（``code == 0`` 或任何负数，例如
          ``-60012`` 任务不存在），就说明请求已经穿过鉴权层 —— 鉴权通过。
          这条判据比"必须返回 -60012"稳：官方错误码表里任务查询这一路会返回
          ``-60012``，但具体码值随场景会变，而"能不能拿到业务码"是恒定信号。
        """
        probe = str(uuid.uuid4())
        t0 = time.perf_counter()
        result: dict[str, Any] = {
            "ok": False, "state": "unknown", "http_status": None,
            "code": None, "server_msg": "", "token_masked": self.token_masked,
            "probe": probe,
        }
        if not self.token:
            result.update(state="missing",
                          message="还没配置 MinerU token（设置 → 在线解析）")
            return result
        url = f"{self.base_url}/extract/task/{probe}"
        try:
            status, payload = self.transport.get_json(url, self._headers(), self.timeout)
        except MineruAuthError as exc:
            result.update(state="invalid", message=exc.message,
                          latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            return result
        except Exception as exc:  # noqa: BLE001 —— 网络层的任何异常都归"连不上"
            result.update(state="network",
                          message=f"连不上 MinerU（{type(exc).__name__}：{str(exc)[:160]}）",
                          latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            return result

        code = payload.get("code") if isinstance(payload, dict) else None
        msg = (str(payload.get("msg") or "") if isinstance(payload, dict) else "")
        result.update(http_status=status, code=code, server_msg=msg,
                      latency_ms=round((time.perf_counter() - t0) * 1000, 1))

        # ---- 鉴权层：HTTP 401/403、或网关那套 ``success:false`` ----
        if status in (401, 403):
            result.update(state="invalid", message=hint_for("A0202", msg))
            return result
        if isinstance(payload, dict) and payload.get("success") is False:
            gateway_code = (payload.get("msgCode") or payload.get("msg_code")
                            or code or "")
            if not gateway_code:
                result.update(state="invalid",
                              message=f"MinerU 鉴权失败：{msg or '服务端未给出原因'}")
                return result
            result.update(state="expired" if str(gateway_code) == "A0211" else "invalid",
                          message=hint_for(gateway_code, msg))
            return result

        # ---- HTTP 层：地址对不对 ----
        if status == 404:
            result.update(state="error",
                          message=f"接口地址不对（HTTP 404）：请确认 {self.base_url}")
            return result
        if status != 200:
            result.update(state="error", message=f"MinerU 返回 HTTP {status}：{msg or '无正文'}")
            return result

        # ---- 业务层：能拿到业务码 ⟹ 已经穿过鉴权 ----
        #
        # 判据是"能不能拿到业务码"而不是"业务码等于多少"：官方错误码表里任务查询这一路
        # 常见的是 -60012（任务不存在），但具体码值随场景会变（服务端也可能直接返回
        # code=0），而"请求被业务层受理了"是恒定信号 —— 鉴权没过的话根本到不了这里。
        if code is None:
            result.update(state="error",
                          message=f"MinerU 返回了无法识别的响应（HTTP {status}，没有 code 字段）："
                                  f"请确认接口地址是 {self.base_url}")
            return result
        note = {0: "服务端接受了请求",
                -60012: "任务不存在（探测用的随机 id，正常）"}.get(code, f"服务端返回 code={code}")
        result.update(ok=True, state="valid",
                      message=f"Token 有效（{result['latency_ms']:.0f}ms，{note}）")
        return result

    # ---------------- 主流程 ----------------
    def parse_pdf(
        self,
        pdf_path: str | Path,
        dest_dir: str | Path,
        *,
        name: str | None = None,
        data_id: str | None = None,
        model_version: str = "pipeline",
        language: str = "en",
        enable_formula: bool = True,
        enable_table: bool = True,
        is_ocr: bool = False,
        page_ranges: str | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict:
        """跑完整条流程，返回解包后的产物索引（见 :func:`locate_bundle`）。

        ``on_progress(stage, done, total)``：与本地解析同一套进度契约，
        任务处理器据此把 ``extracted_pages/total_pages`` 透到任务面板。
        ``should_stop()``：暂停/取消检查点（每轮询一次），返回 True 立刻放弃等待。
        """
        pdf = Path(pdf_path)
        if not pdf.is_file():
            raise MineruError(f"PDF 不存在：{pdf}")
        upload_name = (name or pdf.name).strip() or "paper.pdf"
        raw_dir = Path(dest_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)

        self._emit(on_progress, "mineru_apply", 0, 3)
        batch_id, upload_url = self._apply_upload(
            upload_name, model_version=model_version, language=language,
            enable_formula=enable_formula, enable_table=enable_table,
            is_ocr=is_ocr, page_ranges=page_ranges, data_id=data_id)
        log.info("MinerU 上传链接已申请：batch=%s file=%s", batch_id, upload_name)

        self._emit(on_progress, "mineru_upload", 1, 3)
        status = self.transport.put_file(
            upload_url, pdf, {"Content-Length": str(pdf.stat().st_size)},
            self.upload_timeout)
        # 官方文档：「上传文件时无须设置 Content-Type」。实测一些 OSS 反而会因
        # 带上 Content-Type 而签名不匹配，所以这里**不带**任何多余请求头。
        if status not in (200, 201, 204):
            raise MineruError(f"上传 PDF 到 MinerU 失败：HTTP {status}")

        final = self._poll_batch(batch_id, upload_name, on_progress, should_stop)
        zip_url = str(final.get("full_zip_url") or "")
        if not zip_url:
            raise MineruError(f"MinerU 已标记完成但没有给出结果包地址：{final}")

        self._emit(on_progress, "mineru_download", 2, 3)
        zip_path = raw_dir / "mineru_result.zip"
        size = self.transport.download(zip_url, zip_path, self.download_timeout)
        extracted = safe_extract_zip(zip_path, raw_dir)
        self._emit(on_progress, "mineru_download", 3, 3)

        bundle = locate_bundle(raw_dir)
        if bundle["content_list"] is None:
            raise MineruError(
                "MinerU 结果包里没有找到 content_list.json（无法还原版面结构）："
                f"{sorted(p.name for p in raw_dir.iterdir())[:20]}")
        bundle.update(batch_id=batch_id, zip_path=str(zip_path),
                      zip_bytes=size, extracted=extracted,
                      raw_dir=str(raw_dir), result=final,
                      model_version=model_version, language=language)
        log.info("MinerU 解析完成：batch=%s 解包 %s 个文件，content_list=%s",
                 batch_id, extracted, bundle["content_list"])
        return bundle

    # ---------------- 步骤 ----------------
    def _apply_upload(self, name: str, *, model_version: str, language: str,
                      enable_formula: bool, enable_table: bool, is_ocr: bool,
                      page_ranges: str | None, data_id: str | None) -> tuple[str, str]:
        """申请签名上传链接；返回 ``(batch_id, 第一个上传 URL)``。

        单文件批量接口（``/file-urls/batch``）比"传公网 URL"更可靠：官方文档明确
        警告 GitHub / AWS 这类境外 URL 会被拉取超时，而本系统的 PDF 只在本机。
        """
        files: dict[str, Any] = {"name": name}
        if data_id:
            files["data_id"] = data_id[:128]
        if is_ocr:
            files["is_ocr"] = True
        if page_ranges:
            files["page_ranges"] = page_ranges
        body = {
            "files": [files],
            "model_version": model_version,
            "language": language,
            "enable_formula": bool(enable_formula),
            "enable_table": bool(enable_table),
        }
        status, payload = self.transport.post_json(
            f"{self.base_url}/file-urls/batch", body, self._headers(), self.timeout)
        self._raise_for_business(status, payload, "申请上传链接")
        data = (payload or {}).get("data") or {}
        batch_id = str(data.get("batch_id") or "")
        urls = data.get("file_urls") or []
        upload_url = ""
        if urls:
            first = urls[0]
            upload_url = str(first.get("url") if isinstance(first, dict)
                             else first or "")
        if not batch_id or not upload_url:
            raise MineruError(f"申请上传链接返回不完整（缺少 batch_id/file_urls）：{payload}")
        return batch_id, upload_url

    def _poll_batch(self, batch_id: str, upload_name: str,
                    on_progress: Callable[[str, int, int], None] | None,
                    should_stop: Callable[[], bool] | None) -> dict:
        """轮询批次结果，返回我们上传的那个文件的 ``extract_result`` 项。

        退避：2s 起、每次 ×1.5、封顶 30s —— 论文解析通常 30s~3min，固定 2s 会把
        查询类配额（官方限流页：1000 次/分钟）用在无意义的空转上。
        """
        from urllib.parse import quote

        url = f"{self.base_url}/extract-results/batch/{quote(batch_id, safe='')}"
        started = self._clock()
        interval = self.poll_interval
        last_state = ""
        while True:
            if should_stop is not None and should_stop():
                raise MineruCancelled(
                    f"已放弃等待 MinerU 结果（batch={batch_id}）："
                    f"服务端任务不会因此撤销，可以稍后重新解析")
            status, payload = self.transport.get_json(url, self._headers(), self.timeout)
            self._raise_for_business(status, payload, "查询解析进度")
            results = ((payload or {}).get("data") or {}).get("extract_result") or []
            if isinstance(results, dict):
                results = [results]
            item = _pick_result(results, upload_name)
            if item is not None:
                state = str(item.get("state") or "").lower()
                if state != last_state:
                    log.info("MinerU batch=%s 状态：%s", batch_id, state or "(空)")
                    last_state = state
                if state == "done":
                    return item
                if state == "failed":
                    raise MineruError(
                        "MinerU 解析失败："
                        + hint_for("-60010", str(item.get("err_msg") or "服务端未给出原因")))
                prog = item.get("extract_progress") or {}
                done = int(prog.get("extracted_pages") or 0)
                total = int(prog.get("total_pages") or 0)
                self._emit(on_progress, "mineru_poll", done, total or done or 1)
            if self._clock() - started > self.poll_deadline:
                raise MineruTimeoutError(
                    f"等待 MinerU 超过 {self.poll_deadline / 60:.0f} 分钟仍未完成"
                    f"（batch={batch_id}，最后状态 {last_state or '未知'}）")
            self._sleep(interval)
            interval = min(interval * 1.5, self.poll_max_interval)

    @staticmethod
    def _emit(on_progress: Callable[[str, int, int], None] | None,
              stage: str, done: int, total: int) -> None:
        if on_progress is None:
            return
        try:
            on_progress(stage, done, total)
        except Exception:  # noqa: BLE001 —— 进度上报失败绝不能影响解析
            log.debug("MinerU 进度回调失败", exc_info=True)


# --------------------------------------------------------------------------
# 结果包
# --------------------------------------------------------------------------


def _pick_result(results: list, upload_name: str) -> dict | None:
    """挑出我们上传的那个文件的结果项。

    单文件批次也**不要**直接取第一项：批量接口是按请求顺序返回的，一旦将来改成
    多文件，按名字匹配才是对的（官方 SDK 同样先按 ``file_name`` 匹配、匹配不到才退回第一项）。
    """
    items = [r for r in results if isinstance(r, dict)]
    if not items:
        return None
    stem = Path(upload_name).stem
    for it in items:
        got = Path(str(it.get("file_name") or it.get("name") or "")).stem
        if got and got == stem:
            return it
    return items[0]


def locate_bundle(raw_dir: str | Path) -> dict:
    """在解包目录里定位产物（**按后缀匹配**，不硬编码文件名）。

    实测 zip 里的文件名带任务 uuid 前缀（``<uuid>_content_list.json``、
    ``<uuid>_middle.json``、``<uuid>_model.json``），官方文档给的
    ``content_list.json`` 只是"对应关系"说明。所以一律用后缀找。

    只用 ``full.md`` + ``*_content_list.json`` + ``images/`` 当**稳定契约**：
    ``middle.json`` 在引擎 4.0 换了 schema（``docvortex.middle/2.0``），在线 API
    侧到底是哪套文档没说，所以它只作为可选增强，找到就用、找不到不影响出图。
    """
    root = Path(raw_dir)

    def _find(names: tuple[str, ...], suffixes: tuple[str, ...] = ()) -> Path | None:
        """按"精确文件名优先、后缀兜底"找，浅的目录优先（解包后常见嵌套一层）。"""
        found = [p for p in root.rglob("*") if p.is_file()
                 and (p.name in names or (suffixes and p.name.endswith(suffixes)))]
        if not found:
            return None
        exact = [p for p in found if p.name in names]
        pool = exact or found
        pool.sort(key=lambda p: (len(p.relative_to(root).parts), p.name))
        return pool[0]

    # v1 是主源（字段形态见 mineru_builder）；v2（按页分组）只有部分版本产出，
    # 当且仅当没有 v1 时兜底 —— builder 两种都认。
    content_list = (_find(("content_list.json",), ("_content_list.json",))
                    or _find(("content_list_v2.json",), ("_content_list_v2.json",)))
    images = next((p for p in sorted(root.rglob("images")) if p.is_dir()), None)
    return {
        "raw_dir": str(root),
        "content_list": content_list,
        "full_md": _find(("full.md", "markdown.md"), (".md",)),
        "middle_json": _find(("middle.json", "layout.json"), ("_middle.json",)),
        "model_json": _find(("model.json",), ("_model.json",)),
        "images_dir": images,
    }


def safe_extract_zip(zip_path: str | Path, dest_dir: str | Path, *,
                     max_entries: int = MAX_ZIP_ENTRIES,
                     max_total_bytes: int = MAX_ZIP_TOTAL_BYTES) -> int:
    """解包远程 zip：拒绝绝对路径 / ``..`` / 符号链接，并限制条目数与解压总量。

    为什么不能直接 ``ZipFile.extractall``：zip 来自外部服务，``extractall`` 在
    Python 里虽然已经会清洗绝对路径与 ``..``，但它不拦符号链接、也不限制体积 ——
    一个声明了 10GB 的包会把磁盘写满，而这类事故发生在"解析"这条最不该出事的路上。
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    count = 0
    total = 0
    with zipfile.ZipFile(str(zip_path)) as zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise MineruError(f"结果包条目过多（{len(infos)} > {max_entries}），拒绝解包")
        for info in infos:
            name = info.filename.replace("\\", "/")
            if info.is_dir():
                continue
            # 符号链接：external_attr 高 16 位是 Unix 模式，0o120000 即 S_IFLNK
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise MineruError(f"结果包里含符号链接（{name}），拒绝解包")
            if name.startswith("/") or name.startswith("//") or ".." in name.split("/"):
                raise MineruError(f"结果包含不安全路径（{name}），拒绝解包")
            target = (base / name).resolve()
            if target != base and base not in target.parents:
                raise MineruError(f"结果包路径越界（{name}），拒绝解包")
            total += int(info.file_size or 0)
            if total > max_total_bytes:
                raise MineruError(
                    f"结果包解压后超过 {max_total_bytes // (1024 ** 3)}GB，拒绝解包")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
            count += 1
    return count


def sha256_file(path: str | Path) -> str:
    """结果包指纹（记进解析统计，便于排查"两次解析结果不一样"）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "DEFAULT_BASE_URL", "ERROR_HINTS", "HttpTransport", "HttpxTransport",
    "MineruAuthError", "MineruCancelled", "MineruClient", "MineruError",
    "MineruQuotaError", "MineruTimeoutError", "hint_for", "is_auth_code",
    "load_json", "locate_bundle", "mask_token", "safe_extract_zip", "sha256_file",
]
