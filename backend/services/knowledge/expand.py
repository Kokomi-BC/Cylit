"""查询扩展：把**用户写出来的词**归一成**正文里的写法**（只服务词法检索）。

为什么需要它（线上实测，turboquant 篇，mode=text）：

- TURBOQUANT_mse → **0 条**：正文写作 TURBOQUANT$_{\text{mse}}$，而分词把下划线
  当词内字符，查询变成**整 token** turboquant_mse，永远等不上；把下划线拆开
  （TurboQuant MSE）就是 20 条命中。
- SLB 与 Shannon lower bound 各回 8 条、**只重叠 4 条**（并集 12）：
  同一概念，两组各看到一半，因为查询前没有缩写 ↔ 全称扩展。

两条规则都是**确定性**的，不调模型、不需要额外索引：

1. 下标写法（X_y / X_{y}）→ 拆词，做在 retrieval.tokenize 里（查询侧与正文侧
   同一套规则，两边必须一致，否则又是「只有一半命中」）；
2. 缩写 ↔ 全称 → 从**这篇论文自己的正文**学「全称 (缩写)」「缩写 (全称)」句式，
   双向补词。

**只认「像缩写」的括号**（全大写或含数字、2~7 字符）：Online (data-oblivious) 这类
同义并置**不学** —— online 在正文里常常只是普通形容词，把它并成 data-oblivious 的
同义词，检索会带进整段无关正文，翻译也会被带偏（术语表要求一对一）。
"""
from __future__ import annotations

import re
from typing import Iterable

#: 一次查询最多扩展到多少个词。扩展是「多加几个词」，不是「把查询变成一整段正文」：
#: BM25 对每个词都算分，词越多越容易被周边词面带着走（实测 dequantization 段就是被
#: 邻词带进 data-oblivious 组的）。
MAX_TERMS = 24
#: 最多学多少条别名（同一篇里 X (ABBR) 句式可能上百条，全学等于把查询撑大）。
MAX_ALIASES = 40
#: 扫描上限：别名只在「这次真的要用」时才学，学习本身不许把检索拖慢。
SCAN_ROWS = 4000
SCAN_CHARS = 3_000_000

#: 全称开头的冠词/代词：The Shannon Lower Bound (SLB) 的 full 是 Shannon Lower Bound，
#: 不去掉 The 的话扩展出来的词会多一个到处都有的 the（idf 极低，纯噪声）。
_LEAD_STOP = {"the", "a", "an", "this", "that", "these", "those", "our", "its",
              "their", "his", "her", "we", "i", "here", "there"}

_WORD = r"[A-Za-z][A-Za-z0-9\-]*"
_CAPWORD = r"[A-Z][A-Za-z0-9\-]*"
#: 一次扫出「某某 (某某)」；左边**必须以大写字母开头** —— 这一条既是排版事实（术语定义句
#: 的首词都大写），也是防止把「as shown in the Shannon Lower Bound (SLB)」里的
#: as/shown/in 一起当成全称的一部分（正则从最左的可匹配处起，小写词起不了头）。
_PAIR = re.compile(
    rf"({_CAPWORD}(?:\s+{_WORD}){{0,5}})\s*\(\s*"
    rf"({_WORD}(?:\s+{_WORD}){{0,5}})\s*\)")

#: 学过的别名表，按 (作用域, 行数) 缓存：学一次只花一次正则扫描的钱，之后每次查询白拿。
_CACHE: dict[tuple, dict[str, tuple[str, ...]]] = {}
_CACHE_MAX = 64


def looks_like_abbr(text: str) -> bool:
    """括号里那串**像不像缩写**：全大写（可带数字）或含数字，2~7 字符。

    SLB / MSE / QJL / R2 算；data-oblivious / big / Is 不算。
    """
    body = (text or "").strip()
    if not (2 <= len(body) <= 7):
        return False
    if not any(ch.isalpha() for ch in body):
        return False
    if body.isupper() or any(ch.isdigit() for ch in body):
        return True
    # 复数形式（LLMs / QJLs）：只有末位小写 s，其余全大写，且大写字母不少于两个
    return (len(body) >= 4 and body.endswith("s") and body[:-1].isupper()
            and sum(ch.isupper() for ch in body) >= 2)


def _words(text: str) -> list[str]:
    """按**分词口径**把一串词切成 token（Johnson-Lindenstrauss → 两个词）。

    必须与 retrieval.tokenize 的英文口径一致：扩展出来的词要能对上班里的 token，
    差一个连字符就等于没扩展。
    """
    return re.findall(r"[a-z0-9]+", text.lower())


def _initials(words: list[str]) -> list[str]:
    """词首字母（连字符按两段算：Johnson-Lindenstrauss → J、L）。"""
    out: list[str] = []
    for word in words:
        for part in re.split(r"[-\u2010]", word):
            if part[:1].isalpha():
                out.append(part[0].upper())
    return out


