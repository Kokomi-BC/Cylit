"""DeepSeek 模型连通性与多模态能力探测（一次性脚本，可直接复用）。

用法：
    python scripts/probe_deepseek.py --key sk-xxx
    python scripts/probe_deepseek.py --key-file data/secrets/deepseek.key
    python scripts/probe_deepseek.py --key sk-xxx --image path/to.png

测试内容：
1. 列模型名（对若干候选做探测，报告哪些可用、路由到哪个真实模型）
2. 纯文本生成（延迟 / token / 是否返回 prompt_cache_hit_tokens）
3. KV 缓存命中（同一长前缀连发两次，比较 cache hit token）
4. 图像理解（base64 内联 + 文字识别），验证多模态是否可用
5. 结构化 JSON 输出（本项目翻译用的契约）
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "https://api.deepseek.com/v1"
CANDIDATE_MODELS = [
    "deepseek-flash",
    "deepseek-chat",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4-pro",
    "deepseek-reasoner",
    "deepseek-v4.1-flash",
]


def call(base: str, key: str, payload: dict, timeout: float = 120.0) -> tuple[int, dict]:
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{base}/chat/completions", json=payload, timeout=timeout,
                       headers={"Authorization": f"Bearer {key}"})
        elapsed = (time.perf_counter() - t0) * 1000
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:300]}
        data["_latency_ms"] = round(elapsed, 1)
        return r.status_code, data
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": {"message": f"{type(exc).__name__}: {exc}"},
                   "_latency_ms": round((time.perf_counter() - t0) * 1000, 1)}


def ok(data: dict) -> str:
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def usage_line(data: dict) -> str:
    u = data.get("usage") or {}
    return (f"tokens={u.get('total_tokens')} "
            f"prompt={u.get('prompt_tokens')} completion={u.get('completion_tokens')} "
            f"cache_hit={u.get('prompt_cache_hit_tokens')} "
            f"cache_miss={u.get('prompt_cache_miss_tokens')} "
            f"latency={data.get('_latency_ms')}ms")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key")
    ap.add_argument("--key-file")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--image", help="用于视觉测试的图片路径")
    ap.add_argument("--model", default="deepseek-flash")
    args = ap.parse_args()

    key = args.key
    if not key and args.key_file:
        key = Path(args.key_file).read_text(encoding="utf-8").strip()
    if not key:
        print("需要 --key 或 --key-file")
        return 2

    print("=" * 72)
    print("1) 模型名探测")
    print("=" * 72)
    available = []
    for m in CANDIDATE_MODELS:
        code, data = call(args.base, key, {
            "model": m, "messages": [{"role": "user", "content": "只回答 OK"}],
            "max_tokens": 8, "temperature": 0,
        })
        if code == 200:
            real = data.get("model", "?")
            available.append((m, real))
            print(f"  [OK]   {m:<32} -> 实际模型 {real}")
        else:
            msg = (data.get("error") or {}).get("message", str(data))[:110]
            print(f"  [FAIL] {m:<32} HTTP {code}: {msg}")
    if not available:
        print("\n没有任何模型可用，请检查 key / base_url")
        return 1

    model = args.model if any(m == args.model for m, _ in available) else available[0][0]
    print(f"\n后续测试使用 model={model}")

    print("\n" + "=" * 72)
    print("2) 纯文本生成")
    print("=" * 72)
    code, data = call(args.base, key, {
        "model": model,
        "messages": [{"role": "user", "content": "用一句话说明什么是向量量化。"}],
        "max_tokens": 120,
    })
    print(f"  HTTP {code}  {usage_line(data)}")
    print(f"  输出：{ok(data)[:160]}")

    print("\n" + "=" * 72)
    print("3) KV 前缀缓存（同一长前缀连发 2 次）")
    print("=" * 72)
    long_system = ("你是学术论文翻译专家。" + "以下为固定规则，整篇不变。" * 220)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": long_system},
                     {"role": "user", "content": "把 'random rotation' 译成中文，只输出译文。"}],
        "max_tokens": 24, "temperature": 0,
    }
    for i in (1, 2):
        code, data = call(args.base, key, payload)
        u = data.get("usage") or {}
        print(f"  第 {i} 次：HTTP {code} prompt={u.get('prompt_tokens')} "
              f"cache_hit={u.get('prompt_cache_hit_tokens')} "
              f"cache_miss={u.get('prompt_cache_miss_tokens')} "
              f"{data.get('_latency_ms')}ms")

    print("\n" + "=" * 72)
    print("4) 图像理解（多模态）")
    print("=" * 72)
    if not args.image:
        print("  未提供 --image，跳过")
    else:
        img = Path(args.image)
        b64 = base64.b64encode(img.read_bytes()).decode("ascii")
        content = [
            {"type": "text",
             "text": "请准确读出图片中的所有文字（逐字照抄），并说明图形里有什么。"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
        code, data = call(args.base, key, {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 300,
        }, timeout=180)
        print(f"  HTTP {code}  {usage_line(data)}")
        if code == 200:
            answer = ok(data)
            print(f"  回答：{answer[:400]}")
            for probe in ("PAPERMIND-2026-QX7", "2.7207", "p03-b12"):
                print(f"    {'命中' if probe in answer else '未命中'}  期望包含：{probe}")
        else:
            print(f"  失败：{(data.get('error') or {}).get('message', data)}")
            print("  → 该模型可能不支持图片输入，请改用 deepseek-v4-flash-vision-exp")

    print("\n" + "=" * 72)
    print("5) 结构化 JSON（本项目翻译契约）")
    print("=" * 72)
    code, data = call(args.base, key, {
        "model": model,
        "messages": [
            {"role": "system", "content": "只输出 JSON 对象，不要解释或代码块。"},
            {"role": "user", "content": (
                '把 "The distortion bound is 2.7207." 译成中文，输出 '
                '{"translation_md": str, "terms_used": [str], "uncertain": [str], '
                '"confidence": float}')},
        ],
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    })
    text = ok(data)
    print(f"  HTTP {code}  {usage_line(data)}")
    try:
        parsed = json.loads(text)
        print(f"  JSON 解析成功：{json.dumps(parsed, ensure_ascii=False)[:240]}")
    except ValueError:
        print(f"  非合法 JSON：{text[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
