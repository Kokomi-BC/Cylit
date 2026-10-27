"""引用分组：一个标号 = 一处连续的材料（见 backend/agent/cite_groups.py）。

这里钉住四件事，每一条都对应一个**实测过**的现象：

1. 模型写过的**全部**方括号形态都要认得出（半角逗号 / 全角逗号 / 顿号 / 区间 / 区间里带逗号），
   但**认不出的必须原样留着**（论文自己的 `[12, Eq.3]`、页码、年份 —— 吞掉一个字都是坏正文）；
2. 不相邻的证据**必须拆成多个标号**（一枚按钮只闪一处），相邻的合成一个 `[a–b]`；
3. 编号按正文首次出现顺序压成 1..M、连续无洞；**同一处材料**在全文复用同一个号，
   不同组合（哪怕只差一行、或是子集）各给各的号；
4. `[36, 41]` 这种"一枚方括号里塞两个号"的形态能被正确改写**并且被计入引用** ——
   改造前它既没被改写、也没进引用表（那是"编号很奇怪"的根因）。
"""

from __future__ import annotations

from backend.agent.citations import Citation, RefRegistry
from backend.agent.cite_groups import (
    EN_DASH, apply_cite_groups, group_run, identity_key, parse_bracket_marks, plan_cite_groups,
    split_runs,
)
from backend.agent.sanitize import referenced_citations, used_refs


def cite(ref: int, row_id: str | None = None, pids: tuple[str, ...] = (), *,
         page: int | None = None, kind: str = "block", block_id: str | None = None,
         latex: str = "") -> Citation:
    """造一条证据：默认是一个只认得出 row_id 的块。"""
    return Citation(ref=ref, paper_id="demo", kind=kind, page=page, row_id=row_id,
                    block_id=block_id, snippet=f"片段{ref}", pids=list(pids), latex=latex)


def registry_with(count: int, *, page: int = 5) -> RefRegistry:
    """一页上的 count 条块证据（ref 依次 1..count，行号就是 ref）—— 复刻"工具返回顺序发号"。"""
    reg = RefRegistry("demo")
    for i in range(1, count + 1):
        reg.assign(key=f"block:{i}", kind="block", page=page, row_id=f"p{page:03d}-{i:02d}",
                   snippet=f"片段{i}")
    return reg


# --------------------------------------------------------------------------
# 1. 扫描：认得哪些写法、哪些必须原样留着
# --------------------------------------------------------------------------


def test_parse_reads_every_form_the_model_actually_writes() -> None:
    text = "A[3] B[3,7] C[3、7] D[3，7] E[3-5] F[3" + "\u2013" + "5] G[3-5, 9]"
    got = [mark.members for mark in parse_bracket_marks(text)]
    assert got == [(3,), (3, 7), (3, 7), (3, 7), (3, 4, 5), (3, 4, 5), (3, 4, 5, 9)]


def test_parse_never_swallows_what_it_cannot_read() -> None:
    text = "见[12, Eq.3]、[1-99]（太宽）、[见附录]、[2024]、[] 这些都不是引用，只有[7]是"
    got = [mark.raw for mark in parse_bracket_marks(text)]
    assert got == ["[7]"], "认不出的方括号不许出现在结果里（原样留文本）"
    out, _ = apply_cite_groups(text, [cite(7, "p005-02")])
    for raw in ("[12, Eq.3]", "[1-99]", "[见附录]", "[2024]", "[]"):
        assert raw in out, f"{raw} 被吞了"


def test_tightly_written_marks_form_one_run_but_punctuation_splits() -> None:
    assert [len(run) for run in split_runs("[3][7][9]")] == [3]
    assert [len(run) for run in split_runs("[3] [7]")] == [2], "只隔空白仍算同一个 run"
    assert [len(run) for run in split_runs("[3]，[7]")] == [1, 1], "中间有标点就是两处引用"
    assert [len(run) for run in split_runs("[3] 正文 [7]")] == [1, 1]

# --------------------------------------------------------------------------
# 2. 分组：连续才合并，不相邻必须拆开
# --------------------------------------------------------------------------


def test_adjacent_rows_become_one_mark_with_en_dash() -> None:
    out, payload = apply_cite_groups(
        "量化误差在坐标间变得均匀[7][8][9]。",
        [cite(7, "p005-02"), cite(8, "p005-03"), cite(9, "p005-04")])
    assert out == f"量化误差在坐标间变得均匀[1{EN_DASH}3]。"
    assert payload[0]["ref"] == 1 and payload[0]["members"] == [1, 2, 3]


def test_non_adjacent_rows_are_split_into_separate_marks() -> None:
    """相邻的 run 里夹着"不相邻的证据"：必须拆成两枚标号，绝不并进一个区间。"""
    out, payload = apply_cite_groups(
        "同一句话里提到了两处[7][9]。",
        [cite(7, "p005-02"), cite(9, "p005-04")])
    assert out == "同一句话里提到了两处[1][2]。"
    assert all("members" not in item for item in payload), "拆开之后每枚都是单词条"


