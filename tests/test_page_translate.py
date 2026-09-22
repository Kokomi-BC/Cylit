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
    build_stable_prefix, call_page, effective_max_tokens, next_max_tokens,
    pair_rows, parse_pair, prefix_hash, render_page_prompt, split_blocks,
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
