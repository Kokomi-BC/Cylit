"""页级翻译管线测试：prompt 契约 / 两份 Markdown 的行对齐 / 页产物落盘 / mock 形状。

不依赖数据库与网络：只验证"页级调用 → 对齐 → 落盘"这一层的契约。
端到端（需要 MySQL）的部分放在 test_paper_service.py 与 scripts/e2e_check.py。
"""

from __future__ import annotations

import json

from backend.providers.generation.mock import MockGeneration
from backend.services.translation.brief import DocumentBrief
from backend.services.translation.page_translate import (
    MAX_OUTPUT_TOKENS, PAGES_FILENAME, PageCallResult, PageStore, build_page_payload,
    build_stable_prefix, call_page, effective_max_tokens, history_context,
    mark_rows_continuity, next_max_tokens, pair_rows, parse_pair,
    render_page_prompt, split_blocks, _row_key,
)

PAGE_MD = """# TurboQuant: Online Vector Quantization

## 2.1 MSE Optimal TurboQuant

We find optimal scalar quantizers for random variables with a Beta
distribution, minimizing the MSE distortion.

$$ D_{mse} := \\mathbb{E}\\|x - Q(x)\\|_2^2 $$

Theorem 1 states that the distortion bound is 2.7207 for any bit-width $b \\geq 1$.

**Figure 2**: MSE distortion of TurboQuant compared with PQ and QJL.
"""

PAGE_MD_ZH = """# TurboQuant：在线向量量化

## 2.1 MSE 最优 TurboQuant

我们为服从 Beta 分布的随机变量找到了最优标量量化器，使 MSE 失真最小。

$$ D_{mse} := \\mathbb{E}\\|x - Q(x)\\|_2^2 $$

定理 1 指出：对任意位宽 $b \\geq 1$，失真上界为 2.7207。

**图 2**：TurboQuant 与 PQ、QJL 的 MSE 失真对比。
"""


def _result(original: str = PAGE_MD, translation: str = PAGE_MD_ZH) -> PageCallResult:
    return PageCallResult(original_md=original, translation_md=translation,
                          model="mock-vision", prompt_tokens=100,
                          completion_tokens=200, latency_ms=12.0, max_tokens=8192)


# --------------------------------------------------------------------------
# 1. prompt：稳定前缀 + 页内文本 + JSON 契约
# --------------------------------------------------------------------------


def test_stable_prefix_is_byte_identical_for_same_inputs():
    brief = DocumentBrief(paper_id="p", domain="向量量化", style="学术",
                          glossary={"quantization": "量化"})
    a = build_stable_prefix(brief, "- quantization → 量化")
    b = build_stable_prefix(brief, "- quantization → 量化")
    assert a == b and "向量量化" in a and "量化" in a


def test_render_page_prompt_carries_body_and_json_contract():
    prompt = render_page_prompt(page=3, page_total=22, body="## 2 Method\n\nBody.",
                                prefix="【文档简报】…", section="2 Method")
    assert "第 3 页 / 共 22 页" in prompt
    assert "章节：2 Method" in prompt
    assert "<页面文本>" in prompt and "Body." in prompt
    assert "original_md" in prompt and "translation_md" in prompt


def test_render_page_prompt_without_text_says_use_image():
    prompt = render_page_prompt(page=1, page_total=1, body="")
    assert "请完全依据页图" in prompt


def test_stable_prefix_carries_outline_skeleton():
    """骨架在稳定前缀里（前缀缓存仍然命中），且跟着简报/术语表。"""
    brief = DocumentBrief(paper_id="p", domain="向量量化")
    skeleton = "【全文标题骨架】\n# 1 EXPERIMENTS\n## 1.1 EMPIRICAL VALIDATION"
    prefix = build_stable_prefix(brief, "- quantization → 量化", skeleton=skeleton)
    assert "1 EXPERIMENTS" in prefix and prefix.endswith(skeleton)
    # 不传骨架时不能多出一段空行
    assert build_stable_prefix(brief, "- quantization → 量化") == build_stable_prefix(
        brief, "- quantization → 量化", skeleton="")


def test_render_page_prompt_carries_previous_context():
    prompt = render_page_prompt(page=6, page_total=22, body="Body.",
                                previous="【前文结尾】上一页的最后一句")
    assert "上一页的最后一句" in prompt
    # 前文必须在【本页】之前（前缀缓存的稳定段在前、动态段在后）
    assert prompt.index("上一页的最后一句") < prompt.index("【本页】")
    assert "上一页的最后一句" not in render_page_prompt(page=6, page_total=22, body="Body.")


def test_history_context_is_complete_and_append_only(tmp_path):
    """前文必须**完整**（第 1..N-1 页整页）且**追加式**（升序、模板逐字相同、表头不带页码）。"""
    store = PageStore("p", tmp_path / PAGES_FILENAME)
    for page in (1, 2, 3):
        store.put(page, build_page_payload(
            page=page, page_total=4,
            result=_result(original=f"ORIGINAL-OF-PAGE-{page}",
                           translation=f"TRANSLATION-OF-PAGE-{page}"),
            source_chars=10))

    text = history_context(store, "p", 4)
    for page in (1, 2, 3):                                  # 完整：前几页一个不少
        assert f"ORIGINAL-OF-PAGE-{page}" in text
        assert f"TRANSLATION-OF-PAGE-{page}" in text
    assert text.index("PAGE-1") < text.index("PAGE-2") < text.index("PAGE-3")   # 升序
    assert "不要" in text and "完整内容" in text
    assert history_context(store, "p", 1) == ""             # 第一页没有前文


