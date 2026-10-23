"""「Cyli 偏好」页的后端支撑：**口吻预设 + 回复长度**（可组合的提示词片段）。

钉住四件事：

1. **默认配置逐字节不变**：`tests/golden/agent_system_default.txt` 是**改动前**的代码
   跑出来的 system 全文（`build_instructions` 默认 deps）。这一页只许"追加片段"，
   绝不许动到默认那一份 —— 它与前缀缓存绑定（从第 0 个 token 起比前缀）。
2. 预设只换「口吻」那半段：`IDENTITY` 与所有常驻规则块一个字都不动。
3. 长度只追加在 system 末尾：`normal` 是空串（= 什么都不加）。
4. 两项各自**冻结在会话行**上（`agent_style_preset` / `agent_reply_length`），
   老会话（列 NULL）按"谁更晚"判一次并写回。

片段每次都由常量拼出、每次调用只拼一次：跨轮累加就是把同一条要求叠第 N 遍，
既费 token，也会让模型把口吻看得比规则还重。
"""

from __future__ import annotations

import hashlib
import pathlib
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.agent import prompts
from backend.agent.deps import Deps

GOLDEN = pathlib.Path(__file__).parent / "golden" / "agent_system_default.txt"
API = "/api/v1/settings"


def _deps(**kw) -> Deps:
    """最小 deps：`build_instructions` 只看这几个字段。"""
    kws = dict(cfg=SimpleNamespace(max_rounds=60, deadline_seconds=600.0), host=None,
               paper_id="demo-paper", registry=None, page_total=12, title="Demo Paper")
    kws.update(kw)
    return Deps(**kws)  # type: ignore[arg-type]


def _prefs(**prefs) -> Deps:
    """带「Cyli 偏好」两项的 deps。

    这两个值最终由 `AskRequest` 带过 agent 侧（`Deps.style_preset` / `Deps.reply_length`）。
    这里用 setattr 挂上去，是为了让提示词的用例不依赖 dataclass 的字段声明 —— 字段本身
    的改动在交付给主后端的 diff 里（deps.py 此刻由另一位执行者占着）。
    """
    item = _deps()
    for key, value in prefs.items():
        setattr(item, key, value)
    return item


def _golden() -> str:
    return GOLDEN.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 1. 默认：逐字节等于改动前
# --------------------------------------------------------------------------


def test_default_system_prompt_is_byte_identical_to_the_golden() -> None:
    """默认配置（预设=默认、无自定义指令、长度=适中）必须与改动前**逐字节相同**。

    这条是这次改动的硬性验收：这一页只允许"往后追加"，默认那一份要是变了一个字节，
    所有历史会话的 `system + 全部历史` 前缀缓存下一轮就全部作废（从第 0 个 token 比）。
    """
    golden = _golden()
    text = prompts.build_instructions(_deps())
    assert text == golden
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == \
        hashlib.sha256(golden.encode("utf-8")).hexdigest()
    # 拆出来的两块拼回去必须就是原来那一整块风格（含那个换行）
    assert prompts.DEFAULT_STYLE == f"{prompts.IDENTITY}\n{prompts.DEFAULT_TONE}"
    assert text.startswith(prompts.DEFAULT_STYLE + "\n\n" + prompts.BASE)


def test_explicit_defaults_render_the_same_bytes() -> None:
    """把两项**显式**写成默认值（前端的下拉就是会带值过来），文本也不许多一个字节。"""
    assert prompts.build_instructions(
        _prefs(style_preset="default", reply_length="normal")) == _golden()
    # 脏值 / 老库里的怪值一律当默认（渲染层不抛错）
    assert prompts.build_instructions(
        _prefs(style_preset="catgirl", reply_length="long")) == _golden()


# --------------------------------------------------------------------------
# 2. 预设：只换口吻
# --------------------------------------------------------------------------


