"""语义边界锚点（breaks）层测试：prompt 契约 / 容错解析 / 校验层（非法位置一律回退）。

为什么单独一层测：marker 是**模型给的建议**，代码必须把它当不可信输入 —— 校验层就是这一层的
全部价值。这里逐条断言"哪些位置允许切、哪些必须拒"，因为切错了会直接毁掉 chunk
（切在句子中间 / 公式与解释之间 / 表格内部）。
"""

from __future__ import annotations

from backend.services.translation.page_translate import (
    MIN_BREAK_ANCHOR_KEY, PAGE_SCHEMA, SYSTEM_PROMPT, PageCallResult,
    anchor_key, apply_semantic_breaks, build_page_payload, pair_rows, parse_breaks,
)

EN_MD = """# 1 TURBOQUANT ONLINE QUANTIZATION

We study vector quantization for online applications without

any assumption on the input distribution.

## 1.1 MSE OPTIMAL

The distortion is defined as follows:

$$D := \\mathbb{E}\\|x - Q(x)\\|^2$$

The quantizer minimizes this distortion over all codebooks.

**Table 1: LongBench results.**

| Method | SingleQA | Average |
|---|---|---|
| Full Cache | 45.0 | 43.1 |

We also evaluate the quantizer on the Needle test.

The results show that recall is preserved.

Recall is measured on the hidden sentence.

Recall is measured on the long context.
"""

ZH_MD = """# 1 TURBOQUANT ONLINE QUANTIZATION

我们研究面向在线应用的向量量化，不

对输入分布做任何假设。

## 1.1 MSE 最优

失真定义如下：

$$D := \\mathbb{E}\\|x - Q(x)\\|^2$$

量化器在所有码本中最小化该失真。

**表 1：LongBench 结果。**

| Method | SingleQA | Average |
|---|---|---|
| Full Cache | 45.0 | 43.1 |

我们还在 Needle 测试上评估该量化器。

结果表明召回率被完整保留。

召回率是在隐藏句子上测的。

召回率是在长上下文上测的。
"""


def _payload(breaks):
    return build_page_payload(page=7, page_total=22,
                              result=PageCallResult(original_md=EN_MD,
                                                    translation_md=ZH_MD,
                                                    breaks=list(breaks),
                                                    model="mock-vision"))


def _rows(*specs) -> list[dict]:
    """按 (type, original) 造行，id 位置编号与真实产物一致。"""
    return [{"id": f"p007-{i:02d}", "index": i, "type": t, "level": 0, "number": "",
             "original": o, "translation": zh or o}
            for i, (t, o, zh) in enumerate(specs)]


# --------------------------------------------------------------------------
# 1. prompt 契约 + 容错解析
# --------------------------------------------------------------------------


def test_breaks_contract_in_prompt_and_schema():
    assert "breaks" in SYSTEM_PROMPT
    assert "语义边界" in SYSTEM_PROMPT
    # 契约要写清"不许标在哪里"，否则模型会把锚点塞进句子/公式/表格里
    for banned in ("一句话中间", "公式与紧随其后的解释之间", "表格标题与表体之间"):
        assert banned in SYSTEM_PROMPT
    props = PAGE_SCHEMA["properties"]
    assert props["breaks"]["type"] == "array"
    # v2：每一条是 `{"start", "type"}` 对象（类型只是标签，不参与切分判定）
    assert props["breaks"]["items"]["type"] == "object"
    assert set(props["breaks"]["items"]["required"]) == {"start"}
    # breaks 不是必填：缺失只应退化成"机械切分"，不该让整页翻译作废
    assert "breaks" not in PAGE_SCHEMA["required"]


def test_parse_breaks_is_lenient():
    """v2 对象数组 + v1 纯字符串数组都要读得懂（老产物在磁盘上就是 v1）。"""
    assert parse_breaks('{"breaks": [{"start": "A sentence starts here", "type": "method"}]}') == \
        [{"start": "A sentence starts here", "type": "method"}]
    # v1 兼容：纯字符串数组 → type 为空
    assert parse_breaks('{"breaks": ["A sentence starts here", "Another one"]}') == \
        [{"start": "A sentence starts here", "type": ""},
         {"start": "Another one", "type": ""}]
    assert parse_breaks('{"breaks": "single anchor string"}') == \
        [{"start": "single anchor string", "type": ""}]
    assert parse_breaks('{"breaks": ["  ", "ok anchor here", null]}') == \
        [{"start": "ok anchor here", "type": ""}]
    # 未知类型归一成 other（**不丢锚点**）、大小写与空格归一
    assert parse_breaks('{"breaks": [{"start": "x", "type": " Method "}]}') == \
        [{"start": "x", "type": "method"}]
    assert parse_breaks('{"breaks": [{"start": "x", "type": "???"}]}') == \
        [{"start": "x", "type": "other"}]
    assert parse_breaks('{"breaks": [{"type": "method"}]}') == []   # 没有 start 就没有锚点
    assert parse_breaks('{"original_md": "x"}') == []               # 缺字段
    assert parse_breaks("不是 JSON") == []
    assert parse_breaks('{"breaks": {"a": 1}}') == []