def test_next_page_prompt_extends_the_previous_one(tmp_path):
    """缓存命中的充要条件：第 N+1 页的 prompt 以第 N 页的"稳定前缀 + 历史"开头。

    这是本设计的核心性质 —— 历史只要完整且逐页追加，前文就全部命中前缀缓存；
    一旦改成"最近几页的尾巴"或让表头带上页码，公共前缀会被截断，缓存就白丢。
    """
    store = PageStore("p", tmp_path / PAGES_FILENAME)
    for page in (1, 2, 3):
        store.put(page, build_page_payload(
            page=page, page_total=4,
            result=_result(original=f"原始第{page}页", translation=f"译文第{page}页"),
            source_chars=10))

    prefix = build_stable_prefix(DocumentBrief(paper_id="p", domain="向量量化"),
                                 "- quantization → 量化", skeleton="【全文标题骨架】\n# 1 EXPERIMENTS")

    def prompt(page_no: int) -> str:
        return render_page_prompt(page=page_no, page_total=4, body=f"本页正文{page_no}",
                                  prefix=prefix,
                                  previous=history_context(store, "p", page_no))

    p3, p4 = prompt(3), prompt(4)
    history_of_3 = history_context(store, "p", 3)     # 第 3 页看到的历史（第 1~2 页）
    head = prefix + "\n\n" + history_of_3
    assert p3.startswith(head) and p4.startswith(head)
    # 下一句：第 3 页的**整页内容**又成为第 4 页历史的一部分（追加式）
    assert history_context(store, "p", 4).endswith(
        "<第 3 页｜译文>\n译文第3页\n</第 3 页｜译文>")


def test_history_context_stops_at_the_first_gap(tmp_path):
    """并发场景：只取紧邻且连续完成的页，遇到缺口就停（避免"缺一页"的错位拼接）。"""
    store = PageStore("p", tmp_path / PAGES_FILENAME)
    for page in (1, 3, 4):        # 第 2 页还没落盘
        store.put(page, build_page_payload(
            page=page, page_total=5,
            result=_result(original=f"原始第{page}页", translation=f"译文第{page}页"),
            source_chars=10))

    text = history_context(store, "p", 5)
    assert "原始第4页" in text and "原始第3页" in text
    assert "原始第1页" not in text     # 被缺口（第 2 页）挡住


def test_history_context_has_overflow_guard(tmp_path):
    """超长论文的兜底：超上限就从**最早的页**开始丢，保住最近的内容。"""
    store = PageStore("p", tmp_path / PAGES_FILENAME)
    for page in (1, 2, 3, 4):
        store.put(page, build_page_payload(
            page=page, page_total=5,
            result=_result(original="x" * 4000 + f" tail-of-page{page}", translation=f"译文{page}"),
            source_chars=10))

    text = history_context(store, "p", 5, max_chars=4200)
    assert "tail-of-page4" in text
    assert "tail-of-page1" not in text
    assert len(text) < 4200 + 120                          # 表头 + 溢出裁剪后的正文


# --------------------------------------------------------------------------
# 2. 解析模型回复
# --------------------------------------------------------------------------


def test_parse_pair_plain_json():
    text = json.dumps({"original_md": "EN", "translation_md": "ZH"})
    assert parse_pair(text) == ("EN", "ZH")


def test_parse_pair_with_code_fence_and_prose():
    text = ("好的，这是结果：\n```json\n"
            + json.dumps({"original_md": "EN", "translation_md": "ZH"})
            + "\n```\n以上。")
    assert parse_pair(text) == ("EN", "ZH")


def test_parse_pair_falls_back_to_markers():
    assert parse_pair("【原文】EN\n【译文】ZH") == ("EN", "ZH")


# --------------------------------------------------------------------------
# 3. 块切分与行对齐
# --------------------------------------------------------------------------


def test_split_blocks_types_and_levels():
    blocks = split_blocks(PAGE_MD)
    assert [b["type"] for b in blocks] == [
        "heading", "heading", "para", "formula", "para", "caption"]
    assert blocks[0]["level"] == 1 and blocks[0]["number"] == ""
    assert blocks[1]["level"] == 2 and blocks[1]["number"] == "2.1"
    assert blocks[3]["text"].startswith("$$")


def test_pair_rows_exact_when_block_counts_match():
    rows, quality = pair_rows(PAGE_MD, PAGE_MD_ZH, page=3)
    assert quality == "exact"
    assert len(rows) == 6
    assert rows[0]["id"] == "p003-00"
    assert rows[0]["original"] == "# TurboQuant: Online Vector Quantization"
    assert rows[0]["translation"] == "# TurboQuant：在线向量量化"
    # 公式两侧完全一致（模型不该改 LaTeX）
    assert rows[3]["original"] == rows[3]["translation"]


