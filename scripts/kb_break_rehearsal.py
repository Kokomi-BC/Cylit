"""重译前的单页彩排：只花一次视觉模型调用，验证「breaks 锚点」契约能不能被遵守。

不写任何产物（不碰 PageStore），只打印：锚点原文 / 落到哪一行 / 合法还是被拒（原因）。
**全量重译前先跑这个**：37 页视觉调用之前，用 1 页确认模型能按契约给锚点、校验层判得对。

用法::

    python scripts/kb_break_rehearsal.py <paper_id> <page>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.model.model_manager import model_manager
from backend.services.translation.page_translate import (
    apply_semantic_breaks, build_outline_skeleton, build_stable_prefix, call_page,
    load_page_image, page_source_text, pair_rows, render_page_prompt)

paper_id = sys.argv[1] if len(sys.argv) > 1 else "turboquant-online-vector-iclr-26"
page = int(sys.argv[2]) if len(sys.argv) > 2 else 5

provider = model_manager.get_vision()
body, _src = page_source_text(paper_id, page)
prompt = render_page_prompt(page=page, page_total=22, body=body,
                            prefix=build_stable_prefix(None, "", build_outline_skeleton(paper_id)),
                            section="")
image = load_page_image(paper_id, page)
print(f"provider={provider.model_name} 页 {page}：页图 {len(image or b'')} bytes，"
      f"解析文本 {len(body)} 字")

result = call_page(provider, prompt, image, max_tokens=16384)
print(f"model={result.model} prompt={result.prompt_tokens} "
      f"completion={result.completion_tokens} latency={result.latency_ms:.0f}ms")
print(f"original_md {len(result.original_md)} 字 / translation_md {len(result.translation_md)} 字")
print(f"\n锚点 {len(result.breaks)} 条：")
for b in result.breaks:
    print(f"  · {b!r}")

rows, quality = pair_rows(result.original_md, result.translation_md, page)
stats = apply_semantic_breaks(rows, result.breaks)
print(f"\nalign={quality} 行数 {len(rows)}")
print(json.dumps(stats, ensure_ascii=False, indent=2))
print("\n落到行上的边界：")
for i, r in enumerate(rows):
    if r.get("semantic_break"):
        print(f"  → 行 {r['id']} (type={r['type']}) 前一行结尾："
              f"…{((rows[i-1].get('original') or '')[-60:])!r}")
        print(f"     本行开头：{(r.get('original') or '')[:80]!r}")
print("\n前 2 行预览：")
for r in rows[:2]:
    print(f"  {r['id']} {r['type']}: {(r.get('original') or '')[:100]!r}")
