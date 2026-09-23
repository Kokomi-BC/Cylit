"""页级翻译管线测试：prompt 契约 / 两份 Markdown 的行对齐 / 页产物落盘 / mock 形状。

不依赖数据库与网络：只验证"页级调用 → 对齐 → 落盘"这一层的契约。
端到端（需要 MySQL）的部分放在 test_paper_service.py 与 scripts/e2e_check.py。
"""

from __future__ import annotations

import json

from backend.providers.generation.mock import MockGeneration
from backend.services.translation.brief import DocumentBrief
from backend.services.translation.page_translate import (
    PAGES_FILENAME, PageCallResult, PageStore, build_page_payload,
    build_stable_prefix, call_page, effective_max_tokens, history_context,
    mark_rows_continuity, next_max_tokens, pair_rows, parse_pair, prefix_hash,
    render_page_prompt, split_blocks,
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
    assert prefix_hash(a) == prefix_hash(b)


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
    assert effective_max_tokens(None) == 8192
    assert effective_max_tokens(2048) == 8192        # 安全下限
    assert effective_max_tokens(16384) == 16384
    assert next_max_tokens(8192) == 16384
    assert next_max_tokens(65536) is None


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
    assert "下一页开头" not in render_page_prompt(page=1, page_total=22, body="Body.")


def test_page_lookahead_and_continuity_use_parser_evidence(monkeypatch):
    """搭接只取下一页开头几行；末页没有下一页 → 空串。"""
    import backend.services.translation.page_translate as pt

    pages = {
        1: "first line of p1\nsecond line of p1",
        2: "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8",
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