def test_split_blocks_keeps_markdown_hard_breaks():
    """行尾两个空格是 Markdown 的**硬换行**标记，切块时不能被 rstrip 剥掉。

    剥掉之后块文本里的单换行成了软换行，前端 marked 会把整块折成一段 —— 表现就是
    "Markdown 视图正常（它读的是原文 md），对照 / 原文 / 译文里算法块糊成一坨"。
    """
    md = "1: **输入：** a  \n2: b  \n3: c\n\n下一段"
    blocks = split_blocks(md)
    assert blocks[0]["text"] == "1: **输入：** a  \n2: b  \n3: c"
    assert blocks[1]["text"] == "下一段"
    # 单个行尾空格仍然清掉（不是硬换行标记，留着会污染对照文本）
    assert split_blocks("a \nb")[0]["text"] == "a\nb"
    # 代码围栏里是字面量（本来就 rstrip），不会补标记回去
    assert split_blocks("```\nx  \ny\n```")[0]["text"] == "```\nx\ny\n```"


def test_pair_rows_keeps_hard_breaks_for_structured_blocks():
    algo = "1: input $d$  \n2: rotate  \n3: output"
    rows, quality = pair_rows(algo, algo, page=5)
    assert quality == "exact"
    assert rows[0]["original"] == algo
    assert rows[0]["original"].count("  \n") == 2


def test_row_key_separates_step_blocks():
    """编号步骤块（算法伪代码）必须和正文段落**不同键**。

    这是上面那条对齐 bug 的**直接守门**：真页（p006，13 块 vs 12 块）里才复现，
    缩小样本就失去复现条件（SequenceMatcher 的选择依赖整页块序列），所以这里盯最里面的键。
    """
    steps = {"type": "para", "text": "1: a  \n2: b  \n3: c"}
    prose = {"type": "para", "text": "just one sentence"}
    assert _row_key(steps) == ("para", "steps")
    assert _row_key(prose) == ("para",)
    # 只有一行序号、或整块只有一行 → 还是普通段落
    assert _row_key({"type": "para", "text": "1: only one line"}) == ("para",)
    assert _row_key({"type": "para", "text": "1: a  \nno number here"}) == ("para",)


def test_pair_rows_keeps_step_blocks_together():
    """一侧多出一段正文时，编号步骤块应当仍然两侧配对（记录预期行为；守门见上一条）。"""
    en = ("**Algorithm 2**\n\n"
          "1: input  \n2: rotate  \n3: output\n\n"
          "this index vector is passed to the primitive\n\n"
          "- a bullet\n")
    zh = ("**算法 2**\n\n"
          "1: 输入  \n2: 旋转  \n3: 输出\n\n"
          "- 一条要点\n")
    rows, quality = pair_rows(en, zh, page=6)
    assert quality == "structure"
    steps = [r for r in rows if r["original"].startswith("1:")]
    assert steps and steps[0]["translation"].startswith("1: 输入")
    lone = [r for r in rows if r["original"].startswith("this index")]
    assert lone and not lone[0]["translation"]        # 多出来的正文单独成行，不抢算法译文


def test_marker_key_localizes_a_mid_page_insertion():
    """块数不等时，**跨语言稳定标记**（公式片段 / 引用年份）让"插入"被定位到正确位置。

    纯类型键下"多出来的那一段该插在哪"**不可判定**，`SequenceMatcher` 会把它插在**页尾** ——
    于是后面的每一行都错位一行（实测 turbo p1 就是摘要里相邻两句：A 并成一块、B 拆成两块，
    结果从第 4 行起英中整体错位）。两块都带引用年份时，插入应当落在中间。
    """
    en = ("# 1 Method\n\nAlpha statement (1948).\n\n"
          "Beta statement (2023).\n\nGamma statement (2024).")
    zh = ("# 1 方法\n\n甲句 (1948)。\n\n多出来的一段 (1999)。\n\n"
          "乙句 (2023)。\n\n丙句 (2024)。")
    rows, quality = pair_rows(en, zh, page=1)
    assert quality == "structure"
    assert len(rows) == 5
    # 插入落在中间（一行只有译文），后面的行照样配得上 —— 而不是把错位甩到页尾
    assert rows[2]["original"] == "" and rows[2]["translation"].startswith("多出来")
    assert rows[3]["original"].startswith("Beta") and rows[3]["translation"].startswith("乙句")
    assert rows[4]["original"].startswith("Gamma") and rows[4]["translation"].startswith("丙句")


def test_pair_rows_structure_fallback_keeps_every_block():
    # 译文漏掉了一段：块数不等，必须走结构对齐且**不丢任何块**
    broken = PAGE_MD_ZH.replace(
        "我们为服从 Beta 分布的随机变量找到了最优标量量化器，使 MSE 失真最小。\n\n", "")
    rows, quality = pair_rows(PAGE_MD, broken, page=3)
    assert quality == "structure"
    originals = [r["original"] for r in rows if r["original"]]
    assert len(originals) == 6                     # 原文一块不少
    assert sum(1 for r in rows if not r["translation"]) >= 1


def test_pair_rows_handles_empty_translation():
    rows, quality = pair_rows(PAGE_MD, "", page=1)
    assert quality == "structure"
    assert all(r["translation"] == "" for r in rows)
    assert len(rows) == 6


# --------------------------------------------------------------------------
# 4. 页产物与落盘
# --------------------------------------------------------------------------


def test_build_page_payload_shape():
    payload = build_page_payload(page=3, page_total=22, result=_result(),
                                 source_chars=1200)
    assert payload["page"] == 3 and payload["page_total"] == 22
    assert payload["align"] == "exact"
    assert payload["original_md"].startswith("# TurboQuant")
    assert len(payload["rows"]) == 6
    assert payload["model"] == "mock-vision"
    assert payload["updated_at"]