def test_geometry_less_members_stay_single() -> None:
    """认不出几何信息（没有 row_id、没有 pids）就**不猜**：各自单独成组。"""
    groups = group_run([cite(1), cite(2), cite(3)])
    assert [len(g) for g in groups] == [1, 1, 1]
    out, _ = apply_cite_groups("看不出位置的证据[1][2]", [cite(1), cite(2)])
    assert out == "看不出位置的证据[1][2]"


def test_same_block_halves_merge_when_pid_rows_are_known() -> None:
    """同一块跨页被切成两半：pids 不相交，但并起来是同页连续行 → 一个标号。"""
    first = cite(1, pids=("r-aaaaaaaaaaaaaaa1",))
    second = cite(2, pids=("r-aaaaaaaaaaaaaaa2",))
    assert [len(g) for g in group_run([first, second])] == [1, 1], "没有解析表就不猜"
    rows = {"r-aaaaaaaaaaaaaaa1": (5, 11), "r-aaaaaaaaaaaaaaa2": (5, 12)}
    assert [len(g) for g in group_run([first, second], pid_rows=rows)] == [2]
    # 解析不全（有一个 pid 查不到）同样不猜
    half = {"r-aaaaaaaaaaaaaaa1": (5, 11)}
    assert [len(g) for g in group_run([first, second], pid_rows=half)] == [1, 1]


def test_shared_pids_merge_without_any_row_id() -> None:
    groups = group_run([cite(1, pids=("r-b", "r-a")), cite(2, pids=("r-b",))])
    assert [len(g) for g in groups] == [2]


# --------------------------------------------------------------------------
# 3. 编号：1..M 连续无洞，同一处材料复用同一个号
# --------------------------------------------------------------------------


def test_numbering_is_compressed_by_first_appearance() -> None:
    citations = [cite(31, "p005-02"), cite(47, "p005-07"), cite(52, "p009-01")]
    out, payload = apply_cite_groups(
        "先看第一处[47]，再看第二处[31]，最后是第三处[52]。", citations)
    assert out == "先看第一处[1]，再看第二处[2]，最后是第三处[3]。"
    assert [item["ref"] for item in payload] == [1, 2, 3]
    assert [item["snippet"] for item in payload] == ["片段47", "片段31", "片段52"], \
        "1..M 的顺序 = 正文首次出现顺序"


def test_unused_evidence_is_dropped_and_numbers_have_no_holes() -> None:
    reg = registry_with(6)
    text = "只用到第 5 和第 2 条[5]，还有第 2 条[2]。"
    used = referenced_citations(text, reg)
    out, payload = apply_cite_groups(text, used)
    assert out == "只用到第 5 和第 2 条[1]，还有第 2 条[2]。"
    assert [item["ref"] for item in payload] == [1, 2]
    assert all(item["ref"] == i for i, item in enumerate(payload, start=1))


def test_same_material_set_reuses_the_same_number_everywhere() -> None:
    """同一处材料在正文里出现两次：**同一个号**（不能一次 3 一次 36）。"""
    citations = [cite(3, "p005-02"), cite(36, "p005-09")]
    out, payload = apply_cite_groups(
        "前面说过[36]，后面再说一次[36]，最后又提[3]。", citations)
    assert out == "前面说过[1]，后面再说一次[1]，最后又提[2]。"
    assert len(payload) == 2
    # 同一份集合（同一条证据的两种写法：pids 相同的 block 与 chunk）也只占一个号
    block = cite(3, pids=("r-c", "r-d"))
    chunk = cite(4, pids=("r-c", "r-d"), kind="chunk")
    assert identity_key(block) == identity_key(chunk)
    out2, payload2 = apply_cite_groups("同一处[3][4]", [block, chunk])
    assert out2 == "同一处[1]", "同一处材料写两遍只留一个标号"


def test_subset_gets_its_own_mark_not_one_interval() -> None:
    """{3,4} 与 {3,4,5} 是**不同组合**：各给各的标号，绝不并成一个区间。"""
    citations = [cite(1, "p005-02"), cite(2, "p005-03"), cite(3, "p005-04")]
    out, payload = apply_cite_groups("先两处[1][2]，再三处[1][2][3]。", citations)
    assert out == f"先两处[1{EN_DASH}2]，再三处[1{EN_DASH}3]。"
    assert payload[0]["members"] == [1, 2] and payload[0]["ref"] == 1

# --------------------------------------------------------------------------
# 4. [36, 41] 那种形态：改写 + 计入引用（这次改造的根因）
# --------------------------------------------------------------------------


def test_multi_number_bracket_is_rewritten_and_counted_as_citations() -> None:
    """实测：正文写着 `[36, 41]`，引用表却只有 1/2/3 号 —— 这条钉住它必须被改写。"""
    reg = registry_with(45)
    text = "同一处材料写了两个号[36, 41]。"
    assert used_refs(text, reg) == [36, 41], "多号方括号必须被计入引用（改造前这里是空列表）"
    out, payload = apply_cite_groups(text, referenced_citations(text, reg))
    assert out == "同一处材料写了两个号[1][2]。", "这两行不相邻 → 必须拆成两枚标号"
    assert [item["ref"] for item in payload] == [1, 2]
    # 全角逗号 / 顿号是同一种写法
    for form in ("[36，41]", "[36、41]"):
        assert used_refs(form, reg) == [36, 41]
        assert apply_cite_groups(form, referenced_citations(form, reg))[0] == "[1][2]"


