"""技能库的落盘管理（上传 / 导出 / 禁用 / 卸载 / 自带技能物化）。

**技能是主后端管理的文件，agent 只读。** agent 进程不连数据库、也不认识这里的
manifest，它做的唯一一件事是扫描 `<data_dir>/skills/*/SKILL.md`（见
`agent/runtime.py`）。所以技能库的形状必须"自解释"：

    <data_dir>/skills/
    ├── .manifest.json          {名字: 自带 SKILL.md 的内容哈希}（识别"这是自带技能"）
    ├── <name>/SKILL.md         启用中 —— agent 的扫描器只认这一层
    └── .disabled/<name>/…      已禁用 —— 目录名以点开头、扫描器自然不会进

三个"为什么"值得写下来：

1. **为什么禁用是"搬目录"**：agent 只读目录本身，它不看我们的开关。把目录挪出扫描
   范围是唯一不需要改 agent 就能生效的做法 —— 也就没有"两边口径不一致"的可能。
2. **为什么技能落在 `data/` 而不是仓库里**：部署后 `data/` 不受 `git reset` 影响，
   用户装的技能、禁用状态都能留住；自带技能由 `ensure_builtin_skills()` 每次启动
   **幂等**物化进去（内容哈希没变就什么都不做）。
3. **为什么 manifest 记的是哈希而不是布尔**：既要认出"这是自带技能"（→ 不能卸载），
   也要认出"用户改过这份副本没有"。改过就不覆盖 —— 用户的手改比版本更新更重要。

zip 上传的硬闸（防穿越 + 防炸）在 `_extract()`：绝对路径 / `..` / 反斜杠 / 符号链接
一律拒；条目数与解压总大小封顶；顶层必须正好一个目录且含 `SKILL.md`。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from backend.core.errors import ConflictError, NotFoundError, ValidationError

#: 技能名（也是目录名）。比框架的上限（64）保守一点，且强制"字母数字开头"：
#: 名字会进 URL（`/agent/skills/{name}/download`），点号/斜杠开头的名字只会带来歧义。
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")

#: 禁用区（以点开头 → 不会被技能扫描器当成技能包）
DISABLED_DIR = ".disabled"
#: 自带技能的登记表（名字 → 内容哈希）
MANIFEST_NAME = ".manifest.json"
UPLOAD_PREFIX = ".upload-"

#: 一个技能包最多多少个文件 / 解压后多少字节（"技能"是给模型看的说明文本，
#: 不是数据集；5MB 已经足够放下整套文档与示例图）
MAX_ENTRIES = 200
MAX_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------
# 路径与小工具
# --------------------------------------------------------------------------


def skills_root(settings) -> Path:
    """技能库根目录（`<data_dir>/skills`）。"""
    return Path(settings.agent_skills_dir)


def builtin_root(settings) -> Path:
    """自带技能的源目录（仓库里的那份，只读）。"""
    return Path(settings.agent_builtin_skills_dir)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_manifest(root: Path) -> dict[str, str]:
    try:
        data = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _save_manifest(root: Path, manifest: dict[str, str]) -> None:
    path = root / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(manifest.items())), ensure_ascii=False, indent=2),
                    encoding="utf-8")


def parse_frontmatter(text: str) -> tuple[str | None, str]:
    """`(name, description)`；没有 frontmatter 就 `(None, "")`。

    优先用 PyYAML（技能包里 `description: >` 这种块标量很常见），
    但它不是硬依赖 —— 读不到就退回"逐行 `key: value`"的土办法。
    """
    if not text.startswith("---"):
        return None, ""
    end = text.find("\n---", 3)
    if end < 0:
        return None, ""
    head = text[3:end]
    data: Any
    try:
        import yaml

        data = yaml.safe_load(head) or {}
    except Exception:  # noqa: BLE001 —— 土办法能救回绝大多数单行 frontmatter
        data = {}
        for line in head.splitlines():
            key, _, value = line.partition(":")
            if value.strip():
                data.setdefault(key.strip(), value.strip())
    if not isinstance(data, dict):
        data = {}
    name = str(data.get("name") or "").strip() or None
    description = str(data.get("description") or "").strip()
    return name, description


def _check_name(name: str) -> str:
    if not SKILL_NAME_RE.match(name or ""):
        raise ValidationError(
            f"技能名 {name!r} 不合法：只允许小写字母 / 数字 / 连字符，2~41 个字符，"
            f"且以字母或数字开头（技能名同时是目录名与 URL 里的那段）")
    return name


# --------------------------------------------------------------------------
# 探查
# --------------------------------------------------------------------------


def builtin_names(settings) -> set[str]:
    """自带技能的名字集合（manifest 里登记过的 ∪ 仓库里现在有的）。"""
    names = set(_load_manifest(skills_root(settings)))
    src = builtin_root(settings)
    if src.is_dir():
        names |= {p.name for p in src.iterdir()
                  if p.is_dir() and (p / "SKILL.md").is_file()}
    return names


def is_builtin(settings, name: str) -> bool:
    return name in builtin_names(settings)


def locate(settings, name: str) -> tuple[Path, bool] | None:
    """`(目录, 是否启用)`；两处都没有就是 `None`。"""
    root = skills_root(settings)
    for base, enabled in ((root, True), (root / DISABLED_DIR, False)):
        candidate = base / name
        if candidate.is_dir() and (candidate / "SKILL.md").is_file():
            return candidate, enabled
    return None


def _entry(path: Path, *, builtin: bool, enabled: bool) -> dict:
    md = path / "SKILL.md"
    text = md.read_text(encoding="utf-8", errors="ignore")
    _fm_name, description = parse_frontmatter(text)
    return {
        #: 名字取**目录名**：它是身份（URL / 禁用 / 卸载都按它），frontmatter 的 name
        #: 必须与它一致（框架加载器也是这么要求的，见 `pydantic_ai_harness` 的校验）。
        "name": path.name,
        "description": description,
        "builtin": builtin,
        "enabled": enabled,
        "chars": len(text),
        "updated_at": datetime.fromtimestamp(
            md.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
    }


def list_skills(settings) -> list[dict]:
    """全部技能（启用的在前，各按名字排）。"""
    root = skills_root(settings)
    builtin = builtin_names(settings)
    out: list[dict] = []
    for base, enabled in ((root, True), (root / DISABLED_DIR, False)):
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.name.startswith(".") or not child.is_dir():
                continue
            if not (child / "SKILL.md").is_file():
                continue
            out.append(_entry(child, builtin=child.name in builtin, enabled=enabled))
    out.sort(key=lambda item: (not item["enabled"], item["name"]))
    return out


# --------------------------------------------------------------------------
# 自带技能物化（启动时，幂等）
# --------------------------------------------------------------------------


def ensure_builtin_skills(settings) -> dict[str, str]:
    """把仓库里的自带技能合并进技能库；返回 manifest 内容。

    三条规则：
    - 目标里没有 → 整目录拷过去，登记哈希；
    - 目标里有一份、且**没被用户改过**（内容哈希与登记值一致）→ 随版本更新覆盖；
    - 目标里有一份、但用户改过 → **一律不动**（手改比版本更新重要）。
    """
    root = skills_root(settings)
    src_root = builtin_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(root)
    if not src_root.is_dir():
        return manifest

    for src in sorted(src_root.iterdir()):
        src_md = src / "SKILL.md"
        if not (src.is_dir() and src_md.is_file()):
            continue
        name = src.name
        target = root / name
        target_md = target / "SKILL.md"
        src_hash = _sha256(src_md)
        if not target_md.is_file():
            if target.exists():                     # 半截目录（上次拷到一半）
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(src, target)
            manifest[name] = src_hash
            continue
        recorded = manifest.get(name)
        local_hash = _sha256(target_md)
        if recorded is None:
            # 第一次接管：只有"内容与自带完全一致"才算没改过；否则当作用户版本，别覆盖。
            manifest[name] = src_hash if local_hash == src_hash else local_hash
            continue
        if local_hash == recorded and local_hash != src_hash:
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(src, target)
            manifest[name] = src_hash
    _save_manifest(root, manifest)
    return manifest


# --------------------------------------------------------------------------
# zip 上传
# --------------------------------------------------------------------------


def _safe_member(info: zipfile.ZipInfo) -> PurePosixPath | None:
    """校验一个 zip 条目的路径；返回 `None` 表示"刻意忽略"（系统垃圾文件）。"""
    raw = info.filename
    if not raw or "\x00" in raw:
        raise ValidationError("zip 里有非法文件名（空名或含空字节）")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValidationError(f"zip 里有绝对路径：{raw}（只接受相对路径）")
    if "\\" in raw:
        raise ValidationError(f"zip 里有反斜杠路径：{raw}（Windows 打包的越界写法，请重新打包）")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValidationError(f"zip 里有 .. 路径：{raw}（拒绝路径穿越）")
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        raise ValidationError(f"zip 里有符号链接：{raw}（只接受普通文件）")
    if not parts:
        return None
    if parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
        return None
    if any(p.startswith(".") for p in parts):
        return None
    return PurePosixPath(*parts)


def _extract(data: bytes, tmp: Path) -> str:
    """解压到 `tmp/raw`，返回技能名（尚未做重名/内容校验）。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValidationError("这不是一个有效的 zip 文件") from None

    raw = tmp / "raw"
    raw.mkdir()
    total = 0
    with archive as zf:
        members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = _safe_member(info)
            if rel is None:
                continue
            total += int(info.file_size or 0)
            if total > MAX_BYTES:
                raise ValidationError(f"解压后超过 {MAX_BYTES // 1024 // 1024}MB 上限")
            if len(members) >= MAX_ENTRIES:
                raise ValidationError(f"文件数超过 {MAX_ENTRIES} 个上限")
            members.append((info, rel))
        if not members:
            raise ValidationError("zip 是空的（或者里面只有系统垃圾文件）")

        # 边写边数**实际**字节：上面那个 `total` 用的是 zf 里声明的 file_size，
        # 一份伪造头部的 zip 可以声称 1KB、解出 1GB。写的时候再拦一次才叫真的封顶。
        written = 0
        for info, rel in members:
            dest = raw.joinpath(*rel.parts)
            try:
                dest.resolve().relative_to(raw.resolve())
            except ValueError:                       # 理论上前面已经拦住，兜一层
                raise ValidationError(f"zip 条目逃出了技能目录：{info.filename}") from None
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise ValidationError(
                            f"解压后超过 {MAX_BYTES // 1024 // 1024}MB 上限")
                    out.write(chunk)

    tops = list(raw.iterdir())
    dirs = [p for p in tops if p.is_dir() and not p.is_symlink()]
    files = [p for p in tops if not p.is_dir()]
    if len(dirs) == 1 and not files:
        name = _check_name(dirs[0].name)
        # 统一搬到 tmp/<name>：调用方只需要知道"技能名"，暂存位置不该分两种形状
        shutil.move(str(dirs[0]), str(tmp / name))
    elif len(dirs) == 0 and (raw / "SKILL.md").is_file():
        # 根即 SKILL.md：名字只能来自 frontmatter（这时候它必须给）
        fm_name, _desc = parse_frontmatter(
            (raw / "SKILL.md").read_text(encoding="utf-8", errors="ignore"))
        if not fm_name:
            raise ValidationError(
                "zip 根目录直接放 SKILL.md 时，frontmatter 里必须写 name（并且要合法）；"
                "或者把技能放进一个与技能同名的目录再压缩")
        name = _check_name(fm_name)
        staged = tmp / name
        shutil.move(str(raw), str(staged))
    else:
        raise ValidationError(
            "zip 的顶层必须正好是一个目录（里面放 SKILL.md），或者根上直接放 SKILL.md；"
            f"现在顶层是 {sorted(p.name for p in tops)[:6]}")
    return name