def test_page_store_roundtrip_and_resume(tmp_path):
    path = tmp_path / PAGES_FILENAME
    store = PageStore("p", path)
    store.set_page_total(22)
    store.put(3, build_page_payload(page=3, page_total=22, result=_result()))
    store.put(4, build_page_payload(page=4, page_total=22, result=_result()))

    again = PageStore("p", path)
    assert again.done_pages() == [3, 4]
    assert again.page(3)["rows"][0]["id"] == "p003-00"
    stats = again.stats()
    assert stats["page_total"] == 22 and stats["pages_done"] == 2
    assert stats["rows"] == 12 and stats["align"] == {"exact": 2}
    assert stats["model"] == "mock-vision"


def test_page_store_clear(tmp_path):
    path = tmp_path / PAGES_FILENAME
    store = PageStore("p", path)
    store.put(1, build_page_payload(page=1, page_total=1, result=_result()))
    assert store.clear() == 1
    assert store.done_pages() == []


# --------------------------------------------------------------------------
# 5. 调用层（mock provider）
# --------------------------------------------------------------------------


def test_call_page_with_mock_vision_returns_pair():
    provider = MockGeneration({"model_name": "mock-vision"})
    prompt = render_page_prompt(page=3, page_total=22, body=PAGE_MD)
    result = call_page(provider, prompt, b"\x89PNG", max_tokens=8192)
    assert result.original_md.startswith("# TurboQuant")
    assert "量化" in result.translation_md
    rows, quality = pair_rows(result.original_md, result.translation_md, 3)
    assert quality == "exact" and len(rows) == 6


def test_max_tokens_ladder():
    # 2026-09-27 用户口径："不应该有 token 生成限制" → **基线就是模型自己的上限**：
    # 官方模型细节页写"输出长度 最大 384K"（上下文 1M），实测接受 max_tokens=393216。
    # 模型配置里那些小值（8192/16384）一律被抬上来；阶梯只剩一级 → 没有"再升一档"可升。
    assert MAX_OUTPUT_TOKENS == 393216
    assert effective_max_tokens(None) == MAX_OUTPUT_TOKENS
    assert effective_max_tokens(2048) == MAX_OUTPUT_TOKENS
    assert effective_max_tokens(16384) == MAX_OUTPUT_TOKENS
    assert next_max_tokens(MAX_OUTPUT_TOKENS) is None
    assert next_max_tokens(8192) == MAX_OUTPUT_TOKENS    # 老阶梯值只会被抬到上限


# --------------------------------------------------------------------------
# 跨页衔接：断词/续句标记 + 输出卫生 + 搭接片段
# --------------------------------------------------------------------------


def test_prompt_states_boundary_rules():
    """提示里必须写清：以自然断句为先 / 不许留断词连字符 / 不重复上页 / 搭接块不输出。"""
    import inspect

    from backend.services.translation import page_translate as pt

    src = inspect.getsource(pt) + pt.SYSTEM_PROMPT + pt.PAGE_USER_TEMPLATE
    assert "不许写成「通信瓶-」或半截词" in src        # 断词连字符仍然禁止
    assert "页面只是**排版单位**，不是语义单位" in src   # 不再"强行按页"翻译
    assert "不要为了迁就页面边界去拆句" in src
    # 边界那句**整句归上一页**、下一页跳过（这样读起来不会有半句，也不会两页各写一遍）
    assert "页尾那句尽量在本页收成完整句" in src
    assert "页首那半句已由上一页译完，本页跳过它" in src
    assert "绝不能两页各写一遍" in src
    assert "不要把搭接块里其他句子的内容输出到本页" in src
    assert "承上页" in src and "续下页" in src            # 明确禁止输出这类页标记
    # 数学必须写进定界符（否则前端会把 LaTeX 原样显示出来）
    assert "所有数学都必须写进公式定界符" in src


def test_edge_facts_are_rendered_into_prompt():
    """页边界事实要进 prompt，但只当**参考**（不再"必须遵守"）。"""
    from backend.services.translation.page_translate import (
        EDGE_FACTS_HEADER, render_edge_facts, render_page_prompt,
    )

    both = render_edge_facts({"head": True, "tail": True})
    assert EDGE_FACTS_HEADER in both
    assert "承接上一页的句子" in both
    assert "已由上一页译完，本页跳过它" in both        # 页首：跳过已被上一页译完的半句
    assert "把这句在本页译成完整句" in both            # 页尾：用搭接块把整句收在本页
    assert "仅供参考" in both
    assert "别为它牺牲中文的自然断句" in both

    clean = render_edge_facts({"head": False, "tail": False})
    assert "新起句子" in clean and "本页句子已结束" in clean
    assert render_edge_facts({}) == ""                     # 没有事实就不写这块

    # A 路（转录）**不能**收到"已由上一页译完、跳过它"：A 没有 carry，上一页从没写过这半句。
    a_facts = render_edge_facts({"head": True, "tail": True}, path="a")
    assert "已由上一页译完" not in a_facts
    assert "本页跳过它" not in a_facts
    assert "必须原样转录出来" in a_facts
    assert "照原样停在断处" in a_facts          # A 页尾不许补句号、也不许用下一页内容补完

    prompt = render_page_prompt(page=2, page_total=22, body="Body.",
                                edges={"head": True, "tail": True})
    assert EDGE_FACTS_HEADER in prompt
    # 参考块必须在【本页输出】之前（先看约束再产出）
    assert prompt.index(EDGE_FACTS_HEADER) < prompt.index("【本页输出】")
    # 不传 edges 时不应出现该块（兼容旧调用）
    assert EDGE_FACTS_HEADER not in render_page_prompt(page=2, page_total=22, body="Body.")