# --------------------------------------------------------------------------
# 2. 校验层：真实页产物上"该收的收、该拒的拒"
# --------------------------------------------------------------------------


def test_valid_breaks_land_on_row_boundaries():
    payload = _payload([
        "We also evaluate the quantizer on the Needle test.",   # 表之后的正文段：合法
        "The results show that recall is preserved.",           # 两句之间：合法
    ])
    rows = payload["rows"]
    flagged = [r["id"] for r in rows if r.get("semantic_break")]
    assert flagged == ["p007-09", "p007-10"]
    stats = payload["break_stats"]
    assert stats["anchors"] == 2 and stats["accepted"] == 2
    assert stats["by_side"] == {"original": 2, "translation": 0}
    assert stats["rejected"] == []


def test_illegal_breaks_are_rejected_with_reason():
    payload = _payload([
        "We study vector quantization for online applications",  # 前一行是标题 → 不许切
        "any assumption on the input distribution",             # 前一行没收句 → 切在句中
        "The quantizer minimizes this distortion",              # 前一行是公式 → 公式要与解释同在
        "| Full Cache |",                                        # 短锚点落在正文行上 → 拒
        "完全不在这一页里的一段锚点文字",                          # 匹配不到
        "Recall is measured on the",                            # 撞车（两段同前缀）
        "# 1 TURBOQUANT ONLINE QUANTIZATION",                   # 第 0 行 → 页首没有上一个单元
    ])
    reasons = [r["reason"] for r in payload["break_stats"]["rejected"]]
    assert reasons == ["after_heading", "mid_sentence", "after_formula",
                       "short_not_heading", "unmatched", "ambiguous", "page_start"]
    assert payload["break_stats"]["accepted"] == 0
    assert not any(r.get("semantic_break") for r in payload["rows"])


def test_break_reasons_are_table_driven():
    """逐条钉住位置规则 —— 切错位置的代价是毁掉一个 chunk。"""
    cases = [
        (("heading", "# 1 TURBOQUANT", "x"), ("para", "A legal looking paragraph here.", "y"),
         "after_heading"),
        (("para", "The distortion is defined as follows:", "x"),
         ("para", "The quantizer minimizes this distortion.", "y"), "dangling_colon"),
        (("para", "We study vector quantization for online applications without", "x"),
         ("para", "any assumption on the input distribution.", "y"), "mid_sentence"),
        (("formula", "$$D := 1$$", "x"), ("para", "The quantizer minimizes it.", "y"),
         "after_formula"),
        (("caption", "**Table 1: LongBench results.**", "x"),
         ("table", "| Method | SingleQA |", "y"), "inside_structure"),
        (("table", "| Method | SingleQA |", "x"),
         ("caption", "**Table 1: LongBench results.**", "y"), "inside_structure"),
        (("table", "| Method | SingleQA |", "x"),
         ("para", "We also evaluate the quantizer.", "y"), ""),
        (("para", "The results show that recall is preserved.", "x"),
         ("para", "Recall is measured on the hidden sentence.", "y"), ""),
    ]
    for prev, cur, expected in cases:
        rows = _rows(("para", "Opening paragraph of this page.", "z"), prev, cur)
        anchor = cur[1]
        stats = apply_semantic_breaks(rows, [anchor])
        reasons = [r["reason"] for r in stats["rejected"]]
        if expected:
            assert reasons == [expected], (anchor, reasons)
            assert not any(r.get("semantic_break") for r in rows)
        else:
            assert reasons == [] and stats["accepted"] == 1, (anchor, reasons)


def test_short_anchor_is_only_accepted_on_heading_rows():
    """标题本来就短（`# 6 Results` 规范化后 8 个字符）→ 短锚点只认标题行，不认正文段。"""
    from backend.services.translation.page_translate import MIN_BREAK_ANCHOR_KEY

    assert len(anchor_key("# 6 Results")) < MIN_BREAK_ANCHOR_KEY
    rows = _rows(("para", "Opening paragraph of this page.", "开头段。"),
                 ("heading", "# 6 Results", "# 6 结果"),
                 ("para", "6 Results are summarized below in Table 2.", "结果如下。"))
    stats = apply_semantic_breaks(rows, ["# 6 Results"])
    assert stats["accepted"] == 1 and stats["short_heading"] == 1
    assert rows[1]["semantic_break"] is True
    # 同样短的锚点落在正文段上：拒（标题行以外不认短锚点）
    stats = apply_semantic_breaks(rows, ["**Table 2**"])
    assert stats["accepted"] == 0
    assert [r["reason"] for r in stats["rejected"]] == ["short_not_heading"]