def test_preset_replaces_only_the_tone_fragment() -> None:
    golden = _golden()
    text = prompts.build_instructions(_prefs(style_preset="rigorous"))
    tone = prompts.TONE_FRAGMENTS["rigorous"]
    # 身份在前、口吻紧随其后（顺序固定：身份不许被预设顶掉）
    assert text.startswith(f"{prompts.IDENTITY}\n{tone}")
    assert text.count(tone) == 1
    assert prompts.DEFAULT_TONE not in text
    assert prompts.TONE_PRECEDENCE in text
    # 规则块一块不少
    for name in ("BASE", "SCOPE", "OFF_TOPIC", "ABSTRACT_RULES", "SELECTION_RULES",
                 "IMAGE_RULES", "CITING", "NO_EVIDENCE", "EDIT_RULES", "NOTES_RULE"):
        assert getattr(prompts, name) in text, f"换了口吻之后 {name} 不许丢"
    # 除了最前面那一块，后面与默认**一个字都不差**
    assert text.split(prompts.BASE, 1)[1] == golden.split(prompts.BASE, 1)[1]


def test_every_preset_has_its_own_tone() -> None:
    for name in ("rigorous", "concise", "plain"):
        text = prompts.build_instructions(_prefs(style_preset=name))
        assert text.count(prompts.TONE_FRAGMENTS[name]) == 1, name
        assert prompts.DEFAULT_TONE not in text, name
        assert text.startswith(prompts.IDENTITY), name


def test_custom_preset_uses_the_instruction_text() -> None:
    mine = "回答时先给一个表格，再解释。"
    text = prompts.build_instructions(_prefs(style_preset="custom", style=mine))
    assert text.startswith(f"{prompts.IDENTITY}\n{mine}")
    assert text.count(mine) == 1
    assert prompts.TONE_PRECEDENCE in text
    # 空白 → 回落到默认口吻（不是"没有口吻要求"：那会让模型失去语气设定）
    blank = prompts.build_instructions(_prefs(style_preset="custom", style="   "))
    assert blank.startswith(f"{prompts.IDENTITY}\n{prompts.DEFAULT_TONE}")
    # 上限照旧挡在 STYLE_MAX_CHARS
    long_text = prompts.build_instructions(
        _prefs(style_preset="custom", style="喵" * (prompts.STYLE_MAX_CHARS + 500)))
    assert "喵" * prompts.STYLE_MAX_CHARS in long_text
    assert "喵" * (prompts.STYLE_MAX_CHARS + 1) not in long_text


def test_custom_instruction_is_ignored_unless_the_preset_is_custom() -> None:
    """`agent_style` 只在 preset=custom 时生效 —— 这是这次定的口径。"""
    text = prompts.build_instructions(
        _prefs(style_preset="concise", style="回答时先给一个表格，再解释。"))
    assert "回答时先给一个表格" not in text
    assert prompts.TONE_FRAGMENTS["concise"] in text


def test_legacy_custom_style_still_takes_effect_as_tone() -> None:
    """**向后兼容**：老设置只有一段 `agent_style` 文本、没有 preset 行，那段文本仍然生效。

    行为是**有意改变**的：以前它整块替换身份段（system 第一句就是它），现在 IDENTITY
    保留、它变成追加在后面的口吻段 —— 一句自定义指令再也顶不掉身份与底线。
    """
    mine = "你是猫娘助手，每句话结尾都要说喵。"
    text = prompts.build_instructions(_deps(style=mine))
    assert text.startswith(f"{prompts.IDENTITY}\n{mine}")
    assert prompts.TONE_PRECEDENCE in text
    assert prompts.BASE in text
    # 老会话行上冻结的正是那段文本 → 渲染成 custom 口吻
    assert prompts.preset_of(_deps(style=mine)) == "custom"
    assert prompts.preset_of(_deps(style=prompts.DEFAULT_STYLE)) == "default"
    assert prompts.preset_of(_deps(style="")) == "default"


# --------------------------------------------------------------------------
# 3. 长度：只追加在末尾
# --------------------------------------------------------------------------