def test_mark_rows_continuity_cleans_and_flags():
    """实测 p001-06 → p002-00 那两个噪声：「通信瓶-」与开头的「……」都要清掉并打标。"""
    rows = [
        {"id": "p002-00", "type": "para", "original": "tlenecks …",
         "translation": "……在加速器上的 HBM 与 SRAM 之间的瓶颈。"},
        {"id": "p002-01", "type": "para", "original": "x", "translation": "正常行"},
    ]
    mark_rows_continuity(rows, head=True, tail=False)
    assert rows[0]["continued_from_prev"] is True
    assert rows[0]["translation"].startswith("在加速器上")          # 「……」已剥掉
    assert "continued_from_prev" not in rows[1]

    tail_rows = [
        {"id": "p001-05", "type": "para", "original": "…", "translation": "正常行"},
        {"id": "p001-06", "type": "para", "original": "…communication bot-",
         "translation": "该延迟主要归因于通信瓶-"},
    ]
    mark_rows_continuity(tail_rows, head=False, tail=True)
    assert tail_rows[-1]["continues"] is True
    assert not tail_rows[-1]["translation"].endswith("-")           # 悬空连字符已去掉
    assert tail_rows[-1]["translation"].endswith("通信瓶")


def test_mark_rows_continuity_handles_empty_rows():
    rows: list[dict] = []
    mark_rows_continuity(rows, head=True, tail=True)                # 不应抛异常
    assert rows == []


def test_render_page_prompt_carries_lookahead():
    prompt = render_page_prompt(page=1, page_total=22, body="Body.",
                                lookahead="【下一页开头】\ntlenecks between HBM and SRAM")
    assert "下一页开头" in prompt and "tlenecks between" in prompt
    # 搭接必须在【本页输出】之前（模型先看到上下文再被要求输出）
    assert prompt.index("tlenecks between") < prompt.index("【本页输出】")
    # 没有 lookahead 时不出现搭接块（【本页输出】里解释 carry_md 时会提到"下一页开头"四个字）
    assert "<下一页文本>" not in render_page_prompt(page=1, page_total=22, body="Body.")


def test_page_lookahead_and_continuity_use_parser_evidence(monkeypatch):
    """搭接只取下一页开头几行；末页没有下一页 → 空串。"""
    import backend.services.translation.page_translate as pt

    pages = {
        1: "first line of p1\nsecond line of p1",
        # 每行都收句 → 窗口就是 lines 行（没收句时会再往后多取几行，见 lookahead 那条测试）
        2: "l1 ends here.\nl2 ends here.\nl3 ends here.\nl4 ends here.\nl5 ends here.",
        3: "",
    }
    monkeypatch.setattr(pt, "page_source_text", lambda pid, page: (pages.get(page, ""), ""))
    la = pt.page_lookahead("p", 1, lines=3)
    assert "l1" in la and "l3" in la and "l4" not in la             # 只取前 3 行
    assert pt.page_lookahead("p", 3) == ""                          # 下一页没有文本


def test_page_continuity_judges_from_block_evidence(monkeypatch):
    """续句/断词判定：两侧同时成立才认；公式/表格结尾不算；证毕号算句末。"""
    import backend.services.translation.page_translate as pt

    def blocks(mapping):
        def fake(pid, page):
            return mapping.get((pid, page), [])
        return fake

    cases = {
        # 断词（p1 末 `bot-` → p2 首 `tlenecks…`）+ 断句（p2 末 `…of the` → p3 首小写起头）
        ("p", 1): [{"type": "para", "order": 1, "text": "…communication bot-"}],
        ("p", 2): [
            {"type": "para", "order": 1, "text": "tlenecks between HBM and SRAM on accelerators."},
            {"type": "para", "order": 2, "text": "…over the randomness of the"},
        ],
        ("p", 3): [{"type": "para", "order": 1, "text": "quantizer's output. Thus, we aim …"}],
        # 页尾是公式 → 不按断句处理
        ("q", 1): [{"type": "formula", "order": 1, "text": "$$ x = 1 $$"}],
        ("q", 2): [{"type": "para", "order": 1, "text": "along with a sign vector"}],
        # 页尾以证毕号收尾 → 完整句，且下页是标题
        ("r", 1): [{"type": "para", "order": 1, "text": "we get the desired lower bound. □"}],
        ("r", 2): [{"type": "heading", "order": 1, "text": "B.2 QJL: 1-BIT INNER PRODUCT"}],
    }
    monkeypatch.setattr(pt, "_page_text_blocks", blocks(cases))
    assert pt.page_continuity("p", 2) == {"head": True, "tail": True}
    assert pt.page_continuity("p", 1)["tail"] is True        # 页尾断词
    assert pt.page_continuity("q", 2)["head"] is False       # 上页是公式结尾
    assert pt.page_continuity("r", 1)["tail"] is False       # 证毕号收尾 = 完整句