def upload(settings, data: bytes, *, filename: str = "") -> tuple[list[dict], list[str]]:
    """安装一个技能包；返回 `(全部技能, 本次新增的名字)`。"""
    if not data:
        raise ValidationError("空文件：请上传一个 .zip")
    if filename and not filename.lower().endswith(".zip"):
        raise ValidationError("只支持 .zip（把技能目录压缩成 zip 再上传）")

    root = skills_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=UPLOAD_PREFIX, dir=str(root)))
    try:
        name = _extract(data, tmp)
        staged = tmp / name
        md = staged / "SKILL.md"
        if not md.is_file():
            raise ValidationError(f"技能包里没有 SKILL.md（应当是 <{name}>/SKILL.md）")

        if name in builtin_names(settings):
            raise ValidationError(
                f"技能名 {name} 与**自带技能**重名，请改名后再上传（自带技能不能被覆盖）")
        if locate(settings, name) is not None:
            raise ConflictError(f"已经有一个叫 {name} 的技能了：先卸载它，再上传新的")

        fm_name, description = parse_frontmatter(
            md.read_text(encoding="utf-8", errors="ignore"))
        if fm_name and fm_name != name:
            raise ValidationError(
                f"SKILL.md 的 frontmatter name（{fm_name}）必须与目录名一致（{name}）"
                f"—— 框架按目录名定位技能，两个名字不同会加载失败")
        if not description:
            raise ValidationError("SKILL.md 的 frontmatter 必须给出非空的 description"
                                  "（它常驻提示词，模型靠它决定要不要用这个技能）")

        shutil.move(str(staged), str(root / name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return list_skills(settings), [name]


# --------------------------------------------------------------------------
# 启用 / 禁用 / 卸载 / 导出
# --------------------------------------------------------------------------


def set_enabled(settings, name: str, enabled: bool) -> list[dict]:
    """启用/禁用：把目录在 `skills/` 与 `skills/.disabled/` 之间搬。"""
    _check_name(name)
    root = skills_root(settings)
    active = root / name
    parked = root / DISABLED_DIR / name
    found = locate(settings, name)
    if found is None:
        raise NotFoundError(f"没有叫 {name} 的技能")
    path, current = found
    if current == bool(enabled):
        return list_skills(settings)
    target = active if enabled else parked
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(path), str(target))
    return list_skills(settings)


def delete(settings, name: str) -> list[dict]:
    """卸载一个**用户技能**（自带技能的 403 由 API 层先判，这里再兜一层）。"""
    _check_name(name)
    if is_builtin(settings, name):
        raise ValidationError(f"{name} 是自带技能：不能卸载，只能禁用")
    found = locate(settings, name)
    if found is None:
        raise NotFoundError(f"没有叫 {name} 的技能")
    path, _enabled = found
    shutil.rmtree(path, ignore_errors=True)
    manifest = _load_manifest(skills_root(settings))
    if manifest.pop(name, None) is not None:      # 理论上只有自带技能在表里，兜一层
        _save_manifest(skills_root(settings), manifest)
    return list_skills(settings)


def export_zip(settings, name: str) -> bytes:
    """把一个技能打包成 zip（`<name>/…` 结构，重新上传即可还原）。"""
    _check_name(name)
    found = locate(settings, name)
    if found is None:
        raise NotFoundError(f"没有叫 {name} 的技能")
    path, _enabled = found
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(path.rglob("*")):
            if item.is_file() and not item.is_symlink():
                zf.write(item, f"{name}/{item.relative_to(path).as_posix()}")
    return buffer.getvalue()
