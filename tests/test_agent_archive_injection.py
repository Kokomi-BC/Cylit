"""论文背景**首轮自动注入**：注入点、幂等、换论文、降级（零模型调用、不联网）。

用户口径 2026-12-11（把 2026-10-07 那条"agent 自己按需取档案"改回来）：
新会话的**第一次**模型调用之前就把**真实档案数据**放到位 —— 只放理解本文必需的那几项
（标题 / 篇幅 / 领域与主要论断 / 章节结构 / 统一译法），只注入**一次**，走**用户消息**
（放进 system 会让第 2 轮的整段前缀缓存作废），第 2 轮起它就在历史里。

这里锁住的是 `runtime._fetch_archive` + `runtime._inject_archive` + `prompts.archive_background`
这条链：数据复用"这一次已经取到的档案"（不多打一次 HTTP），判定在首轮，幂等在 paper_id 上。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from backend.agent import prompts
from backend.agent.deps import Deps

pytest.importorskip("pydantic_ai")

from backend.agent.runtime import AgentRuntime, AskRequest  # noqa: E402

HEADER = prompts.ARCHIVE_INJECT_HEADER


def _archive(pid: str = "A", claim: str = "随机旋转让内积量化误差在各坐标间均匀") -> dict:
    """一份够真实的档案：标题 / 篇幅 / 摘要 / 领域 / 主要论断 / 译法 / 章节结构。"""
    return {
        "title": f"TurboQuant-{pid}",
        "page_total": 24,
        "meta": {"abstract": "We propose TurboQuant.", "abstract_zh": "我们提出 TurboQuant。"},
        "brief": {
            "domain": "向量量化（VQ）与近似最近邻检索",
            "key_claims": [claim, "量化误差与旋转不变性"],
            "glossary": {"random rotation": "随机旋转"},
            "keep_as_is": ["QJL"],
        },
        "outline": [
            {"text": "1 INTRODUCTION", "text_zh": "1 引言", "level": 1},
            {"text": "2 METHOD", "text_zh": "2 方法", "level": 1},
            {"text": "3 EXPERIMENTS", "text_zh": "3 实验", "level": 2},
        ],
    }


class FakeHost:
    """只实现 `archive`：这条链路不该碰宿主的别的东西。"""

    def __init__(self, archive: dict | None = None, raises: Exception | None = None) -> None:
        self._archive = archive or {}
        self._raises = raises
        self.calls = 0

    async def archive(self, paper_id: str) -> dict:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._archive


def _runtime(tmp_path, archive: dict | None = None,
             raises: Exception | None = None) -> tuple[AgentRuntime, FakeHost]:
    """最小可运行的 runtime：skills 目录指向 tmp_path（不碰真实文件）。"""
    cfg = SimpleNamespace(skills_dir=str(tmp_path), archive_chars=6000,
                          max_rounds=60, deadline_seconds=600.0)
    host = FakeHost(archive, raises)
    return AgentRuntime(cfg, host), host  # type: ignore[arg-type]


def _deps(**kw) -> Deps:
    kws = dict(cfg=SimpleNamespace(archive_chars=6000), host=None, paper_id="paper-a",
               registry=None, page_total=24, title="Demo Paper")
    kws.update(kw)
    return Deps(**kws)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# a) 首轮注入发生：档案文本真的出现在首轮的用户消息里
# --------------------------------------------------------------------------


def test_first_turn_injects_the_archive_into_the_user_prompt(tmp_path) -> None:
    rt, host = _runtime(tmp_path, _archive())
    # 摘要两段由 `ask()` 从 archive["meta"] 抄到 deps 上，这里照做（同一份 archive）
    deps = _deps(first_turn=True, abstract_en="We propose TurboQuant.",
                 abstract_zh="我们提出 TurboQuant。")
    archive = asyncio.run(rt._fetch_archive("paper-a"))

    rt._inject_archive(deps, archive)
    assert deps.archive_injected == "paper-a"

    notes = prompts.turn_notes(deps)
    assert HEADER in notes, "首轮必须带上'只这一次、不可引用'的小标题"
    # 必需字段：标题 / 篇幅 / 领域 / 主要论断 / 章节结构 / 统一译法
    assert "标题：Demo Paper" in notes
    assert "篇幅：共 24 页" in notes
    assert "领域与核心方法：向量量化（VQ）与近似最近邻检索" in notes
    assert "随机旋转让内积量化误差在各坐标间均匀" in notes
    assert "章节结构：" in notes and "2 METHOD｜2 方法" in notes
    assert "统一译法：random rotation→随机旋转" in notes
    # 首轮摘要还在（两段并存，不互相顶掉）
    assert "中文摘要（译文）" in notes

    # 真实链路：`_user_prompt` 把这一串拼在**问题之前**的那条用户消息里
    prompt = asyncio.run(rt._user_prompt(AskRequest(paper_id="paper-a", question="这篇讲了什么"), deps))
    assert HEADER in prompt
    assert "【用户这一轮的问题】" in prompt and "这篇讲了什么" in prompt
    # 数据复用：注入没有再去问一次宿主
    assert host.calls == 1


def test_archive_is_not_repeated_every_turn_and_is_idempotent(tmp_path, monkeypatch) -> None:
    """同一个 deps 里同一篇论文**只渲染一次**（第 2 次调用直接返回已渲染的那份）。"""
    rendered: list[str] = []
    real = prompts.archive_block

    def spy(*args, **kwargs):
        rendered.append(kwargs.get("header") or "")
        return real(*args, **kwargs)

    monkeypatch.setattr(prompts, "archive_block", spy)
    rt, _ = _runtime(tmp_path, _archive())
    deps = _deps(first_turn=True)
    first = rt._inject_archive(deps, _archive())
    again = rt._inject_archive(deps, _archive())
    assert first == again and rendered == [HEADER]


# --------------------------------------------------------------------------
# b) 首轮之外不再注入
# --------------------------------------------------------------------------


def test_second_turn_never_injects(tmp_path) -> None:
    rt, _ = _runtime(tmp_path, _archive())
    deps = _deps(first_turn=False)          # 有 history 的那一轮
    assert rt._inject_archive(deps, _archive()) == ""
    assert deps.archive_injected == ""
    assert prompts.turn_notes(deps) == ""

    # 就算有人把文本硬塞进 deps，"不是首轮"这一条仍然压住它
    deps.archive_text = "【论文背景】…"
    deps.archive_injected = deps.paper_id
    assert prompts.archive_background(deps) == ""
    assert prompts.turn_notes(deps) == ""


# --------------------------------------------------------------------------
# c) 两条会话互不影响
# --------------------------------------------------------------------------


def test_two_sessions_do_not_leak_into_each_other(tmp_path) -> None:
    rt, _ = _runtime(tmp_path, _archive())
    one, two = _deps(first_turn=True), _deps(first_turn=True, paper_id="paper-b")
    rt._inject_archive(one, _archive("A", "A 的论断"))
    rt._inject_archive(two, _archive("B", "B 的论断"))
    assert one.archive_injected == "paper-a" and two.archive_injected == "paper-b"
    assert "A 的论断" in prompts.turn_notes(one)
    assert "B 的论断" not in prompts.turn_notes(one)
    assert "B 的论断" in prompts.turn_notes(two)
    assert "A 的论断" not in prompts.turn_notes(two)


# --------------------------------------------------------------------------
# d) 换论文：重新注入，不沿用上一份
# --------------------------------------------------------------------------


def test_switching_paper_reinjects_without_carrying_the_old_one(tmp_path, monkeypatch) -> None:
    rendered: list[int] = []
    real = prompts.archive_block

    def spy(*args, **kwargs):
        rendered.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(prompts, "archive_block", spy)

    rt, _ = _runtime(tmp_path, _archive())
    deps = _deps(first_turn=True)
    rt._inject_archive(deps, _archive("A", "A 的论断"))
    assert "A 的论断" in deps.archive_text and len(rendered) == 1

    rt._inject_archive(deps, _archive("A", "A 的论断"))      # 同一篇：幂等
    assert len(rendered) == 1

    deps.paper_id = "paper-b"                                # 换论文
    rt._inject_archive(deps, _archive("B", "B 的论断"))
    assert len(rendered) == 2
    assert deps.archive_injected == "paper-b"
    text = prompts.archive_background(deps)
    assert "B 的论断" in text and "A 的论断" not in text


# --------------------------------------------------------------------------
# e) 降级：没有论文 / 档案为空 / 宿主报错 —— 不注入、不抛错
# --------------------------------------------------------------------------


def test_no_paper_id_and_empty_archive_degrade_quietly(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    rt, _ = _runtime(tmp_path)
    # 没有 paper_id
    assert rt._inject_archive(_deps(first_turn=True, paper_id=""), _archive()) == ""
    # 档案为空（只剩一行小标题没有信息，也不该注入）
    deps = _deps(first_turn=True, title="", page_total=0)
    assert rt._inject_archive(deps, {}) == ""
    assert deps.archive_text == "" and deps.archive_injected == ""
    assert prompts.turn_notes(deps) == ""
    assert any("不注入背景" in r.getMessage() for r in caplog.records), "降级要留一行 log.info"


def test_host_archive_failure_does_not_raise(tmp_path) -> None:
    rt, host = _runtime(tmp_path, raises=RuntimeError("宿主 502"))
    assert asyncio.run(rt._fetch_archive("paper-a")) == {}
    assert host.calls == 1
    # 拿不到档案 → 不注入，笔记为空，不炸
    deps = _deps(first_turn=True, title="", page_total=0)
    assert rt._inject_archive(deps, {}) == ""
    assert prompts.turn_notes(deps) == ""
