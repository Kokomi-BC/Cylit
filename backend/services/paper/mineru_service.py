"""MinerU 在线解析服务：token 管理 + 配置 + 校验 + 把一篇论文解析出来。

**配置只有这一个写入点**（`save_config`）—— 三个键 `mineru_engine_default` /
`mineru_model_version` / `mineru_language` 存在 setting 表里，不走 `PUT /settings`
（那套白名单另有 `kb.` 前缀回退规则，两处都能写就会出现"界面改了、行为没变"，
这个项目为同类问题吃过一次亏，见 dev-log §17.66）。读的时候仍然是
"setting 表 > `Settings` 字段默认值"，所以 `PR_MINERU_*` 环境变量照样能当默认值用。

token 单独走密钥文件（`data/secrets/mineru.key`，不落库、不进代码，规划 §12.2），
因为它是**长期凭据**：官方没有续期接口，过期（`A0211`）只能重新生成再贴一次。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Callable

from backend.core.config import get_settings
from backend.core.errors import ValidationError
from backend.core.logging import get_logger
from backend.core.utils import iso_utc, json_dumps, json_loads, now
from backend.repositories.mysql.models import Setting
from backend.repositories.mysql.session import session_scope
from backend.parsing.models import ParseResult
from backend.parsing.online import page_ranges
from backend.parsing.online.mineru_builder import build_from_bundle
from backend.parsing.online.mineru_client import MineruClient, mask_token

log = get_logger(__name__)

#: 本服务**独占**写入的设置键（见模块头：只允许这一个写入点）
SETTING_KEYS = ("mineru_engine_default", "mineru_model_version", "mineru_language")

#: 解析引擎（导入弹窗与设置页共用同一份取值，避免两处各写一套）
ENGINES = ("local", "mineru")
ENGINE_LABELS = {"local": "本地解析", "mineru": "在线解析（MinerU）"}

#: 官方文档给出的 ``model_version`` 取值。``MinerU-HTML`` 只对 HTML 源文件有效，
#: 本系统只传 PDF，所以不提供。
MODEL_VERSIONS = ("pipeline", "vlm")
MODEL_VERSION_LABELS = {
    "pipeline": "pipeline（版面+公式更稳，默认）",
    "vlm": "vlm（视觉大模型，复杂版面更强）",
}

#: 官方 ``language`` 取值（只影响 OCR）。全列在这里是为了设置页能给出选项，
#: 而不是让用户去猜；写入时仍然只做格式校验（服务端加了新语言不该被我们拦住）。
LANGUAGES = (
    ("en", "英语"), ("ch", "中文"), ("ch_server", "中文（服务端）"),
    ("japanese", "日语"), ("korean", "韩语"), ("chinese_cht", "繁体中文"),
    ("latin", "拉丁语系（法/德/西/葡等）"), ("arabic", "阿拉伯语系"),
    ("cyrillic", "西里尔语系"), ("east_slavic", "东斯拉夫语系"),
    ("devanagari", "天城文"), ("ta", "泰米尔语"), ("te", "泰卢固语"),
    ("ka", "卡纳达语"), ("el", "希腊语"), ("th", "泰语"),
)


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------


def _stored() -> dict:
    with session_scope() as sess:
        return {r.key: json_loads(r.value, r.value) for r in sess.query(Setting).all()}


def _write_setting(key: str, value: Any) -> None:
    """写一条 setting（**唯一的写入入口**：单测把它换成字典即可完全离线）。

    与 `_stored` 成对：读也一个口、写也一个口，测试注入时不用去 mock SQLAlchemy 会话。
    """
    with session_scope() as sess:
        row = sess.get(Setting, key)
        if row is None:
            row = Setting(key=key)
            sess.add(row)
        row.value = json_dumps(value)


def effective() -> dict:
    """当前生效的 MinerU 配置（setting 表 > `Settings` 默认值）。"""
    s = get_settings()
    stored = _stored()

    def pick(key: str) -> Any:
        if key in stored and stored[key] not in (None, ""):
            return stored[key]
        return getattr(s, key)

    token = s.mineru_token
    return {
        "engine_default": norm_engine(pick("mineru_engine_default")),
        "model_version": norm_model_version(pick("mineru_model_version")),
        "language": norm_language(pick("mineru_language")),
        "base_url": s.mineru_base_url,
        "has_token": bool(token),
        "token_masked": mask_token(token),
        "token_source": token_source(),
        "token_file": str(s.mineru_token_file),
        "online_ready": bool(token),
        "engines": list(ENGINES),
        "model_versions": list(MODEL_VERSIONS),
        # 带中文标签的模型选项（导入弹窗的"模型"下拉直接用；`model_versions` 保留给
        # 设置页那个只显示原始值的下拉，改形状会动到已上线的界面）
        "model_version_options": [
            {"value": v, "label": MODEL_VERSION_LABELS.get(v, v)}
            for v in MODEL_VERSIONS],
        "languages": [{"value": v, "label": t} for v, t in LANGUAGES],
        "last_verify": stored.get("mineru_last_verify"),
        "page_warn": int(s.mineru_page_warn),
    }


def public_config() -> dict:
    """给前端的配置（不含 token 本体，只有掩码）。"""
    return effective()


def token_source() -> str | None:
    """token 从哪来：``file``（界面里保存的）/ ``env``（`PR_MINERU_TOKEN`）/ None。"""
    s = get_settings()
    if s.read_secret("mineru.key"):
        return "file"
    import os

    if (os.environ.get("PR_MINERU_TOKEN") or "").strip():
        return "env"
    return None


def read_token() -> str | None:
    return get_settings().mineru_token


def write_token(token: str) -> Path:
    """把 token 写进密钥文件（界面「保存」走这里）。"""
    value = (token or "").strip()
    if not value:
        raise ValidationError("token 不能为空")
    if any(ch.isspace() for ch in value):
        raise ValidationError("token 里不能有空格或换行：请只粘贴 mineru.net 给出的那串字符")
    s = get_settings()
    s.secrets_dir.mkdir(parents=True, exist_ok=True)
    path = s.mineru_token_file
    path.write_text(value, encoding="utf-8")
    log.info("已保存 MinerU token（%s）", mask_token(value))
    return path


def clear_token() -> None:
    get_settings().mineru_token_file.unlink(missing_ok=True)


def save_config(payload: dict) -> dict:
    """保存 token / 引擎 / 模型版本 / 语言，返回新的完整配置。

    每个字段都是**可选**的：设置页里改一项只提交一项，避免"保存 token 时把没动过的
    引擎选择又写一遍"（那样一旦前端某次没回填就会静默改掉用户的默认值）。
    """
    payload = payload or {}
    applied: dict[str, Any] = {}

    if payload.get("clear_token"):
        clear_token()
        applied["token"] = "cleared"
    elif str(payload.get("token") or "").strip():
        write_token(str(payload["token"]))
        applied["token"] = "saved"

    updates: dict[str, Any] = {}
    if payload.get("engine_default") is not None:
        updates["mineru_engine_default"] = norm_engine(payload["engine_default"])
    if payload.get("model_version") is not None:
        updates["mineru_model_version"] = norm_model_version(payload["model_version"])
    if payload.get("language") is not None:
        updates["mineru_language"] = norm_language(payload["language"])

    if updates:
        for key, value in updates.items():
            _write_setting(key, value)
        applied.update(updates)

    cfg = effective()
    cfg["applied"] = applied
    return cfg


def norm_engine(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "auto":
        text = "local"
    if text not in ENGINES:
        raise ValidationError(
            f"解析引擎只能是 {' / '.join(ENGINES)}（收到 {value!r}）")
    return text


def norm_model_version(value: Any) -> str:
    text = str(value or "").strip()
    if text not in MODEL_VERSIONS:
        raise ValidationError(
            f"MinerU 模型版本只能是 {' / '.join(MODEL_VERSIONS)}（收到 {value!r}）")
    return text


def norm_language(value: Any) -> str:
    text = str(value or "").strip().lower()
    # 只校验格式：官方加了新语言时不该被我们这个列表拦住（列表只用于界面展示）
    if not re.fullmatch(r"[a-z][a-z_]{1,19}", text):
        raise ValidationError(
            f"OCR 语言取值不合法（收到 {value!r}）：应形如 en / ch / latin")
    return text


#: 页码范围：官方支持 `1-600`（区间）、`2,4-6`（逗号串）、`2--2`（`-2` = 倒数第二页）。
#: 语法与"切片内序号 → 原文档页号"的映射都在 `parsing.online.page_ranges`（那份映射
#: 是给构建器用的，服务层只做校验与规范化 —— 一套语法只留一个实现）。
PAGE_RANGES_HINT = page_ranges.PAGE_RANGES_HINT


def norm_page_ranges(value: Any) -> str:
    """校验/规范化页码范围（空串 = 全部页）。

    **必须在这里拦住**：真写错了，服务端只会回一句 `-500 参数错误`，
    用户根本猜不到是页码写错；这里直接说清"哪不对 + 正确写法"。
    """
    try:
        return page_ranges.compact_page_ranges(value)
    except ValueError:
        raise ValidationError(
            f"页码范围格式不对（收到 {value!r}）：{PAGE_RANGES_HINT}") from None


def as_bool(value: Any, *, field: str = "参数") -> bool:
    """表单/查询串里的布尔（`"true"` / `1` / true …）。**不认识的取值一律报错**，不猜。"""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ValidationError(f"{field} 只能是 true / false（收到 {value!r}）")


def normalize_params(payload: dict | None) -> dict:
    """把接口传来的 MinerU 参数归一化，返回可**直接进 PARSE 任务参数**的 `mineru_*` 字典。

    只处理**显式给出**的键：没给的键不出现在结果里，由 :func:`effective_params` 再按
    "任务参数 > 设置默认值 > 官方默认值"补齐。为什么不让空值也写进去 ——
    那会把"用户没动这一项"变成"用户把它设成了默认"，正是静默失效的来源。
    """
    payload = payload or {}
    out: dict[str, Any] = {}
    if payload.get("mineru_model_version") not in (None, ""):
        out["mineru_model_version"] = norm_model_version(payload["mineru_model_version"])
    if payload.get("mineru_language") not in (None, ""):
        out["mineru_language"] = norm_language(payload["mineru_language"])
    if payload.get("mineru_page_ranges") not in (None, ""):
        out["mineru_page_ranges"] = norm_page_ranges(payload["mineru_page_ranges"])
    for key, label in (("mineru_is_ocr", "强制 OCR"),
                       ("mineru_enable_formula", "识别公式"),
                       ("mineru_enable_table", "识别表格")):
        if payload.get(key) not in (None, ""):
            out[key] = as_bool(payload[key], field=label)
    return out


def _import_defaults() -> dict:
    """导入时的两个插图开关默认值（设置页存的那一份）。"""
    from backend.services.paper import parse_prefs

    return parse_prefs.local_defaults()


def effective_params(params: dict | None = None) -> dict:
    """本次解析**真正要用**的 MinerU 参数（任务参数 > 设置默认值 > 官方默认值）。

    官方默认值：`model_version=pipeline`、`language=ch`（本系统面向英文论文，设置里给 `en`）、
    `enable_formula` / `enable_table` 都开、`is_ocr` 关。
    """
    from backend.services.paper import parse_prefs

    params = params or {}
    cfg = effective()
    # 中间那一层是「设置 → 在线服务 → 解析参数」里存的导入默认值（parse_prefs）
    defaults = parse_prefs.mineru_defaults()
    model = str(params.get("mineru_model_version") or cfg["model_version"] or "pipeline")
    lang = str(params.get("mineru_language") or cfg["language"] or "en")
    return {
        "model_version": norm_model_version(model),
        "language": norm_language(lang),
        "enable_formula": as_bool(
            params.get("mineru_enable_formula", defaults["mineru_enable_formula"]),
            field="识别公式"),
        "enable_table": as_bool(
            params.get("mineru_enable_table", defaults["mineru_enable_table"]),
            field="识别表格"),
        "is_ocr": as_bool(params.get("mineru_is_ocr", defaults["mineru_is_ocr"]),
                          field="强制 OCR"),
        "page_ranges": norm_page_ranges(
            params.get("mineru_page_ranges") or defaults["mineru_page_ranges"] or ""),
    }


# --------------------------------------------------------------------------
# 校验（token 是否可用）
# --------------------------------------------------------------------------


def client(token: str | None = None) -> MineruClient:
    """按当前配置构造客户端（超时/轮询参数都来自 `Settings`）。"""
    s = get_settings()
    return MineruClient(
        token or read_token(),
        base_url=s.mineru_base_url,
        upload_timeout=s.mineru_upload_timeout,
        download_timeout=s.mineru_download_timeout,
        poll_interval=s.mineru_poll_interval,
        poll_deadline=s.mineru_poll_deadline,
    )


def verify() -> dict:
    """校验 token 并把结果记一笔（设置页刷新后还能看到上次校验结论）。

    **不创建解析任务、不消耗配额**：实现是拿随机 task_id 去查一次任务
    （见 `MineruClient.verify` 的判据说明）。
    """
    s = get_settings()
    token = read_token()
    if not token:
        result = {"ok": False, "state": "missing",
                  "message": "还没配置 MinerU token：请到 mineru.net 申请后粘贴到下面",
                  "token_masked": None, "at": iso_utc(now())}
    else:
        result = client(token).verify()
        result["at"] = iso_utc(now())
    _write_setting("mineru_last_verify", result)
    log.info("MinerU token 校验：ok=%s state=%s", result.get("ok"), result.get("state"))
    return result


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def parse_online(
    paper_id: str,
    *,
    params: dict | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[ParseResult, dict]:
    """在线解析一篇论文：上传 → 等结果 → 解包 → 归一化成 ParseResult。

    返回 ``(result, bundle)``：bundle 里有 batch_id / 结果包路径，任务回执与解析
    报告都要用到（下载到 ``data/papers/<id>/mineru/``，也方便人工核对 MinerU 的原始产物）。
    """
    from backend.services.paper import paper_service

    params = params or {}
    s = get_settings()
    token = read_token()
    if not token:
        # 抛 MineruAuthError 而不是 ValidationError：调用方（任务处理器）要把它当
        # "配置问题"报给用户，而不是"解析失败"
        from backend.parsing.online.mineru_client import MineruAuthError

        raise MineruAuthError(
            "还没配置 MinerU token：请到「设置 → 在线解析（MinerU）」填入并验证，"
            "或改用本地解析")

    paper = paper_service.get_paper(paper_id)
    paths = paper_service.paper_paths(paper_id)
    pdf = paper_service.resolve_pdf(paper_id)
    # 本次真正生效的参数（任务参数 > 设置默认值 > 官方默认值），见 `effective_params`
    mp = effective_params(params)
    log.info("在线解析参数 %s：model=%s lang=%s formula=%s table=%s ocr=%s pages=%s",
             paper_id, mp["model_version"], mp["language"], mp["enable_formula"],
             mp["enable_table"], mp["is_ocr"], mp["page_ranges"] or "全部")

    page_count = int(paper.get("page_count") or 0)
    warn_pages = int(s.mineru_page_warn)
    if page_count > warn_pages and not mp["page_ranges"]:
        # 官方两处文档对页数上限说法不一致（600 / 200），超过了只说一声，不拦。
        # 限定了页码范围就说明用户已经自己分段了，不用再唠叨。
        log.warning("论文 %s 有 %d 页，超过 MinerU 常见上限 %d 页，可能解析不全",
                    paper_id, page_count, warn_pages)

    raw_dir = paths["root"] / "mineru"
    # 每次解析都从空目录开始：解包出来的文件名带任务 uuid，上一个任务的残留会让
    # `locate_bundle`（取最浅的那份）有可能挑到旧产物 —— 那是最难查的一类静默错误。
    _clear_dir(raw_dir)

    cli = client(token)
    bundle = cli.parse_pdf(
        pdf, raw_dir,
        name=f"{paper_id}.pdf",
        data_id=paper_id,
        model_version=mp["model_version"],
        language=mp["language"],
        enable_formula=mp["enable_formula"],
        enable_table=mp["enable_table"],
        is_ocr=mp["is_ocr"],
        page_ranges=mp["page_ranges"] or None,
        on_progress=progress,
        should_stop=should_stop,
    )

    result = build_from_bundle(
        pdf, paper_id, paths["root"], bundle,
        dpi=int(s.render_dpi), crop_dpi=int(s.crop_dpi),
        # 任务参数没给才落到「设置 → 在线服务」里存的导入默认值（两处引擎共用同一套）
        figure_caption=bool(params.get("figure_caption", _import_defaults()["parse_figure_caption"])),
        deep_figure_search=bool(params.get("deep_figure_search",
                                       _import_defaults()["parse_deep_figure_search"])),
        model_version=mp["model_version"],
        mineru_params=mp,
        progress=progress,
    )
    if page_count > warn_pages and not mp["page_ranges"]:
        result.warnings.append(
            f"本篇 {page_count} 页，超过 MinerU 常见上限 {warn_pages} 页："
            f"若结果缺页，请改用本地解析或在「页码范围」里分段解析")
    return result, bundle


def _clear_dir(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    else:
        path.mkdir(parents=True, exist_ok=True)


__all__ = [
    "ENGINES", "ENGINE_LABELS", "LANGUAGES", "MODEL_VERSIONS",
    "MODEL_VERSION_LABELS", "PAGE_RANGES_HINT", "SETTING_KEYS", "as_bool",
    "clear_token", "client", "effective", "effective_params", "norm_engine",
    "norm_language", "norm_model_version", "norm_page_ranges",
    "normalize_params", "parse_online", "public_config", "read_token",
    "save_config", "token_source", "verify", "write_token",
]