def test_page_lookahead_skips_figure_area_noise(monkeypatch):
    """页首是整块图区（子图标签/坐标轴刻度/图片占位）时要跳过它，把真正的正文续句取回来。

    实测 p20 页尾 `…applied to the`，p21 开头是图 5 的图区 —— 旧实现取回的前 6 行
    全是 `(a) TURBOQUANT` / `$$ \\times 10 $$` / `![](...png)`，模型只好写「…施加于……」。
    """
    import backend.services.translation.page_translate as pt

    p21 = "\n".join([
        "(a) TURBOQUANT$_{\\text{prod}}$",
        "",
        "$$ \\times 10 $$",
        "",
        "![−0.050.000.05 (b) TUR](figures/p21-b02.png)",
        "",
        "*−0.050.000.05−0.050.000.05 (b) TURBOQUANT*",
        "",
        "**Figure 5: The variance of Inner-product error remains constant for TURBOQUANT, while in**",
        "",
        "TURBOQUANTincreases with the average inner product. Bit-width is b = 2.",
        "",
        "input embedding vectors during both quantization and dequantization stages. This transformation is crucial…",
    ])
    monkeypatch.setattr(pt, "page_source_text",
                        lambda pid, page: (p21 if page == 21 else "本页正文", ""))
    la = pt.page_lookahead("p", 20)
    assert "input embedding vectors during both quantization" in la        # 续句进来了
    assert "![" not in la and "(a) TURBOQUANT" not in la                   # 图片占位与子图标签被跳过
    assert "\\times 10" not in la                                          # 坐标轴刻度被跳过


def test_page_continuity_falls_back_to_prose_behind_figure_area(monkeypatch):
    """图区顶在页首时，按"块"判会漏；回退到"同一窗口内第一行小写正文"来判续句。"""
    import backend.services.translation.page_translate as pt

    block_map = {
        ("fig", 20): [{"type": "para", "order": 1, "text": "…rotation matrices applied to the"}],
        # 下页首块是子图标签（既不是公式也不是正文）
        ("fig", 21): [{"type": "caption", "order": 1, "text": "(a) TURBOQUANT"}],
        # 反例：下页首行正文是大写起头的新句子 → 不认续句
        ("new", 1): [{"type": "para", "order": 1, "text": "…stops in the middle of the"}],
        ("new", 2): [{"type": "para", "order": 1, "text": "(a) TURBOQUANT"}],
        ("new", 3): [{"type": "para", "order": 1, "text": "Figure 5: caption line here"}],
    }
    src = {
        ("fig", 21): "(a) TURBOQUANT\n\n$$ \\times 10 $$\n\ninput embedding vectors during both stages.",
        ("new", 2): "(a) TURBOQUANT\n\nFigure 5: caption line here\n\nThe whole method is summarised above.",
    }
    monkeypatch.setattr(pt, "_page_text_blocks",
                        lambda pid, page: block_map.get((pid, page), []))
    monkeypatch.setattr(pt, "page_source_text",
                        lambda pid, page: (src.get((pid, page), ""), ""))

    assert pt.page_continuity("fig", 20)["tail"] is True     # 图区挡不住正文续句
    assert pt.page_continuity("fig", 21)["head"] is True     # 同理，本页在承接上页
    assert pt.page_continuity("new", 1)["tail"] is False     # 下页正文是大写起头 → 不认


def test_mark_rows_continuity_strips_page_tail_ellipsis():
    """页尾的「……」是占位不是内容（用户截图：…将随机旋转矩阵施加于……）。"""
    rows = [
        {"id": "p020-16", "type": "para", "original": "…applied to the",
         "translation": "将随机旋转矩阵施加于……"},
    ]
    mark_rows_continuity(rows, head=False, tail=True)
    assert rows[-1]["translation"] == "将随机旋转矩阵施加于"
    assert rows[-1]["continues"] is True


def test_transcribe_prompt_keeps_footnote_rule_and_drops_arxiv_stamp():
    """用户口径（2026-12-27）：脚注分隔线要保留成 `---`；arXiv 预印本标记要丢掉。

    旧提示词只写「丢弃页眉、页脚、页码、脚注横线、出版社水印」，两件事都做错了 ——
    脚注那条横线被丢（转录里根本看不到分隔线），arXiv 标记却原样抄成了
    `# arXiv:1706.03762v5 [cs.CL] 6 Dec 2017` 一行（生产库 p001-02 实测）。
    """
    import backend.services.translation.page_translate as pt

    for prompt in (pt.TRANSCRIBE_SYSTEM, pt.TRANSLATE_SYSTEM, pt.SYSTEM_PROMPT):
        assert "脚注横线" not in prompt            # 不再把脚注横线列进丢弃清单
    assert "唯一保留的横线是脚注区上方那条短横线" in pt.TRANSCRIBE_SYSTEM
    # 作者区/机构区要写成一段（不能一列一行），并列短项才用 ` · `（用户口径 2026-12-27）
    assert "作者区 / 机构区写成" in pt.TRANSCRIBE_SYSTEM
    assert "不要一列一行" in pt.TRANSCRIBE_SYSTEM
    assert "不要写表格、不要 `<br>`" in pt.TRANSCRIBE_SYSTEM
    assert "arXiv:1706.03762v5 [cs.CL] 6 Dec 2017" in pt.TRANSCRIBE_SYSTEM
    assert "arXiv 预印本标记" in pt.TRANSCRIBE_SYSTEM
    assert "arXiv 预印本标记" in pt.TRANSLATE_SYSTEM
    # 块模式：清单里独立一块 `---` 要原样输出（否则会撞上"译文不能是空串"那条）
    assert "原样输出 `---`" in pt.BLOCK_TRANSLATE_SYSTEM


