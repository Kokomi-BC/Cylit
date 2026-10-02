"""Agent 问答链路自检（规划 §15 的 A1–A14 里 P0 能覆盖的部分）。

跑之前要**两个进程都在**：

    python cli/pr.py serve      # :8710
    python cli/pr.py agent      # :8711

    python scripts/agent_check.py                # 全跑（会真的调模型，花几分钱）
    python scripts/agent_check.py --quick        # 只跑不调模型的那几节

报告落 `data/logs/agent_check.json`。断言口径尽量"能失败"：比如"流式增量的拼接
必须等于 `done.answer`"、"引用编号必须连续"、"正文里不许出现 chunk_id 形态串"，
这些都是真出过问题的地方（思维链被当正文推出去、模型把 chunk_id 抄进回答）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_BASE = "http://127.0.0.1:8710"
DEFAULT_PAPER = "attention-is-all-you-need"
#: 硬护栏用例：这篇论文在库里**没有建索引、也没有档案**（"没有证据"的确定形态）
DEFAULT_GUARDRAIL_PAPER = "sniffer"

#: 护栏文案的特征片段（`runtime.GUARDRAIL_NO_EVIDENCE` 的开头）
GUARDRAIL_MARK = "没有检索到"
#: "模型自己说没依据"的措辞（软护栏：有索引的论文问无关问题时）
REFUSAL_MARKS = ("没有", "未找到", "找不到", "不涉及", "无法回答", "不含")
#: 内部 id 的形态（chunk id 形如 ``{paper}:{road}:{type}:…:{lang}``）
CHUNK_ID_RE = re.compile(r"\b\w[\w.\-]*:(?:a|b):(?:unit|text|formula|figure|table):")
#: 口癖：模型把内部字段名写进正文
ID_ECHO_RE = re.compile(r"\b(?:chunk_id|block_id|page_idx)\b\s*[=:：]")
#: 受控图片地址（只允许本论文自己的 region 图）
IMAGE_URL_RE = re.compile(r"/api/v1/papers/(?P<paper>[^/)]+)/figures/region/\d+/\d+")
#: 非受控图片（外链 / data: / blob:）——**一个都不许留**
FOREIGN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*<?(?:https?://|data:|blob:)[^)]*\)")

results: list[dict] = []


def record(kind: str, path: str, ok: bool, detail: str = "") -> None:
    results.append({"kind": kind, "path": path, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {path}" + (f"  {detail}" if detail else ""))


def skip(path: str, detail: str) -> None:
    print(f"[SKIP] {path}  {detail}")


# --------------------------------------------------------------------------
# HTTP / SSE
# --------------------------------------------------------------------------


def get_json(url: str, headers: dict | None = None, timeout: float = 60.0):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict, headers: dict | None = None,
              timeout: float = 60.0):
    body = json.dumps(payload).encode("utf-8")
    head = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=head)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ask(base: str, paper: str, question: str, *, timeout: float = 300.0) -> dict:
    """POST /agent/ask，消费整条 SSE，返回结构化结果。"""
    payload = json.dumps({"paper_id": paper, "question": question}).encode("utf-8")
    req = urllib.request.Request(f"{base}/api/v1/agent/ask", data=payload,
                                 headers={"Content-Type": "application/json"})
    events: list[dict] = []
    kinds: dict[str, int] = {}
    deltas: list[str] = []
    first_delta_ms = None
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        etype = None
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("event: "):
                etype = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                kinds[etype] = kinds.get(etype, 0) + 1
                events.append({"type": etype, "data": data})
                if etype == "answer_delta":
                    deltas.append(data.get("text") or "")
                    if first_delta_ms is None:
                        first_delta_ms = round((time.perf_counter() - t0) * 1000)
    elapsed = round(time.perf_counter() - t0, 1)
    terminal = [e for e in events if e["type"] in ("done", "error", "cancelled")]
    return {"events": events, "kinds": kinds, "deltas": "".join(deltas),
            "terminal": terminal, "elapsed": elapsed,
            "first_delta_ms": first_delta_ms,
            "done": next((e["data"] for e in terminal if e["type"] == "done"), None),
            "error": next((e["data"] for e in terminal if e["type"] == "error"), None)}


def token() -> str:
    path = ROOT / "data" / "secrets" / "agent.token"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


# --------------------------------------------------------------------------
# ① 服务与 skills
# --------------------------------------------------------------------------


def check_service(base: str) -> dict:
    print("\n" + "=" * 78)
    print("① Agent 服务 / skills / 模型配置")
    print("=" * 78)
    internal = {"X-Internal-Token": token()}
    try:
        status = get_json(f"{base}/api/v1/agent/status")["data"]
    except Exception as exc:  # noqa: BLE001
        record("service", "主后端 /agent/status 可达", False, f"{type(exc).__name__}: {exc}")
        return {}
    record("service", "主后端 /agent/status 可达", True,
           f"running={status.get('running')} enabled={status.get('enabled')}")
    model = status.get("model") or {}
    record("service", "问答模型已配置且可用", bool(model.get("usable")),
           f"{model.get('provider')}/{model.get('model_name')} "
           f"key={model.get('has_key')} 回落generation={model.get('fallback_to_generation')}")

    ready = get_json(f"{base}/api/v1/agent/skills", headers=internal).get("data") or {}
    skills = [s.get("name") for s in (ready.get("skills") or [])]
    sources = {s.get("source") for s in (ready.get("skills") or [])}
    record("service", "skills 已加载", len(skills) >= 4, f"{skills}")
    record("service", "skills 走的是框架加载器（不是退化自扫）",
           sources == {"framework"},
           f"来源={sources}；chars={[s.get('chars') for s in (ready.get('skills') or [])]}")
    record("service", "写能力默认不可见", "formula-fix" not in skills,
           "skills 里没有 formula-fix（P0 不挂写工具）")
    return status


# --------------------------------------------------------------------------
# ② 工具后端投影
# --------------------------------------------------------------------------


def check_projections(base: str, paper: str) -> dict:
    print("\n" + "=" * 78)
    print("② 工具后端投影（archive / pages / search / figures）")
    print("=" * 78)
    api = f"{base}/api/v1/agent/papers/{paper}"
    internal = {"X-Internal-Token": token()}
    out: dict = {}
    try:
        archive = get_json(f"{api}/archive", headers=internal)["data"]
        out["archive"] = archive
        record("projection", "档案含领域与章节结构",
               bool(archive.get("brief", {}).get("domain")) and bool(archive.get("outline")),
               f"domain={str(archive.get('brief', {}).get('domain'))[:24]}… "
               f"outline={len(archive.get('outline') or [])} 条 "
               f"key_claims={len(archive.get('brief', {}).get('key_claims') or [])} 条")
        record("projection", "档案带页数（模型据此知道规模）",
               int(archive.get("page_count") or 0) > 0,
               f"page_count={archive.get('page_count')}")
    except Exception as exc:  # noqa: BLE001
        record("projection", "档案接口", False, f"{type(exc).__name__}: {exc}")

    try:
        pages = get_json(f"{api}/pages?pages=1-2", headers=internal)["data"]
        blocks = [b for p in pages.get("pages") or [] for b in p.get("blocks") or []]
        with_anchor = [b for b in blocks if b.get("row_id")]
        out["pages"] = pages
        record("projection", "按页取块且有正文",
               len(blocks) > 0 and any(b.get("text") for b in blocks),
               f"块={len(blocks)} 首块={blocks[0].get('id') if blocks else None}")
        record("projection", "块带行锚点（引用才能跳到高亮行）",
               len(with_anchor) >= max(1, len(blocks) // 2),
               f"{len(with_anchor)}/{len(blocks)} 个块有 row_id")
    except Exception as exc:  # noqa: BLE001
        record("projection", "按页接口", False, f"{type(exc).__name__}: {exc}")

    try:
        found = post_json(f"{api}/search", {"query": "multi-head attention", "top_k": 3},
                          headers=internal)["data"]
        items = found.get("items") or []
        out["search"] = found
        record("projection", "检索返回候选位置（页码 + 片段）",
               bool(items) and all(i.get("page_no") for i in items),
               f"命中={len(items)} 页码={[i.get('page_no') for i in items]} "
               f"top_score={found.get('top_score')}")
        record("projection", "检索带 page_total", bool(found.get("page_total")),
               f"page_total={found.get('page_total')}")
    except Exception as exc:  # noqa: BLE001
        record("projection", "检索接口", False, f"{type(exc).__name__}: {exc}")

    try:
        figs = get_json(f"{api}/figures?page=1", headers=internal)["data"]
        out["figures"] = figs
        record("projection", "插图清单可读（P1 页可能没有图）", True,
               f"第 1 页插图={len(figs.get('items') or [])} 条")
    except Exception as exc:  # noqa: BLE001
        record("projection", "插图接口", False, f"{type(exc).__name__}: {exc}")
    return out


# --------------------------------------------------------------------------
# ③ 证据闸（不进模型）
# --------------------------------------------------------------------------


def check_guardrail(base: str, paper: str, guardrail_paper: str) -> None:
    print("\n" + "=" * 78)
    print("③ 证据闸：没有索引的论文 → 硬护栏（**不进模型**）；有索引的问无关问题 → 模型自己说没依据")
    print("=" * 78)
    question = "这篇论文里水稻插秧机的液压系统故障诊断是怎么做的？"

    # ---- ① 确定性：一篇没建索引、也没档案的论文 → 直接护栏，一次模型调用都不该有
    if guardrail_paper:
        try:
            run = ask(base, guardrail_paper, question)
            done = run["done"] or {}
            answer = done.get("answer") or ""
            record("guardrail", f"无索引论文（{guardrail_paper}）命中硬护栏",
                   GUARDRAIL_MARK in answer, f"answer[:40]={answer[:40]!r}")
            record("guardrail", "护栏回答没有引用", not (done.get("citations") or []),
                   f"citations={len(done.get('citations') or [])}")
            record("guardrail", "护栏路径没有调用模型（rounds=0）",
                   int(done.get("rounds") or 0) == 0, f"rounds={done.get('rounds')}")
            record("guardrail", "护栏是唯一终态", len(run["terminal"]) == 1,
                   f"终态={[e['type'] for e in run['terminal']]}")
        except Exception as exc:  # noqa: BLE001
            record("guardrail", "无索引论文护栏", False, f"{type(exc).__name__}: {exc}")
    else:
        skip("硬护栏", "没给 --guardrail-paper")

    # ---- ② 软护栏：有索引的论文同样问无关问题 —— 检索**永远**会返回 top-k，
    #         实测重排分阈值会误伤真问题（同一个无关问句，写长一点就从 0.44 涨到 0.68），
    #         所以这里要防的是"模型开始拿不相关片段凑答案"。
    try:
        run = ask(base, paper, question)
        answer = (run["done"] or {}).get("answer") or ""
        record("guardrail", "无关问题下模型明确说没有依据",
               any(mark in answer for mark in REFUSAL_MARKS), f"answer[:50]={answer[:50]!r}")
    except Exception as exc:  # noqa: BLE001
        record("guardrail", "无关问题下模型明确说没有依据", False,
               f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# ④ 真实问答
# --------------------------------------------------------------------------


def check_answer(base: str, paper: str, question: str) -> dict:
    print("\n" + "=" * 78)
    print(f"④ 真实问答：{question}")
    print("=" * 78)
    try:
        run = ask(base, paper, question)
    except Exception as exc:  # noqa: BLE001
        record("answer", "问答可跑", False, f"{type(exc).__name__}: {exc}")
        return {}
    kinds = run["kinds"]
    print(f"        事件：{kinds}（{run['elapsed']}s，首字 {run['first_delta_ms']}ms）")
    for line in run["deltas"][:200].splitlines()[:6]:
        print(f"        | {line[:90]}")

    record("answer", "整条流恰好一个终态",
           len(run["terminal"]) == 1, f"终态={[e['type'] for e in run['terminal']]}")
    if run["error"]:
        record("answer", "没有 error 终态", False, json.dumps(run["error"], ensure_ascii=False))
        return run
    done = run["done"] or {}
    answer = done.get("answer") or ""
    record("answer", "有正文", len(answer) > 80, f"{len(answer)} 字")

    # ★ 思维链一旦漏进正文，这里会立刻炸。比较前先抹掉 `done` 阶段会做的两处**已知改写**：
    # ① 引用编号压成 1..M（流式推的还是 registry 原始编号）；
    # ② 非受控图片被删（模型会去配外链图，实测嵌过 arxiv 地址）。
    norm = _normalize_final
    same = norm(run["deltas"]) == norm(answer)
    if not same:
        _dump_diff(run["deltas"], answer, norm)
    record("answer", "流式增量拼接 == done.answer（忽略编号重排与图片删除）", same,
           f"增量={len(run['deltas'])} 字 权威={len(answer)} 字"
           + ("" if same else f" 首个差异@{_first_diff(norm(run['deltas']), norm(answer))}"
              f"（明细见 data/logs/agent_check_diff.txt）"))

    foreign = FOREIGN_IMAGE_RE.search(answer)
    record("answer", "正文没有非受控图片（外链/data:/blob:）", foreign is None,
           f"命中={foreign.group(0)[:60]!r}" if foreign else "无外链图片")

    citations = done.get("citations") or []
    refs = [c.get("ref") for c in citations]
    record("answer", "引用编号从 1 连续", refs == list(range(1, len(refs) + 1)) and bool(refs),
           f"{refs[:12]}{'…' if len(refs) > 12 else ''}")
    used = sorted({int(m) for m in re.findall(r"\[(\d{1,3})\]", answer)})
    unmapped = [r for r in used if r not in set(refs)]
    record("answer", "正文里的 [n] 都能解析到引用", not unmapped,
           f"正文用到={used[:12]} 未映射={unmapped}")

    leak = CHUNK_ID_RE.search(answer) or ID_ECHO_RE.search(answer)
    record("answer", "正文没有内部 id 泄漏", leak is None,
           f"命中={leak.group(0)!r}" if leak else "无 chunk_id / block_id 形态串")

    record("answer", "轮数在上限内（0<rounds<=6）",
           0 < int(done.get("rounds") or 0) <= 6, f"rounds={done.get('rounds')}")

    trace = done.get("tool_trace") or []
    tools = [t.get("tool") for t in trace if not str(t.get("tool", "")).startswith("__")]
    record("answer", "P0 不含写工具", "page_markdown_edit" not in tools, f"工具序={tools}")
    if "paper_search" in tools and "page_markdown_get" in tools:
        record("answer", "先检索定位、再读页",
               tools.index("paper_search") < tools.index("page_markdown_get"),
               f"工具序={tools}")
    else:
        skip("先检索定位、再读页", f"本轮没用全两类工具：{tools}")

    # 落库先于 done
    conv = done.get("conversation_id")
    record("answer", "done 带 conversation_id 且已落库",
           bool(conv) and bool(done.get("persisted")), f"conversation_id={conv}")
    if conv:
        try:
            data = get_json(f"{base}/api/v1/agent/conversations/{conv}")["data"]
            msgs = data.get("messages") or []
            last = msgs[-1] if msgs else {}
            record("answer", "收到 done 后库里已有这条助手消息",
                   last.get("role") == "assistant" and (last.get("content") or "")[:60] == answer[:60],
                   f"消息数={len(msgs)} 末条角色={last.get('role')} "
                   f"引用={len(last.get('citations') or [])} 条")
            record("answer", "助手消息带工具轨迹（可审计）",
                   bool(last.get("tool_trace")), f"{len(last.get('tool_trace') or [])} 条")
        except Exception as exc:  # noqa: BLE001
            record("answer", "会话读回", False, f"{type(exc).__name__}: {exc}")
    return run


def _first_diff(a: str, b: str) -> int:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


#: 任意 markdown 图片（`done` 阶段会把非受控的那些删掉，比对时两边一起抹去）
_ANY_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
#: 删图时留下的说明（`strip_foreign_images` 的产物，比对时也要抹去）
_OMIT_NOTE_RE = re.compile(r"（配图已省略：[^）]*）")


def _normalize_final(text: str) -> str:
    """抹掉 `done` 阶段的两处已知改写，只留正文本身的差异。

    两处改写：① 引用编号压成 1..M；② 非受控图片被删成一句说明。
    这样"思维链被当正文推出去"这类问题仍然会让比对失败（任何非图片、非编号的文字差异
    都会被抓到），而正常的编号重排 / 外链图删除不会造成假失败。
    """
    text = _ANY_IMAGE_RE.sub("", text or "")
    text = _OMIT_NOTE_RE.sub("", text)
    return re.sub(r"\[\d{1,3}\]", "", text)


def _dump_diff(streamed: str, final: str, norm) -> None:
    """比对失败时把两份文本落盘 —— 否则只能看到"差了 11 个字"，没法查。"""
    at = _first_diff(norm(streamed), norm(final))
    path = ROOT / "data" / "logs" / "agent_check_diff.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    a, b = norm(streamed), norm(final)
    path.write_text(
        f"首个差异 @{at}\n"
        f"--- 流式增量（{len(a)} 字）---\n{a[max(0, at - 120):at + 160]}\n"
        f"--- done.answer（{len(b)} 字）---\n{b[max(0, at - 120):at + 160]}\n"
        f"--- 流式原文 ---\n{streamed}\n--- done 原文 ---\n{final}\n",
        encoding="utf-8")


# --------------------------------------------------------------------------
# ⑤ 图片链路
# --------------------------------------------------------------------------


def check_figure(base: str, paper: str, projections: dict) -> None:
    print("\n" + "=" * 78)
    print("⑤ 图片链路：问图 → 回答里嵌受控地址 → 该地址可取回真图")
    print("=" * 78)
    target = _first_figure_page(base, paper)
    if not target:
        skip("图片链路", "这篇论文没有可用的插图")
        return
    page, number = target
    run = check_answer(base, paper, f"第 {page} 页的图 {number} 画了什么？请结合图说明。")
    if not run:
        return
    done = run["done"] or {}
    answer = done.get("answer") or ""
    trace = done.get("tool_trace") or []
    tools = [t.get("tool") for t in trace]
    urls = {m.group(0) for m in IMAGE_URL_RE.finditer(answer)}
    record("figure", "问图时用到了图工具",
           any(t in tools for t in ("figure_list", "figure_image_get")), f"工具序={tools}")
    if not urls:
        skip("回答里嵌了受控图片", "本轮模型没配图（允许，但链路未验证）")
        return
    bad = [u for u in urls if f"/papers/{paper}/" not in u]
    record("figure", "只引用本论文的受控地址", not bad, f"其它地址={bad}")
    url = sorted(urls)[0]
    try:
        with urllib.request.urlopen(f"{base}{url}", timeout=60) as resp:
            ctype = resp.headers.get("content-type", "")
            data = resp.read()
        record("figure", "受控图片能取回真图",
               resp.status == 200 and "image" in ctype and len(data) > 5000,
               f"{url} → {resp.status} {ctype} {len(data)} bytes")
    except Exception as exc:  # noqa: BLE001
        record("figure", "受控图片能取回真图", False, f"{url} → {type(exc).__name__}: {exc}")


def _first_figure_page(base: str, paper: str, *, max_pages: int = 12):
    """找第一张有插图的页（避免"这篇论文第 1 页恰好没图就整节跳过"）。"""
    api = f"{base}/api/v1/agent/papers/{paper}"
    internal = {"X-Internal-Token": token()}
    for page in range(1, max_pages + 1):
        try:
            data = get_json(f"{api}/figures?page={page}", headers=internal)["data"]
        except Exception:  # noqa: BLE001
            return None
        items = data.get("items") or []
        if items and items[0].get("index") is not None:
            return page, (items[0].get("number") or "1")
    return None


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 问答链路自检")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--paper", default=DEFAULT_PAPER)
    parser.add_argument("--guardrail-paper", default=DEFAULT_GUARDRAIL_PAPER,
                        dest="guardrail_paper",
                        help="一篇**没有建索引**的论文（硬护栏用例）；传空串跳过")
    parser.add_argument("--quick", action="store_true", help="只跑不调模型的几节")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    paper = args.paper

    if not token():
        print("找不到 data/secrets/agent.token —— 先启动一次主后端（它会生成令牌）")
        return 2

    check_service(base)
    projections = check_projections(base, paper)
    check_guardrail(base, paper, args.guardrail_paper)
    if args.quick:
        skip("真实问答 / 图片链路", "--quick 跳过（要调模型）")
    else:
        check_answer(base, paper, "这篇论文的核心贡献是什么？请给出依据。")
        check_figure(base, paper, projections)

    print("\n" + "=" * 78)
    total = len(results)
    failed = sum(1 for r in results if not r["ok"])
    print(f"共 {total} 项：通过 {total - failed}，失败 {failed}")
    report = ROOT / "data" / "logs" / "agent_check.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(
        {"base": base, "paper": paper, "passed": total - failed, "total": total,
         "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