def test_range_form_is_expanded_then_regrouped() -> None:
    reg = registry_with(45)
    text = "第 36 到 41 条[36-41]"
    assert used_refs(text, reg) == [36, 37, 38, 39, 40, 41], "区间要展开成每一条"
    out, payload = apply_cite_groups(text, referenced_citations(text, reg))
    assert out == f"第 36 到 41 条[1{EN_DASH}6]", "连续六行 → 一个标号、一枚按钮"
    assert payload[0]["members"] == [1, 2, 3, 4, 5, 6]


# --------------------------------------------------------------------------
# 5. payload 的契约：members / pids 并集 / 首成员字段
# --------------------------------------------------------------------------


def test_group_payload_follows_the_contract() -> None:
    a = cite(7, "p005-02", pids=("r-a",), page=5, latex="x=1")
    b = cite(8, "p005-03", pids=("r-a", "r-b"), page=5)
    out, payload = apply_cite_groups("同一个公式的两块[7][8]", [a, b])
    assert out == f"同一个公式的两块[1{EN_DASH}2]"
    item = payload[0]
    assert item["ref"] == 1 and item["members"] == [1, 2]
    assert item["pids"] == ["r-a", "r-b"], "组的 pids = 成员并集（文档顺序、去重）"
    assert item["row_id"] == "p005-02" and item["snippet"] == "片段7", "其余字段取第一个成员"
    assert item["latex"] == "x=1", "latex 任一成员有就保留"
    assert "members" not in payload[1], "成员自己那条仍是单词条，形状不变"


def test_single_mark_keeps_the_old_shape() -> None:
    out, payload = apply_cite_groups("结果[9]", [cite(9, "p005-02", pids=("r-a",))])
    assert out == "结果[1]"
    assert payload[0] == {"ref": 1, "paper_id": "demo", "kind": "block", "page": None,
                          "section": None, "block_id": None, "row_id": "p005-02",
                          "snippet": "片段9", "image_urls": [], "label": "block",
                          "pids": ["r-a"]}


def test_unknown_ref_bracket_stays_raw() -> None:
    """正文里"认不出"的方括号原样保留：绝不吞字，也不假装它是引用。"""
    out, payload = apply_cite_groups("论文自己的编号[12]不是我们的", [cite(36, "p005-02")])
    assert out == "论文自己的编号[12]不是我们的"
    assert [item["ref"] for item in payload] == [1], "那一处证据照样进引用表（只是正文没写号）"


# --------------------------------------------------------------------------
# 6. 轨迹步骤：与正文共用**同一份**编号表
# --------------------------------------------------------------------------


def test_steps_render_with_the_same_numbering_plan() -> None:
    citations = [cite(36, "p005-02"), cite(41, "p005-09")]
    plan = plan_cite_groups("最终回答[41]", citations)
    assert plan.render("最终回答[41]") == "最终回答[1]"
    assert plan.render("过程：我先看[36]") == "过程：我先看[2]", "轨迹里的标号走同一份表"
    assert plan.render("这里[36][36]") == "这里[2]", "同一处写两遍只留一个标号"
    assert plan.render("认不出的[999]") == "认不出的[999]"


def test_text_without_any_mark_still_lists_the_evidence() -> None:
    """短回答常常一个号都不写：`referenced_citations` 的兜底语义必须保持不变。"""
    citations = [cite(5, "p005-02"), cite(9, "p005-03")]
    out, payload = apply_cite_groups("这段回答一个编号都没写。", citations)
    assert out == "这段回答一个编号都没写。"
    assert [item["ref"] for item in payload] == [1, 2]
    assert [item["snippet"] for item in payload] == ["片段5", "片段9"]

def test_same_place_evidence_collapses_and_keeps_the_formula_latex() -> None:
    """同一行 = 同一处材料：块证据与它里面那枚公式合成**一个**号，但 latex 不许丢。

    丢了 latex，前端就认不出那一行里是哪一枚 KaTeX，只能退化成整行闪（精度掉一档）。
    """
    block = cite(4, "p005-02")
    formula = cite(9, "p005-02", kind="formula", latex="x=1")
    out, payload = apply_cite_groups("这一行[4][9]", [block, formula])
    assert out == "这一行[1]", "同一行写两遍只留一个标号、一个号"
    assert len(payload) == 1
    assert payload[0]["latex"] == "x=1", "并进同一条时 latex 必须留下"
    # pids 集合相同（只是顺序不同）也是同一处：pids 去重合并，latex 照样保留
    plain = cite(1, pids=("r-a", "r-b"))
    with_latex = cite(2, pids=("r-b", "r-a"), kind="formula", latex="y^2")
    out2, payload2 = apply_cite_groups("同一行[1][2]", [plain, with_latex])
    assert out2 == "同一行[1]"
    assert payload2[0]["pids"] == ["r-a", "r-b"] and payload2[0]["latex"] == "y^2"