def test_empty_block_guard_finds_blocks_that_would_lose_a_sentence():
    """块模式：原文（剪完页首那截后）还有正文、译文却是空串 → 必须被认出来（否则静默丢句）。

    生产库 attention-cleanroom 第 2 页实测：上一页 carry 走掉了开头 84 字符，B 于是把整块
    输出成 `""`，而块里还留着「Numerous efforts have since continued …」，中文里那句话就没了。
    """
    from backend.services.translation.page_translate import EMPTY_BLOCK_RETRY_NOTE
    from backend.services.translation.translation_service import _empty_blocks

    class Res:
        def __init__(self, blocks):
            self.blocks = blocks

    blocks = [{"text": "Numerous efforts have since continued …"}, {"text": "Recurrent models …"}]
    assert _empty_blocks(Res(["", "循环模型……"]), blocks) == [0]
    # 剪完什么都不剩的块（text 空）**不算**丢句：那是"整块已被上一页译完"的合法写法
    assert _empty_blocks(Res(["", ""]), [{"text": "  "}, {"text": "x"}]) == [1]
    # 缺块（数组比清单短）同样算
    assert _empty_blocks(Res([]), blocks) == [0, 1]
    assert _empty_blocks(Res(["a", "b"]), blocks) == []
    assert "空字符串" in EMPTY_BLOCK_RETRY_NOTE and "彻底消失" in EMPTY_BLOCK_RETRY_NOTE


def test_edge_facts_tell_model_what_to_do_when_lookahead_cannot_help():
    """【下一页开头】里看不到后半截（图/表/公式）时，要明确"停在断处"，别写「……」也别硬凑。"""
    from backend.services.translation.page_translate import render_edge_facts

    text = render_edge_facts({"head": False, "tail": True})
    assert "把这句在本页译成完整句" in text
    assert "不要留半句" in text
    assert "不要写「……」" in text
    assert "停在断处" in text
    assert "不要硬凑句号" in text


def test_lookahead_window_reaches_the_end_of_the_carried_sentence(monkeypatch):
    """跨页那句常常拖到图/算法块后面才收尾：窗口要一直取到句末，否则上一页只能补半句。"""
    import backend.services.translation.page_translate as pt

    body = "\n".join([
        "**Algorithm 2 TURBOQUANT: optimized for inner product**",
        "",
        "$$ d\\times d $$",
        "",
        "# 3: Generate a random projection matrix S",
        "",
        "this index vector is passed to the primitive DEQUANT(idx), it produces a reconstructed vector",
        "",
        "x̃ ∈ Rthat satisfies the following distortion bounds: • MSE defined as D := E is bounded",
        "",
        "for any b ≥ 0.",
        "",
        "第二段正文，窗口到这里就够了。",
    ])
    monkeypatch.setattr(pt, "page_source_text", lambda pid, page: (body, ""))
    lines = pt._leading_lines("p", 6)
    assert pt._ends_sentence(lines[-1])                     # 最后一行落在句末
    assert "for any b ≥ 0." in lines
    la = pt.page_lookahead("p", 5)
    assert "for any b ≥ 0." in la                          # 完整那句都在 lookahead 里


def test_carry_md_contract_and_fragment_drop():
    """模型的 carry_md 申报"从下一页借用的原文"，服务端据此精确剪掉下一页的重复部分。"""
    from backend.services.translation.page_translate import (
        SYSTEM_PROMPT, drop_declared_fragment, parse_carry,
    )

    assert "carry_md" in SYSTEM_PROMPT                  # 契约里有这个字段
    assert parse_carry('{"carry_md": "stages. This transformation"}') == "stages. This transformation"
    assert parse_carry('{"original_md": "x"}') == ""   # 缺字段 → 空串（不报错）
    assert parse_carry("不是 JSON") == ""

    next_page = ("input embedding vectors during both quantization and dequantization stages. "
                 "This transformation is crucial for the algorithm's performance.")
    trimmed = drop_declared_fragment(
        next_page, "input embedding vectors during both quantization and dequantization stages.")
    assert trimmed.startswith("This transformation is crucial")
    assert "input embedding vectors" not in trimmed

    # 换行/多空格抄得不一致也能匹配（模型偶尔把换行抄成空格）
    assert drop_declared_fragment("alpha beta\ngamma delta rest", "alpha beta gamma delta").startswith("rest")
    # 空申报 / 找不到 / 位置太后 → 原样返回（宁重勿丢）
    assert drop_declared_fragment(next_page, "") == next_page
    assert drop_declared_fragment(next_page, "完全不在正文里的一段") == next_page
    assert drop_declared_fragment("x" * 4000 + " tail", "tail") == "x" * 4000 + " tail"


def test_heuristic_head_trim_is_gone():
    """启发式剪头（按第一句句末猜）已废弃：它会把 `**Algorithm 1 …: optimized for MSE**` 误剪。"""
    from backend.services.translation import page_translate as pt

    assert not hasattr(pt, "head_fragment_len")
    assert not hasattr(pt, "drop_head_fragment")


