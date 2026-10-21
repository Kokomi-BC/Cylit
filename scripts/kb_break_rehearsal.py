"""单页彩排：只花一次视觉模型调用，验证「breaks 锚点」契约能不能被遵守。

不写任何产物（不碰 `PageStore`），只打印：锚点原文 / 落到哪一行 / 合法还是被拒（原因）。
**全量重译前先跑这个** —— 几十页视觉调用之前，用 1 页确认模型按契约给锚点、校验层判得对。

支持三种模式（两路拆分后新增后两种）：

```
python scripts/kb_break_rehearsal.py <paper_id> <page>            # 融合模式（旧）
python scripts/kb_break_rehearsal.py <paper_id> <page> a          # A 路：英文转录
python scripts/kb_break_rehearsal.py <paper_id> <page> b          # B 路：中文翻译
```

⚠️ B 路的**产线**现在是「块模式」（A 定块、B 照译，见 `render_block_prompt`），而这里彩排的是
旧的两路契约（各自分块）。所以本脚本只能回答「模型会不会按契约给锚点」，不能代表 B 路产物的
真实判定 —— 后者要看 `page_products` 的 `break_stats`。

⚠️ 单路彩排的**校验结果偏乐观**：`apply_semantic_breaks` 的"前一行收句了吗"要用锚点自己
那一侧的文本判（`_break_reject_reason(..., side)`），而单路拿到的行只有一侧有内容。
想看真实判定，得等两路都落盘后看 `page_products` 的 `break_stats`（读时会用合并行重算一次）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.translation import translation_service
from backend.services.translation.page_translate import (
    PATH_A_FILENAME, PATH_B_FILENAME, apply_semantic_breaks, build_outline_skeleton,
    build_stable_prefix, call_page, load_page_image, page_source_text, pair_rows,
    render_page_prompt)

paper_id = sys.argv[1] if len(sys.argv) > 1 else "turboquant-online-vector-iclr-26"
page = int(sys.argv[2]) if len(sys.argv) > 2 else 5
mode = (sys.argv[3] if len(sys.argv) > 3 else "fused").lower()
path = None if mode in ("fused", "f", "both") else mode
if path not in (None, "a", "b"):
    raise SystemExit(f"未知模式：{mode}（只能是 fused / a / b）")

label = {"a": f"A 路（英文转录 → {PATH_A_FILENAME}）",
         "b": f"B 路（中文翻译 → {PATH_B_FILENAME}）"}.get(path, "融合模式（一次调用产出两侧）")

# 视觉模型已下线（翻译链路不再挂它）：彩排改用**本路**的生成模型，与产线同一套解析。
provider = translation_service._path_provider(path)
body, _src = page_source_text(paper_id, page)
prompt = render_page_prompt(
    page=page, page_total=22, body=body,
    prefix=build_stable_prefix(None, "", build_outline_skeleton(paper_id)),
    section="", path=path)
image = load_page_image(paper_id, page)
print(f"【{label}】provider={provider.model_name} 页 {page}："
      f"页图 {len(image or b'')} bytes，解析文本 {len(body)} 字")

result = call_page(provider, prompt, image, max_tokens=16384, path=path)
print(f"model={result.model} prompt={result.prompt_tokens} "
      f"completion={result.completion_tokens} latency={result.latency_ms:.0f}ms")
print(f"original_md {len(result.original_md)} 字 / translation_md {len(result.translation_md)} 字"
      f" / carry_md {len(result.carry_md)} 字")

print(f"\n锚点 {len(result.breaks)} 条：")
for b in result.breaks:
    print(f"  · type={b['type'] or '（未给）':<12} start={b['start']!r}")

rows, quality = pair_rows(result.original_md, result.translation_md, page)
stats = apply_semantic_breaks(rows, result.breaks)
print(f"\nalign={quality} 行数 {len(rows)}")
print(json.dumps(stats, ensure_ascii=False, indent=2))
print("\n落到行上的边界（前一行两侧的结尾都打出来：判定用的是**锚点自己那一侧**）：")
for i, r in enumerate(rows):
    if not r.get("semantic_break"):
        continue
    prev = rows[i - 1] if i else {}
    print(f"  → 行 {r['id']} (type={r['type']}, 锚点类型={r.get('semantic_type') or '-'})")
    print(f"     前一行 en 结尾：…{(prev.get('original') or '')[-60:]!r}")
    print(f"     前一行 zh 结尾：…{(prev.get('translation') or '')[-60:]!r}")
    print(f"     本行开头：{(r.get('original') or r.get('translation') or '')[:80]!r}")
print("\n前 2 行预览：")
for r in rows[:2]:
    print(f"  {r['id']} {r['type']}: en={(r.get('original') or '')[:80]!r}")
    print(f"  {' ' * len(r['id'])} {r['type']}: zh={(r.get('translation') or '')[:80]!r}")
