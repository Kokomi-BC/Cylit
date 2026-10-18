"""技能管理（上传/导出/启停/卸载）+ 问答运行限额：契约与硬闸。

这些用例全部离线：技能库落到 `tmp_path`（一份真的自带技能会被物化进去），
限额只碰临时目录里的两个 json，agent 进程的连接被替换成一个空操作。

钉死的东西：

1. frontmatter 解析（名字/描述 —— 描述常驻提示词，缺了技能就是死文件）；
2. zip 路径穿越（`..` / 绝对路径）必须被拒；
3. 与自带技能重名必须被拒（自带技能不能被用户包覆盖）；
4. 自带技能**不能卸载**（403）、但可以禁用；
5. 禁用/启用是"搬目录"（agent 的扫描器只认 `<dir>/<name>/SKILL.md`）；
6. 限额越界 → 400，且错误里说清范围；
7. `restart_required` 的语义：保存后为真、agent 写下 applied 后为假。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import backend.api.agent as api_agent
from backend.core.config import AGENT_LIMITS, Settings
from backend.core.errors import PaperMindError, ProviderError, ValidationError
from backend.services.agent import skills_store

LIMITS = {"max_rounds": 60, "search_budget": 5, "deadline_seconds": 600,
          "context_budget": 200000, "compact_threshold": 90, "image_max_pages": 3}


def _settings(tmp_path: Path) -> Settings:
    """一个把数据目录指到临时目录的设置（技能库跟着 data_dir 走，见模型校验器）。"""
    cfg = Settings(data_dir=tmp_path)
    assert cfg.agent_skills_dir == tmp_path / "skills"
    return cfg


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buffer.getvalue()


def _skill_md(name: str, description: str = "演示用技能") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n正文\n"


# --------------------------------------------------------------------------
# 1. frontmatter
# --------------------------------------------------------------------------


def test_parse_frontmatter() -> None:
    name, description = skills_store.parse_frontmatter(
        _skill_md("demo-skill", "什么时候用这个技能"))
    assert (name, description) == ("demo-skill", "什么时候用这个技能")


def test_builtin_skills_materialized_with_descriptions(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    manifest = skills_store.ensure_builtin_skills(cfg)
    assert "paper-qa" in manifest, "仓库里的自带技能没有被物化进技能库"

    skills = skills_store.list_skills(cfg)
    assert len(skills) >= 9
    assert all(item["builtin"] and item["enabled"] for item in skills)
    assert all(item["description"] for item in skills), "自带技能的 description 应当都解析得出来"
    assert all(item["chars"] > 0 for item in skills)
    # manifest 记的是内容哈希（用来认"这是自带技能" + "用户改过没有"）
    assert all(len(value) == 64 for value in manifest.values())


# --------------------------------------------------------------------------
# 2. zip 硬闸
# --------------------------------------------------------------------------


def test_zip_path_traversal_rejected(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    data = _zip({"../evil/SKILL.md": _skill_md("evil")})
    with pytest.raises(ValidationError) as excinfo:
        skills_store.upload(cfg, data, filename="evil.zip")
    assert "穿越" in excinfo.value.message or ".." in excinfo.value.message
    assert not (tmp_path / "evil").exists()


def test_zip_absolute_path_rejected(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    with pytest.raises(ValidationError):
        skills_store.upload(cfg, _zip({"/etc/evil/SKILL.md": _skill_md("evil")}),
                            filename="evil.zip")


def test_zip_symlink_entry_rejected(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo("evil-skill/SKILL.md")
        info.external_attr = (0o120777 << 16)          # 符号链接
        zf.writestr(info, _skill_md("evil-skill"))
    with pytest.raises(ValidationError) as excinfo:
        skills_store.upload(cfg, buffer.getvalue(), filename="evil.zip")
    assert "符号链接" in excinfo.value.message


def test_zip_without_skill_md_rejected(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    with pytest.raises(ValidationError):
        skills_store.upload(cfg, _zip({"demo-skill/README.md": "没有 SKILL.md"}),
                            filename="demo.zip")


def test_zip_too_many_entries_rejected(tmp_path: Path) -> None:
    """条目数封顶：技能包是给模型看的说明文本，不是数据集。"""
    cfg = _settings(tmp_path)
    files = {"big-skill/SKILL.md": _skill_md("big-skill")}
    for index in range(skills_store.MAX_ENTRIES + 1):
        files[f"big-skill/note-{index}.md"] = "x"
    with pytest.raises(ValidationError) as excinfo:
        skills_store.upload(cfg, _zip(files), filename="big.zip")
    assert "文件数" in excinfo.value.message


# --------------------------------------------------------------------------
# 3. 与自带技能重名
# --------------------------------------------------------------------------


def test_upload_with_builtin_name_rejected(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    skills_store.ensure_builtin_skills(cfg)
    data = _zip({"paper-qa/SKILL.md": _skill_md("paper-qa")})
    with pytest.raises(ValidationError) as excinfo:
        skills_store.upload(cfg, data, filename="paper-qa.zip")
    assert "自带技能" in excinfo.value.message
    # 自带的那份没被覆盖
    text = (cfg.agent_skills_dir / "paper-qa" / "SKILL.md").read_text(encoding="utf-8")
    assert "演示用技能" not in text


# --------------------------------------------------------------------------
# 4. 启用 / 禁用（搬目录）
# --------------------------------------------------------------------------


def test_disable_and_enable_move_directory(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    skills_store.ensure_builtin_skills(cfg)
    root = cfg.agent_skills_dir

    disabled = skills_store.set_enabled(cfg, "paper-qa", False)
    assert (root / ".disabled" / "paper-qa" / "SKILL.md").is_file()
    assert not (root / "paper-qa").exists()
    entry = next(item for item in disabled if item["name"] == "paper-qa")
    assert entry["enabled"] is False and entry["builtin"] is True

    enabled = skills_store.set_enabled(cfg, "paper-qa", True)
    assert (root / "paper-qa" / "SKILL.md").is_file()
    assert not (root / ".disabled" / "paper-qa").exists()
    assert next(i for i in enabled if i["name"] == "paper-qa")["enabled"] is True


# --------------------------------------------------------------------------
# 5. 上传 → 导出 → 卸载 的往返
# --------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    """只挂 agent 路由的小应用：技能接口不该拖上数据库与任务系统。"""
    cfg = _settings(tmp_path)
    monkeypatch.setattr(api_agent, "get_settings", lambda: cfg)

    async def _agent_down(*_args, **_kwargs):      # agent 进程没起 → 重载必然失败
        raise ProviderError("问答服务没有在运行", retryable=True)

    monkeypatch.setattr(api_agent.bridge, "agent_post", _agent_down)

    app = FastAPI()
    app.include_router(api_agent.router, prefix=cfg.api_prefix)

    @app.exception_handler(PaperMindError)
    async def _handle(_request, exc: PaperMindError):
        return JSONResponse(status_code=exc.http_status,
                            content={"data": None, "error": exc.to_dict(), "trace_id": ""})

    return TestClient(app), cfg


def test_upload_download_delete_roundtrip(client) -> None:
    http, cfg = client
    data = _zip({"demo-skill/SKILL.md": _skill_md("demo-skill")})

    listed = http.get("/api/v1/agent/skills")
    assert listed.status_code == 200
    assert listed.json()["data"]["dir"] == str(cfg.agent_skills_dir)

    uploaded = http.post("/api/v1/agent/skills/upload",
                         files={"file": ("demo-skill.zip", data, "application/zip")})
    assert uploaded.status_code == 200, uploaded.text
    payload = uploaded.json()["data"]
    assert payload["added"] == ["demo-skill"]
    assert any(item["name"] == "demo-skill" and not item["builtin"]
               for item in payload["skills"])
    # agent 没起 → 接口照常成功，只是带一句"没重载"的提示
    assert "没有重载" in (uploaded.json()["message"] or "")

    downloaded = http.get("/api/v1/agent/skills/demo-skill/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/zip"
    assert downloaded.content[:2] == b"PK"

    toggled = http.put("/api/v1/agent/skills/demo-skill", json={"enabled": False})
    assert toggled.status_code == 200
    assert next(i for i in toggled.json()["data"]["skills"]
                if i["name"] == "demo-skill")["enabled"] is False

    deleted = http.delete("/api/v1/agent/skills/demo-skill")
    assert deleted.status_code == 200
    assert all(item["name"] != "demo-skill" for item in deleted.json()["data"]["skills"])


def test_builtin_skill_cannot_be_deleted(client) -> None:
    http, cfg = client
    skills_store.ensure_builtin_skills(cfg)
    response = http.delete("/api/v1/agent/skills/paper-qa")
    assert response.status_code == 403
    assert "自带技能" in response.json()["detail"]
    assert (cfg.agent_skills_dir / "paper-qa" / "SKILL.md").is_file()


def test_put_requires_enabled_flag(client) -> None:
    http, cfg = client
    skills_store.ensure_builtin_skills(cfg)
    response = http.put("/api/v1/agent/skills/paper-qa", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------
# 6. 运行限额：字段对应、越界 400、restart_required 语义
# --------------------------------------------------------------------------


def test_limit_keys_map_to_real_config_fields() -> None:
    """契约键与 `Settings` 字段的对应表就是这一份（前端按左边的名字写死）。"""
    assert {key: spec[0] for key, spec in AGENT_LIMITS.items()} == {
        "max_rounds": "agent_max_rounds",
        "search_budget": "agent_search_budget",
        "deadline_seconds": "agent_deadline_seconds",
        "context_budget": "agent_context_budget",
        "compact_threshold": "agent_auto_compress_percent",
        "image_max_pages": "agent_image_max_pages",
    }


def test_limits_out_of_range_rejected(client) -> None:
    http, _cfg = client
    for patch, expect in (
        ({"max_rounds": 5000}, "1~200"),
        ({"search_budget": -1}, "0~50"),
        ({"deadline_seconds": 10}, "30~3600"),
        ({"context_budget": 1000}, "8000~1000000"),
        ({"compact_threshold": 99.5}, "50~99"),
        ({"image_max_pages": 0}, "1~50"),
    ):
        response = http.put("/api/v1/agent/limits", json={**LIMITS, **patch})
        assert response.status_code == 400, (patch, response.text)
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert expect in body["error"]["message"]


def test_limits_saved_and_read_back(client) -> None:
    http, cfg = client
    response = http.put("/api/v1/agent/limits", json={**LIMITS, "max_rounds": 40})
    assert response.status_code == 200
    assert response.json()["data"]["restart_required"] is True

    saved = json.loads((cfg.data_dir / "agent_limits.json").read_text(encoding="utf-8"))
    assert saved["values"]["max_rounds"] == 40 and saved["saved_at"]

    current = http.get("/api/v1/agent/limits")
    assert current.status_code == 200
    data = current.json()["data"]
    assert data["max_rounds"] == 40
    assert set(data) == {*LIMITS, "restart_required"}
    assert data["restart_required"] is True       # 还没有 agent 进程读过它


def test_restart_required_semantics(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    # 没保存过任何覆盖值 → 不需要重启
    assert cfg.agent_limits_restart_required() is False

    cfg.save_agent_limits({"max_rounds": 40})
    assert cfg.agent_limits_restart_required() is True

    # 模拟 agent 进程启动：读进去、并写下"生效于此刻"
    fresh = _settings(tmp_path)
    fresh.apply_agent_limits()
    assert fresh.agent_max_rounds == 40
    fresh.mark_agent_limits_applied()
    assert fresh.agent_limits_restart_required() is False

    applied = json.loads(
        (tmp_path / "agent_limits.applied.json").read_text(encoding="utf-8"))
    assert applied["values"]["max_rounds"] == 40 and applied["applied_at"]