def _window(words: list[str], abbr: str) -> list[str] | None:
    """在 words 里找**最短的后缀窗口**，它的首字母能按顺序拼出 abbr。

    用途：正则从最左的大写词起匹配，会把句子开头的 Using / As 也吃进全称
    （实测「Using the Random Orthogonal Rotation (ROR)」→ full 里多了 using/the）。
    首字母对得上才算全称，这条同时是**对齐校验**：对不上的长串多半根本不是术语。
    """
    letters = [c for c in abbr.upper() if c.isalpha()]
    if not letters:
        return None

    def match(want: list[str]) -> list[str] | None:
        for size in range(1, min(len(words), 6) + 1):
            pos = 0
            for initial in _initials(words[-size:]):
                if pos < len(want) and initial == want[pos]:
                    pos += 1
            if pos == len(want):
                return words[-size:]
        return None

    hit = match(letters)
    if hit is not None:
        return hit
    # 复数形式（LLMs）：缩写不是全大写、末位是 s，就再去掉那个 s 试一次
    if not abbr.isupper() and abbr.lower().endswith("s") and len(letters) > 3:
        return match(letters[:-1])
    return None


def _clean_full(full: str) -> list[str]:
    words = [w for w in full.split() if w.strip()]
    while words and words[0].lower().strip(".,;:()") in _LEAD_STOP:
        words.pop(0)
    while words and words[-1].lower().strip(".,;:()") in _LEAD_STOP:
        words.pop()
    return words


def learn(texts: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """从正文里学「全称 ↔ 缩写」，返回 {小写键: 要补的词}。

    两个方向都存：
    - "slb" -> ("shannon", "lower", "bound")（查到缩写时补全称）；
    - "shannon lower bound" -> ("slb",)（查到全称时补缩写）。
    """
    out: dict[str, tuple[str, ...]] = {}
    scanned = 0
    for text in texts:
        if scanned >= SCAN_CHARS or len(out) >= MAX_ALIASES:
            break
        body = str(text or "")
        scanned += len(body)
        if "(" not in body:            # 便宜的前置过滤：没有括号就学不到东西
            continue
        for match in _PAIR.finditer(body):
            left = match.group(1).strip()
            right = match.group(2).strip()
            # 哪一边是缩写：全称有多词、缩写只有一个词 —— 这个判断同时把
            # 「某词 (普通单词)」挡在外面（Transformer (big) / Online (data-oblivious)）
            if " " not in right and looks_like_abbr(right):
                abbr, full = right, left
            elif " " not in left and looks_like_abbr(left):
                abbr, full = left, right
            else:
                continue
            if "-" in abbr:
                continue
            full_words = _clean_full(full)
            window = _window(full_words, abbr)
            if window is None:      # 首字母对不上 = 不是「缩写 ↔ 全称」，宁可不学
                continue
            full_tokens: list[str] = []
            for word in window:
                full_tokens.extend(_words(word))
            if not (2 <= len(full_tokens) <= 6) or len(window) < 2:
                continue
            abbr_tokens = _words(abbr)
            if len(abbr_tokens) != 1:
                continue
            out.setdefault(abbr_tokens[0], tuple(full_tokens))
            out.setdefault(" ".join(full_tokens), (abbr_tokens[0],))
    return out


def aliases_for(rows: list[dict], *, key: object = None) -> dict[str, tuple[str, ...]]:
    """这次检索用得上的别名表（按 (作用域, 行数) 缓存）。

    key 由调用方给（词法检索传 paper_ids）：缓存只按**作用域**分，不按 chunk 类型 /
    语言分 —— 别名是「这篇论文怎么称呼这个概念」的全局事实，与这次只查哪几类块无关。
    """
    sig = (key, len(rows))
    hit = _CACHE.get(sig)
    if hit is not None:
        return hit
    table = learn(str(r.get("content") or "") for r in rows[:SCAN_ROWS])
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[sig] = table
    return table


def expand_terms(tokens: list[str], query: str,
                 aliases: dict[str, tuple[str, ...]]) -> list[str]:
    """查询 token 列表 → 补上别名那一侧的词（保序去重，最多 MAX_TERMS 个）。

    aliases 为空（正文里没有可学的句式）时原样返回 —— 不做任何「通用缩写表」猜测：
    同名缩写在不同领域含义完全不同，凭记忆展开比不展开更糟。
    """
    del query                              # 词组匹配用 token 串，不用原始串
    if not tokens or not aliases:
        return tokens
    out = list(tokens)
    phrase = " ".join(tokens)
    for key, extra in aliases.items():
        if " " in key:
            if key in phrase:
                out.extend(extra)
        elif key in tokens:
            out.extend(extra)
    seen: list[str] = []
    for token in out:
        if token and token not in seen:
            seen.append(token)
    return seen[:MAX_TERMS]