def test_reply_length_appends_once_at_the_end() -> None:
    golden = _golden()
    detailed = prompts.build_instructions(_prefs(reply_length="detailed"))
    assert detailed.count(prompts.LENGTH_FRAGMENTS["detailed"]) == 1
    assert detailed.startswith(golden), "除了末尾那一段，system 必须与默认逐字节一致"
    assert detailed.endswith(
        prompts.LENGTH_FRAGMENTS["detailed"] + "\n" + prompts.LENGTH_PRECEDENCE)
    short = prompts.build_instructions(_prefs(reply_length="short"))
    assert short.count(prompts.LENGTH_FRAGMENTS["short"]) == 1
    assert short.startswith(golden)
    # normal = 空片段 = 一个字节都不加
    assert prompts.LENGTH_FRAGMENTS["normal"] == ""
    assert prompts.build_instructions(_prefs(reply_length="normal")) == golden


def test_style_and_length_are_independent() -> None:
    """两项各自生效：只改长度时口吻那块不变，只改口吻时末尾不变。"""
    golden = _golden()
    style_head, style_tail = golden.split(prompts.BASE, 1)
    for length in ("short", "detailed"):
        text = prompts.build_instructions(_prefs(reply_length=length))
        assert text.split(prompts.BASE, 1)[0] == style_head
    for preset in ("rigorous", "concise", "plain"):
        text = prompts.build_instructions(_prefs(style_preset=preset))
        assert text.split(prompts.BASE, 1)[1] == style_tail


def test_both_preferences_together_and_nothing_accumulates() -> None:
    """两项同时改：两段都在、各自只出现一次；反复渲染不累加（片段每次现拼）。"""
    for _ in range(3):
        text = prompts.build_instructions(
            _prefs(style_preset="rigorous", reply_length="detailed"))
    assert text.count(prompts.TONE_FRAGMENTS["rigorous"]) == 1
    assert text.count(prompts.LENGTH_FRAGMENTS["detailed"]) == 1
    assert text.count(prompts.TONE_PRECEDENCE) == 1
    assert text.count(prompts.LENGTH_PRECEDENCE) == 1
    # 同一个 deps 连渲染三次：结果一样，deps 上也没被写回/累加出任何东西
    deps = _prefs(style_preset="concise", reply_length="short")
    snapshot = dict(vars(deps))
    first = prompts.build_instructions(deps)
    assert [prompts.build_instructions(deps) for _ in range(3)] == [first] * 3
    assert vars(deps) == snapshot


# --------------------------------------------------------------------------
# 4. 冻结：新建会话抄一份、老会话判一次写回（纯函数）
# --------------------------------------------------------------------------


def test_pick_preset_freezes_new_and_judges_old_sessions() -> None:
    from backend.services.agent import style as style_service

    t0 = datetime(2026, 12, 1, 10, 0, 0)
    later = t0 + timedelta(hours=1)
    # 行上有值（新会话）→ 冻结值，设置改了也追不到
    assert style_service.pick_preset("concise", "rigorous") == "concise"
    # 老会话（NULL）+ 会话建得**晚于**设置改动 → 它出生时就见的这一份
    assert style_service.pick_preset(
        None, "rigorous", created_at=later, changed_at=later) == "rigorous"
    # 更早 → 默认（它出生时还没有这个预设）
    assert style_service.pick_preset(
        None, "rigorous", created_at=t0, changed_at=later) == "default"
    # 从没设过预设 / 认不出的值 → 默认
    assert style_service.pick_preset(None, "", created_at=later, changed_at=later) == "default"
    assert style_service.pick_preset("catgirl", "", created_at=later,
                                    changed_at=later) == "default", "认不出的旧值 = 没判过"
    # 老语义兜底：升级前的会话行上没有 preset，只有当年那段自定义文本
    assert style_service.pick_preset(None, "", style_text="你是猫娘助手，喵。") == "custom"
    assert style_service.pick_preset(None, "", style_text=prompts.DEFAULT_STYLE) == "default"


