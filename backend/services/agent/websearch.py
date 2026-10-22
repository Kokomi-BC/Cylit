"""DeepSeek 官方 Search / Fetch 的配置与调用（工具 `web_search` / `web_fetch` 的后端）。

为什么能力在主后端而不是 agent 进程：agent 进程**不持任何密钥**（见 `agent/config.py` 顶部），
它要联网只能经 `/agent/websearch/*` 这条内部路由 —— 密钥、错误映射、参数边界都留在这一侧，
"agent 能访问什么"仍然只有一个审批点。

密钥照 MinerU 那套（`api/mineru.py` + `mineru_service`）：落 `data/secrets/websearch.key`，
不落库、不进代码、不进日志，接口只回掩码。四个非密钥参数（provider / count /
need_content / fetch_chars）存 setting 表，**写入点只有 `save_config`、读取点只有
`effective()`** —— 两处各写一套默认值就会出现"界面改了、行为没变"（本项目为这个问题
吃过一次亏，见 `mineru_service` 模块头）。

接口口径（一手来源：https://api-docs.deepseek.com/zh-cn/guides/search ）：
Search 返回 `{"results":[{rerank_score,title,url,snippet,summary,content,site_name,date,authority}]}`，
条数 1~50（`count` 默认 10、只影响条数不影响计费，本系统默认 8）；Fetch 返回
`{"title","content"}`，`content` 是 Markdown 正文、**没有长度参数**，要截断只能我们自己做。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from backend.core.config import get_settings
from backend.core.errors import PaperMindError, ValidationError
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, json_loads
from backend.parsing.online.mineru_client import mask_token
from backend.repositories.mysql.models import Setting
from backend.repositories.mysql.session import session_scope

log = get_logger(__name__)

#: 提供方注册表。**只放 deepseek**：别家没有同一套 search/fetch 语义，
#: 加一家就要加一份错误映射与参数归一化，先不预留（YAGNI）。
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek 搜索",
        "search_url": "https://api.deepseek.com/v1/tools/search",
        "fetch_url": "https://api.deepseek.com/v1/tools/fetch",
    },
}

DEFAULT_PROVIDER = "deepseek"
DEFAULT_COUNT = 8
DEFAULT_NEED_CONTENT = False
DEFAULT_FETCH_CHARS = 6000

#: 官方口径 `count` 1~50、`query` 1~128 字符；超了服务端只会回一句
#: "'count' must be between 1 and 50"，不如在这里说清正确取值。
COUNT_RANGE = (1, 50)
QUERY_MAX_CHARS = 128
#: 正文截断上限的合理区间：太小等于没读，太大会把上下文挤爆。
#:
#: 上界给到 200k 字符，是因为 `web_fetch` 是**主动**读某一页（用户给了链接、或搜到的那一条
#: 就是要的依据），这种读法的上限该由**上下文预算**决定，而不是拍一个 2 万字的数字
#: （用户用 20000 读官方文档时就撞上了）。200k 字符 ≈ 50k tokens，仍在
#: `context_budget`（默认 1e6）之内；真超了也不报错，靠 `truncated` 标记告诉模型
#: "后面还有"，要不要分段读由它决定。
FETCH_CHARS_RANGE = (500, 200000)

SEARCH_TIMEOUT = 20.0
#: Fetch 要等目标站点，比 Search 慢；官方文档也把 504 列成"下游请求超时"
FETCH_TIMEOUT = 45.0

#: 密钥：文件优先、环境变量兜底（CI / 临时验证时不该把长期凭据落到工作区）
KEY_FILE_NAME = "websearch.key"
KEY_ENV = "PR_DEEPSEEK_SEARCH_KEY"

#: 本服务**独占**写入的设置键（见模块头：只允许 `save_config` 一个写入点）
SETTING_KEYS = ("websearch_provider", "websearch_count",
                "websearch_need_content", "websearch_fetch_chars")


class WebSearchError(PaperMindError):
    """联网搜索 / 抓取失败（消息本身就是给人和模型看的一句话）。"""

    code = "WEBSEARCH_ERROR"
    http_status = 502


class WebSearchAuthError(WebSearchError):
    """密钥缺失 / 无效 / 已过期。单独一类：处置动作是"去换一个密钥"。"""

    code = "WEBSEARCH_AUTH"
    http_status = 401


class WebSearchQuotaError(WebSearchError):
    """频率或额度不够（HTTP 402 / 429）。"""

    code = "WEBSEARCH_QUOTA"
    http_status = 429


class WebSearchTimeoutError(WebSearchError):
    code = "WEBSEARCH_TIMEOUT"
    http_status = 504


# --------------------------------------------------------------------------
# 配置读写（照 `mineru_service` 的两个口：测试把 `_stored`/`_write_setting` 换掉即可离线）
# --------------------------------------------------------------------------


def _stored() -> dict:
    with session_scope() as sess:
        return {r.key: json_loads(r.value, r.value) for r in sess.query(Setting).all()}


def _write_setting(key: str, value: Any) -> None:
    with session_scope() as sess:
        row = sess.get(Setting, key)
        if row is None:
            row = Setting(key=key)
            sess.add(row)
        row.value = json_dumps(value)


def key_file() -> Path:
    return get_settings().secrets_dir / KEY_FILE_NAME


def read_key() -> str | None:
    """密钥（文件优先，其次 `PR_DEEPSEEK_SEARCH_KEY`）。**只给内部用，永不出接口。**"""
    path = key_file()
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    env = (os.environ.get(KEY_ENV) or "").strip()
    return env or None


def key_source() -> str | None:
    """密钥从哪来：`file`（界面里保存的）/ `env`（环境变量）/ None（还没配）。"""
    path = key_file()
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return "file"
    return "env" if (os.environ.get(KEY_ENV) or "").strip() else None


def write_key(value: str) -> Path:
    text = (value or "").strip()
    if not text:
        raise ValidationError("密钥不能为空")
    if any(ch.isspace() for ch in text):
        raise ValidationError("密钥里不能有空格或换行：请只粘贴 platform.deepseek.com 给出的那串字符")
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    # 日志只能出现掩码 —— 明文密钥进一次日志，就等于把凭据写进了磁盘上的历史
    log.info("已保存联网搜索密钥（%s）", mask_token(text))
    return path


def clear_key() -> None:
    key_file().unlink(missing_ok=True)


def providers() -> list[dict]:
    return [{"id": pid, "label": conf["label"]} for pid, conf in PROVIDERS.items()]


def norm_provider(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in PROVIDERS:
        raise ValidationError(
            f"搜索提供方只能是 {' / '.join(PROVIDERS)}（收到 {value!r}）")
    return text


def norm_count(value: Any) -> int:
    lo, hi = COUNT_RANGE
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError(f"条数（count）只能是 {lo}~{hi} 的整数（收到 {value!r}）") from None
    if not lo <= num <= hi:
        raise ValidationError(
            f"条数（count）只能是 {lo}~{hi}：官方上限 {hi}，多了会被服务端直接拒绝（收到 {num}）")
    return num


def norm_fetch_chars(value: Any) -> int:
    lo, hi = FETCH_CHARS_RANGE
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError(f"正文上限（fetch_chars）只能是 {lo}~{hi} 的整数（收到 {value!r}）") from None
    if not lo <= num <= hi:
        raise ValidationError(
            f"正文上限（fetch_chars）只能是 {lo}~{hi} 个字符（收到 {num}）："
            f"太小读不到内容，太大会把上下文挤爆")
    return num


def norm_bool(value: Any, *, field: str = "参数") -> bool:
    """布尔（表单串 `"true"` / `1` 都认）。**不认识的取值一律报错**，不猜。"""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ValidationError(f"{field} 只能是 true / false（收到 {value!r}）")


def effective() -> dict:
    """当前生效的搜索配置（setting 表 > 模块默认值）。**唯一的读取点。**"""
    stored = _stored()

    def pick(key: str, default: Any) -> Any:
        if key in stored and stored[key] not in (None, ""):
            return stored[key]
        return default

    return {
        "provider": norm_provider(pick("websearch_provider", DEFAULT_PROVIDER)),
        "count": norm_count(pick("websearch_count", DEFAULT_COUNT)),
        "need_content": norm_bool(pick("websearch_need_content", DEFAULT_NEED_CONTENT),
                                  field="need_content"),
        "fetch_chars": norm_fetch_chars(pick("websearch_fetch_chars", DEFAULT_FETCH_CHARS)),
    }


def public_config() -> dict:
    """给前端 / 给 agent 看的配置（**不含密钥本体，只有掩码**）。"""
    cfg = effective()
    token = read_key()
    return {
        "provider": cfg["provider"],
        "providers": providers(),
        "has_key": bool(token),
        # 前端契约是"没密钥时给空串"（不是 null）：输入框据此显示占位符
        "key_masked": mask_token(token) or "",
        "count": cfg["count"],
        "need_content": cfg["need_content"],
        "fetch_chars": cfg["fetch_chars"],
        "ready": bool(token),
    }


def save_config(payload: dict) -> dict:
    """保存密钥与四个参数，返回新的完整配置（+ `applied`）。

    每个字段都是**可选**的：设置页改一项只提交一项，避免"保存密钥时把没动过的条数
    又写一遍"。`api_key` 空串 = 本次不改；`clear_key` = 清除。
    """
    payload = payload or {}
    applied: dict[str, Any] = {}

    if payload.get("clear_key"):
        clear_key()
        applied["api_key"] = "cleared"
    elif str(payload.get("api_key") or "").strip():
        write_key(str(payload["api_key"]))
        applied["api_key"] = "saved"

    updates: dict[str, Any] = {}
    if payload.get("provider") is not None:
        updates["websearch_provider"] = norm_provider(payload["provider"])
    if payload.get("count") is not None:
        updates["websearch_count"] = norm_count(payload["count"])
    if payload.get("need_content") is not None:
        updates["websearch_need_content"] = norm_bool(payload["need_content"],
                                                     field="need_content")
    if payload.get("fetch_chars") is not None:
        updates["websearch_fetch_chars"] = norm_fetch_chars(payload["fetch_chars"])

    for key, value in updates.items():
        _write_setting(key, value)
    applied.update(updates)

    cfg = public_config()
    cfg["applied"] = applied
    return cfg


# --------------------------------------------------------------------------
# 传输层：默认 httpx，测试注入假实现（与 `mineru_client.HttpTransport` 同一形状）
# --------------------------------------------------------------------------


class HttpTransport(Protocol):
    def post_json(self, url: str, body: dict, headers: dict,
                  timeout: float) -> tuple[int, dict]: ...


class HttpxTransport:
    def post_json(self, url: str, body: dict, headers: dict,
                  timeout: float) -> tuple[int, dict]:
        import httpx

        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            resp = c.post(url, json=body, headers=headers)
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001 —— 网关 html / 空体不该炸在解析上
                data = {}
            return resp.status_code, data if isinstance(data, dict) else {}


def _transport() -> HttpTransport:
    """测试替换这一个函数就能完全离线（不联网、不碰真密钥）。"""
    return HttpxTransport()


def _upstream_message(body: dict) -> str:
    """上游错误里的原文（OpenAI 兼容格式 `{"error":{"message":...}}`）。"""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        text = str(err.get("message") or "").strip()
        if text:
            return text
    return str(body.get("message") or "").strip() if isinstance(body, dict) else ""


def _fail(status: int, body: dict, *, what: str) -> WebSearchError:
    """HTTP 状态码 → 一句**可执行**的中文；上游原文一律附在括号里，绝不吞。"""
    upstream = _upstream_message(body)
    tail = f"（上游：{upstream}）" if upstream else ""
    if status in (401, 403):
        return WebSearchAuthError(
            f"DeepSeek 搜索密钥无效或已过期（HTTP {status}）：请到 platform.deepseek.com "
            f"重新生成一个，粘贴到「设置 → 联网搜索」{tail}")
    if status == 402:
        return WebSearchQuotaError(f"DeepSeek 账号余额不足（HTTP 402）：请先充值再试{tail}")
    if status == 429:
        return WebSearchQuotaError(
            f"调用太频繁，触发了频率或额度限制（HTTP 429）：Search 限 100 次/秒、"
            f"Fetch 限 25 次/秒，等一会儿再试、或把并发的检索收一收{tail}")
    if status in (408, 504):
        return WebSearchTimeoutError(f"{what}超时（HTTP {status}）：稍后重试，或换一个关键词{tail}")
    if status == 404:
        return WebSearchError(
            f"抓不到这个网页（HTTP 404）：地址不可用、被站点策略拦下，或页面没有可提取的正文{tail}")
    if status == 413:
        return WebSearchError(f"请求体太大（HTTP 413）：换个短一点的搜索词{tail}")
    if status == 400:
        return WebSearchError(f"{what}请求不合法（HTTP 400）：请检查参数取值{tail}")
    if status >= 500:
        return WebSearchError(f"DeepSeek {what}服务内部错误（HTTP {status}）：稍后重试{tail}")
    return WebSearchError(f"DeepSeek {what}失败（HTTP {status}）{tail}")


def _post(provider: str, kind: str, body: dict, *, what: str,
          timeout: float) -> dict:
    """打一次官方接口，返回响应体；失败一律抛 `WebSearchError` 的子类。"""
    conf = PROVIDERS.get(norm_provider(provider))
    key = read_key()
    if not key:
        raise WebSearchAuthError(
            "还没配置 DeepSeek 搜索密钥：请在「设置 → 联网搜索」里粘贴一个，"
            "或在 data/secrets/websearch.key 里写入 / 设 PR_DEEPSEEK_SEARCH_KEY 环境变量")
    url = str(conf[f"{kind}_url"])
    # 密钥只进请求头：不拼进 URL、不进日志、不进异常消息
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        status, data = _transport().post_json(url, body, headers, timeout)
    except Exception as exc:  # noqa: BLE001 —— 网络异常也要变成人话
        if type(exc).__name__.startswith("Timeout") or "Timeout" in type(exc).__name__:
            raise WebSearchTimeoutError(
                f"{what}超时（{timeout:.0f} 秒）：网络或 DeepSeek 侧太慢，稍后重试") from None
        raise WebSearchError(f"{what}失败：{type(exc).__name__}: {exc}"[:300]) from None
    if status >= 400:
        log.warning("DeepSeek %s 返回 HTTP %s：%s", what, status, _upstream_message(data)[:200])
        raise _fail(status, data, what=what)
    return data


def _check_url(url: str) -> str:
    """Fetch 只收"带 host 的 http/https、且不带用户名密码"（官方文档的硬条件）。"""
    text = (url or "").strip()
    if not text:
        raise ValidationError("url 不能为空")
    if len(text) > 8192:
        raise ValidationError(f"url 太长（{len(text)} > 8192 字符）")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationError(f"url 必须是带域名的 http/https 地址（收到 {url!r}）")
    if parts.username or parts.password:
        raise ValidationError("url 不能带用户名或密码")
    return text


def search(query: str, *, provider: str | None = None, count: Any = None,
           need_content: Any = None, ori_question: str = "",
           localization: str = "") -> dict:
    """联网搜索（`web_search` 工具的后端）。`count`/`need_content` 不传就用设置里的默认。"""
    cfg = effective()
    query_text = (query or "").strip()
    if not query_text:
        raise ValidationError("搜索词不能为空")
    if len(query_text) > QUERY_MAX_CHARS:
        raise ValidationError(f"搜索词最长 {QUERY_MAX_CHARS} 个字符（收到 {len(query_text)}）")
    num = cfg["count"] if count in (None, "") else norm_count(count)
    with_content = (cfg["need_content"] if need_content is None
                    else norm_bool(need_content, field="need_content"))

    body: dict[str, Any] = {"query": query_text, "count": num,
                            "need_content": with_content}
    if str(ori_question or "").strip():
        body["ori_question"] = str(ori_question).strip()[:QUERY_MAX_CHARS]
    if str(localization or "").strip():
        body["localization"] = str(localization).strip()

    data = _post(provider or cfg["provider"], "search", body,
                 what="搜索", timeout=SEARCH_TIMEOUT)
    results = []
    for raw in list(data.get("results") or []):
        if not isinstance(raw, dict):
            continue
        results.append({
            "title": str(raw.get("title") or ""),
            "url": str(raw.get("url") or ""),
            "snippet": str(raw.get("snippet") or ""),
            "summary": str(raw.get("summary") or ""),
            "content": str(raw.get("content") or ""),
            "site_name": str(raw.get("site_name") or ""),
            "date": raw.get("date"),
            "authority": raw.get("authority"),
            "rerank_score": raw.get("rerank_score"),
        })
    return {"provider": norm_provider(provider or cfg["provider"]),
            "query": query_text, "results": results}


def fetch(url: str, *, provider: str | None = None, max_chars: Any = None) -> dict:
    """抓一篇网页正文（`web_fetch` 工具的后端）；按 `fetch_chars` 截断并回 `truncated`。"""
    cfg = effective()
    target = _check_url(url)
    limit = cfg["fetch_chars"] if max_chars in (None, "") else norm_fetch_chars(max_chars)
    data = _post(provider or cfg["provider"], "fetch", {"url": target},
                 what="抓取网页", timeout=FETCH_TIMEOUT)
    text = str(data.get("content") or "")
    return {
        "url": target,
        "title": str(data.get("title") or ""),
        "content": text[:limit],
        "truncated": len(text) > limit,
    }


def verify() -> dict:
    """跑一次真实搜索（`count=1`）判断密钥能不能用。

    **HTTP 一律 200**（结论在 `ok`/`state` 里，与 `/mineru/verify` 同一口径）：
    "密钥过期"是用户要去修的状态，不是请求出错。计费按请求次数算，一次 count=1 的
    搜索是最便宜的真实验证。
    """
    started = time.perf_counter()
    if not read_key():
        return {"ok": False, "state": "no_key",
                "detail": "还没配置 DeepSeek 搜索密钥：请到 platform.deepseek.com 申请后粘贴到下面",
                "ms": 0}
    try:
        data = search("DeepSeek 官方文档", count=1)
    except WebSearchAuthError as exc:
        return {"ok": False, "state": "auth", "detail": exc.message,
                "ms": _ms(started)}
    except WebSearchQuotaError as exc:
        return {"ok": False, "state": "quota", "detail": exc.message,
                "ms": _ms(started)}
    except WebSearchTimeoutError as exc:
        return {"ok": False, "state": "timeout", "detail": exc.message,
                "ms": _ms(started)}
    except PaperMindError as exc:
        return {"ok": False, "state": "error", "detail": exc.message,
                "ms": _ms(started)}
    found = len(data.get("results") or [])
    log.info("联网搜索校验：ok=True 命中 %d 条", found)
    return {"ok": True, "state": "ok",
            "detail": f"搜索可用：返回 {found} 条结果",
            "ms": _ms(started)}


def _ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


__all__ = [
    "COUNT_RANGE", "DEFAULT_COUNT", "DEFAULT_FETCH_CHARS", "DEFAULT_NEED_CONTENT",
    "DEFAULT_PROVIDER", "FETCH_CHARS_RANGE", "KEY_ENV", "KEY_FILE_NAME", "PROVIDERS",
    "SETTING_KEYS", "WebSearchAuthError", "WebSearchError", "WebSearchQuotaError",
    "WebSearchTimeoutError", "clear_key", "effective", "fetch", "key_file",
    "key_source", "norm_bool", "norm_count", "norm_fetch_chars", "norm_provider",
    "providers", "public_config", "read_key", "save_config", "search", "verify",
    "write_key",
]