def test_chinese_anchor_matches_translation_side():
    rows = _rows(("para", "The results show that recall is preserved.", "结果表明召回率被完整保留。"),
                 ("para", "Recall is measured on the hidden sentence.", "召回率是在隐藏句子上测的。"))
    stats = apply_semantic_breaks(rows, ["召回率是在隐藏句子上测的"])
    assert stats["accepted"] == 1
    assert stats["by_side"] == {"original": 0, "translation": 1}
    assert rows[1]["semantic_break"] is True


def test_duplicate_anchors_are_counted_once():
    rows = _rows(("para", "Opening paragraph of this page.", "开头段。"),
                 ("para", "The results show that recall is preserved.", "结果段。"))
    stats = apply_semantic_breaks(rows, ["The results show that recall is preserved."] * 3)
    assert stats["accepted"] == 1 and stats["duplicate"] == 2


def test_anchor_key_is_markdown_insensitive():
    assert anchor_key("**Table 1: LongBench** results.") == "table1longbenchresults"
    assert len(anchor_key("| Full |")) < MIN_BREAK_ANCHOR_KEY
    assert anchor_key("## 3.2.1 Scaled Dot-Product Attention") == \
        anchor_key("### 3.2.1 Scaled dot product attention")


def test_translation_side_anchor_is_judged_by_translation_text():
    """中文锚点的"前一行收句了吗"必须用**中文**那一侧判。

    两路拆分后这点变硬：拿英文标点去判中文块，会出现"中文块以「：」结尾却被放行"的漏判
    （英文那侧正好以 `.` 收句）。这里造出两侧收句状态**不一致**的一行来钉住它。
    """
    rows = _rows(("heading", "# 1 Method", "# 1 方法"),
                 ("para", "Alpha sentence.", "甲句："),
                 ("para", "Beta sentence.", "乙句，它足够长以便通过下限。"))
    stats = apply_semantic_breaks(
        rows, [{"start": "乙句，它足够长以便通过下限", "type": "method"}])
    assert stats["accepted"] == 0
    # 关键：如果拿**英文**侧去判，前一行是 `Alpha sentence.`（以 `.` 收句）→ 会被放行
    assert stats["rejected"] == [{"anchor": "乙句，它足够长以便通过下限",
                                  "reason": "dangling_colon", "side": "translation"}]
    assert "semantic_break" not in rows[2]


def test_original_side_anchor_ignores_translation_text():
    """反过来也一样：英文锚点只看英文侧（融合模式的老行为，一个字符都没变）。"""
    rows = _rows(("heading", "# 1 Method", "# 1 方法"),
                 ("para", "Alpha sentence", "甲句。"),      # 英文没收句、中文收了
                 ("para", "Beta sentence.", "乙句。"))
    stats = apply_semantic_breaks(rows, ["Beta sentence."])
    assert stats["accepted"] == 0
    # 关键：如果拿**中文**侧去判，前一行是 `甲句。`（收句了）→ 会被放行
    assert stats["rejected"] == [{"anchor": "Beta sentence.", "reason": "mid_sentence",
                                  "side": "original"}]
    assert "semantic_break" not in rows[2]


# --------------------------------------------------------------------------
# 3. 落盘与向后兼容
# --------------------------------------------------------------------------


def test_payload_stores_raw_breaks_and_flags_rows():
    v2 = [{"start": "The results show that recall is preserved.", "type": "result"}]
    payload = _payload(v2)
    # **原样留档**：换模型重译时要拿它重新落回行，所以存的就是归一后的 v2 形状
    assert payload["breaks"] == v2
    assert payload["break_stats"]["accepted"] == 1
    assert payload["break_stats"]["by_type"] == {"result": 1}
    assert payload["rows"][10]["semantic_break"] is True
    assert payload["rows"][10]["semantic_type"] == "result"


def test_v1_string_breaks_still_work_and_get_no_type():
    """存量产物里的 `breaks` 是纯字符串数组 —— 必须照旧落成边界，只是没有类型标签。"""
    payload = _payload(["The results show that recall is preserved."])
    assert payload["rows"][10]["semantic_break"] is True
    assert "semantic_type" not in payload["rows"][10]
    assert payload["break_stats"]["typed"] == 0


def test_old_payload_without_breaks_is_untouched():
    """存量产物（没有 breaks 字段）必须与改造前行为完全一致 —— 不重译也能继续跑。"""
    rows, _ = pair_rows(EN_MD, ZH_MD, 7)
    stats = apply_semantic_breaks(rows, None)
    assert stats["anchors"] == 0 and stats["accepted"] == 0
    assert not any(r.get("semantic_break") for r in rows)
    # 幂等：带标记的行再跑一次（读时会重算）不会留下上一轮的痕迹
    apply_semantic_breaks(rows, ["The results show that recall is preserved."])
    assert rows[10].get("semantic_break") is True
    apply_semantic_breaks(rows, None)
    assert "semantic_break" not in rows[10]