def test_pick_length_freezes_new_and_judges_old_sessions() -> None:
    from backend.services.agent import style as style_service

    t0 = datetime(2026, 12, 1, 10, 0, 0)
    later = t0 + timedelta(hours=1)
    assert style_service.pick_length("short", "detailed") == "short"
    assert style_service.pick_length(
        None, "detailed", created_at=later, changed_at=later) == "detailed"
    assert style_service.pick_length(
        None, "detailed", created_at=t0, changed_at=later) == "normal"
    # 升级前根本没有长度这一项 → 老会话就是 normal
    assert style_service.pick_length(None, "", created_at=later) == "normal"
    # 冻结列上是认不出的脏值 → 当作"没判过"，判不出"谁更晚"就落 normal
    assert style_service.pick_length("long", "", created_at=later,
                                     changed_at=later) == "normal"


def test_stored_preset_reads_the_legacy_setting_only(monkeypatch) -> None:
    """老库只有 `agent_style` 一行、没有 preset 行 → 读成 custom；显式存的优先。"""
    from backend.services.agent import style as style_service
    from backend.services.knowledge import retrieval

    def patched(mapping: dict):
        return lambda key: mapping.get(key, retrieval._MISSING)

    monkeypatch.setattr(retrieval, "_setting_raw", patched({"agent_style": "你是猫娘助手，喵。"}))
    assert style_service.stored_preset() == "custom"
    assert style_service.stored_length() == ""
    assert style_service.effective_length() == "normal"

    monkeypatch.setattr(retrieval, "_setting_raw",
                        patched({"agent_style": "你是猫娘助手，喵。",
                                 "agent_style_preset": "Default"}))
    assert style_service.stored_preset() == "default", "显式存过的值优先于老语义兜底"

    monkeypatch.setattr(retrieval, "_setting_raw", patched({}))
    assert style_service.stored_preset() == ""
    assert style_service.effective_preset() == "default"


# --------------------------------------------------------------------------
# 5. 设置接口：往返 / 空串删行 / 非法值 400（真起 TestClient）
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db() -> None:
    """用例要写库（settings 表 / conversation 表）：没有可用数据库就整组跳过。"""
    try:
        from backend.repositories.mysql.models import Setting
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            sess.query(Setting).count()
    except Exception as exc:  # noqa: BLE001 —— 没有库的机器上这些用例不适用
        pytest.skip(f"没有可用的数据库：{exc}")

    from backend.repositories.mysql.session import ensure_columns

    # 补列机制本身就是被测对象之一（`_ADDED_COLUMNS`），这里先让它跑一遍
    ensure_columns()


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    import backend.main as main

    return TestClient(main.app)


@pytest.fixture
def clean_settings(db):
    """用例前后把这几行 setting **原样存回**：跑测试不许改掉这台机器上用户自己的偏好。"""
    from backend.repositories.mysql.models import Setting
    from backend.repositories.mysql.session import session_scope

    keys = ("agent_style", "agent_style_preset", "agent_reply_length")
    saved: dict[str, str | None] = {}
    with session_scope() as sess:
        for key in keys:
            row = sess.get(Setting, key)
            saved[key] = None if row is None else row.value
        # 先清干净，用例才有确定的起点（`PUT` 本来就靠"删行 = 恢复默认"）
        for key in keys:
            for name in (key, f"kb.{key}"):
                row = sess.get(Setting, name)
                if row is not None:
                    sess.delete(row)
    yield saved
    with session_scope() as sess:
        for key, value in saved.items():
            row = sess.get(Setting, key)
            if value is None:
                if row is not None:
                    sess.delete(row)
            elif row is None:
                sess.add(Setting(key=key, value=value))
            else:
                row.value = value