def test_pair_rows_aligns_by_type_not_text():
    """配对键只能用类型/公式/编号：中英正文文本永不相同，用文本签名会让一侧少一块时整体错位一行。

    实测 p19：译文侧故意少掉"上一页已译完的承接句"，于是「证明。…」被挂到上一条 bullet 上、
    最后一行空成「未翻译」（用户截图报过）。
    """
    from backend.services.translation.page_translate import pair_rows

    en = "\n\n".join([
        "along with a sign vector qjl applied to the residual vector",   # 承接上一页 → 译文侧没有
        "- **Expected inner-product** $\\mathbb{E}$",
        "- **For small bit-widths** $b = 1, 2, 3, 4$",
        "*Proof.* First we compute the conditional expectation.",
        "$$ \\mathbb{E}[Y] = 1 $$",
        "where the first equality follows from the definition.",
    ])
    zh = "\n\n".join([
        "- **期望内积** $\\mathbb{E}$",
        "- **对于小位宽** $b = 1, 2, 3, 4$",
        "*证明。* 我们首先如下计算条件期望。",
        "$$ \\mathbb{E}[Y] = 1 $$",
        "其中第一个等号来自定义。",
    ])
    rows, quality = pair_rows(en, zh, 19)
    assert quality == "structure"
    assert rows[0]["translation"] == ""                        # 承接句只在上一页，本页留空
    assert rows[0]["original"].startswith("along with a sign vector")
    assert rows[1]["translation"].startswith("- **期望内积**")
    assert rows[2]["translation"].startswith("- **对于小位宽**")
    assert rows[3]["translation"].startswith("*证明。*")        # 不再错位到上一条 bullet
    assert rows[4]["type"] == "formula" and rows[4]["translation"].startswith("$$")
    assert rows[5]["translation"].startswith("其中第一个等号")


def test_page_source_text_keeps_every_block(monkeypatch):
    """组装侧不做任何"图内文字"删减（曾做过一轮，已按用户要求撤掉：死规则容易误判、
    把图注/正文一起吞掉）。是否把图内文字写进正文，交给提示词去判。"""
    import backend.services.translation.page_translate as pt
    from backend.services.paper import reader_service as rs

    blocks = {"blocks": [
        {"id": "b01", "page": 13, "order": 1, "type": "heading",
         "rect": "[108,72,230,84]", "text": "Attention Visualizations",
         "section": "Attention Visualizations"},
        {"id": "b02", "page": 13, "order": 2, "type": "ref",
         "rect": "[434,241,503,263]", "text": "<pad><pad><pad><pad><pad><pad>"},
        {"id": "b03", "page": 13, "order": 3, "type": "caption",
         "rect": "[108,312,504,355]", "text": "**Figure 3: An example of the attention mechanism …**"},
    ]}
    monkeypatch.setattr(rs, "load_block_index", lambda paper_id: blocks)

    text, section = pt.page_source_text("p", 13)
    assert "Attention Visualizations" in text
    assert "<pad>" in text                # 块照给，不做图内文字过滤
    assert "Figure 3" in text
    assert section == "Attention Visualizations"


def test_prompt_forbids_copying_figure_internal_text():
    """提示词要明确"只写图注本身"：图内文字（示例句/图例/刻度/子图标签）不许抄成正文行。"""
    from backend.services.translation.page_translate import SYSTEM_PROMPT

    assert "只写图注本身" in SYSTEM_PROMPT
    assert "图内部的成段文字一律不要抄" in SYSTEM_PROMPT or "图里面的一切文字都不要抄" in SYSTEM_PROMPT
    assert "示例句子" in SYSTEM_PROMPT
    assert "图例" in SYSTEM_PROMPT                     # 图例行（实测 TurboQuant 图 3）
    assert "每一行的原文都必须逐字来自给你的文本" in SYSTEM_PROMPT
    # 旧的"允许短标注跟一行"的口子必须堵住，否则图例会又被抄成一行
    assert "这类**短标注**" not in SYSTEM_PROMPT




def test_block_mode_ignores_stale_a_page_when_retranslating(monkeypatch):
    """两路同时 retranslate：B 不能拿**上一轮**的 A 产物当块清单（整页会错一行）。

    实测（attention-cleanroom 第 1 页）：A 重跑后 11 块、B 读到的还是旧的 10 块，
    于是原文那一列对上了译文里的下一行。修法：force 模式下只认本任务开始之后写下的那一页。
    """
    from datetime import datetime

    from backend.services.translation import translation_service as ts

    class FakeStore:
        def __init__(self, payload):
            self.payload = payload
        def reload(self):
            return None
        def page(self, page):
            return self.payload

    stale = {'original_md': 'stale page', 'updated_at': '2020-01-01T00:00:00'}
    monkeypatch.setattr(ts, '_active_path_task', lambda paper_id, path: None)

    # 没有 fresh_after：旧产物照用（resume 模式的正常行为）
    assert ts.await_a_page(FakeStore(stale), 'p', 1, required=True) is stale

    # 有 fresh_after（本任务开始得更晚）：旧产物算过期，等不到就抛，绝不静默错位
    fresh = datetime.fromisoformat('2026-01-01T00:00:00')
    try:
        ts.await_a_page(FakeStore(stale), 'p', 1, timeout=0, required=True, fresh_after=fresh)
    except Exception as exc:
        assert '转录产物' in str(exc)
    else:
        raise AssertionError('过期页没被拒绝')

    # 新鲜的页照用
    ok_page = {'original_md': 'new page', 'updated_at': '2026-06-01T00:00:00'}
    assert ts.await_a_page(FakeStore(ok_page), 'p', 1, timeout=0, required=True,
                           fresh_after=fresh) is ok_page