def test_settings_round_trip_and_reset(client, clean_settings) -> None:
    response = client.put(API, json={"agent_style_preset": "rigorous",
                                     "agent_reply_length": "short",
                                     "agent_style": "回答时先给一个表格。"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["applied"]["agent_style_preset"] == "rigorous"
    assert data["applied"]["agent_reply_length"] == "short"

    got = client.get(API).json()["data"]
    assert got["values"]["agent_style_preset"] == "rigorous"
    assert got["values"]["agent_reply_length"] == "short"
    assert got["values"]["agent_style"] == "回答时先给一个表格。"
    assert got["stored"]["agent_style_preset"] == "rigorous"
    assert got["defaults"]["agent_style_preset"] == "default"
    assert got["defaults"]["agent_reply_length"] == "normal"

    # 空串 = 删行 + 回落默认（不是存一个空值）
    response = client.put(API, json={"agent_style": "", "agent_style_preset": "",
                                     "agent_reply_length": ""})
    assert response.status_code == 200, response.text
    got = client.get(API).json()["data"]
    assert "agent_style" not in got["stored"]
    assert "agent_style_preset" not in got["stored"]
    assert "agent_reply_length" not in got["stored"]
    assert got["values"]["agent_style"] == got["defaults"]["agent_style"]
    assert got["values"]["agent_style_preset"] == "default"
    assert got["values"]["agent_reply_length"] == "normal"


def test_invalid_enum_values_are_rejected_with_400(client, clean_settings) -> None:
    cases = (("agent_style_preset", "catgirl",
              "default / rigorous / concise / plain / custom"),
             ("agent_reply_length", "long", "short / normal / detailed"))
    for key, bad, allowed in cases:
        response = client.put(API, json={key: bad})
        assert response.status_code == 400, response.text
        message = response.json()["error"]["message"]
        assert key in message and allowed in message
    # 一条都没写进去（校验是在写库之前做的）
    got = client.get(API).json()["data"]
    assert "agent_style_preset" not in got["stored"]
    assert "agent_reply_length" not in got["stored"]


def test_frozen_preferences_are_written_back_once(client, clean_settings) -> None:
    """同一会话第二次取仍用**冻结**值；老会话（列 NULL）判一次后写回，之后设置改了也不动。"""
    from backend.repositories.mysql.models import Conversation, Setting
    from backend.repositories.mysql.session import session_scope
    from backend.services.agent import conversations
    from backend.services.agent import style as style_service

    conversation_id = str(uuid.uuid4())        # conversation.id 就是 36 位 uuid
    conversations.create_conversation(conversation_id, paper_id="demo-paper")
    try:
        with session_scope() as sess:
            row = sess.get(Conversation, conversation_id)
            # 建会话时就把"此刻这两项"抄了进去
            assert row.agent_style_preset == style_service.effective_preset()
            assert row.agent_reply_length == style_service.effective_length()
            # 打回升级前的样子：这两列还是 NULL
            row.agent_style_preset = None
            row.agent_reply_length = None

        first = conversations.frozen_preferences(conversation_id)
        # 键名就是 api/agent.py 直接展开进请求体的那一份：agent 侧按
        # deps.style / style_preset / reply_length 读（见交付给主后端的 diff）
        assert set(first) == {"agent_style", "agent_style_preset", "agent_reply_length"}
        assert first["agent_style_preset"] in prompts.STYLE_PRESETS
        assert first["agent_reply_length"] in prompts.REPLY_LENGTHS
        with session_scope() as sess:                      # 判一次就写回了
            row = sess.get(Conversation, conversation_id)
            assert row.agent_style_preset == first["agent_style_preset"]
            assert row.agent_reply_length == first["agent_reply_length"]

        # 设置改成另外一组之后，同一条会话仍然用它冻结的那一份
        from backend.core.utils import json_dumps

        with session_scope() as sess:
            for key, value in (("agent_style_preset", "rigorous"),
                               ("agent_reply_length", "detailed")):
                row = sess.get(Setting, key)
                if row is None:
                    sess.add(Setting(key=key, value=json_dumps(value)))
                else:
                    row.value = json_dumps(value)
        assert conversations.frozen_preferences(conversation_id) == first
    finally:
        with session_scope() as sess:
            row = sess.get(Conversation, conversation_id)
            if row is not None:
                sess.delete(row)
