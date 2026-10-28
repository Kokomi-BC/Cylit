"""把 13 个技能的 description + 正文开头汇总成一份可编辑的 txt（docs/skills-descriptions.txt）。

用途：用户在这份 txt 上改「技能什么时候该被用到 / 正文开头怎么说」，改完由人写回各自的 SKILL.md ——
所以这份文件必须**永远可以从源文件重新生成**（这就是 `dump`），不许手改完就放着不同步。

跑法：`python scripts/skills_descriptions.py`（幂等：连跑两次结果一样）。
"""

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "data", "skills")
OUT = os.path.join(ROOT, "docs", "skills-descriptions.txt")
TICK = chr(96)


def one_line(s: str) -> str:
    """一条要点 → 一行：去掉行首的列表记号与反引号（正文里的 `tool_x` 保持可读）。"""
    s = s.strip()
    if s.startswith("- ") or s.startswith("* "):
        s = s[2:]
    return s.replace(TICK, "")


def opening(body: str, h1: str) -> str:
    """正文开头：H1 之后的第一段；若紧接着就是小节，就取第一个小节，多句用 ｜ 连成一行。"""
    lines = body.split("\n")
    i = 0
    while i < len(lines) and lines[i].strip() != h1:
        i += 1
    i += 1
    intro, sect_head, sect = [], None, []
    while i < len(lines):
        s = lines[i].strip()
        i += 1
        if s.startswith("#"):
            if intro or sect_head is not None:
                break
            sect_head = s.lstrip("#").strip()
            continue
        if s:
            (intro if sect_head is None else sect).append(one_line(s))
    if intro:
        return " ".join(intro)
    if sect_head:
        return sect_head + "：" + " ｜ ".join(sect)
    return ""


def collect() -> list[dict]:
    """按目录名排序读一遍 SKILL.md（顺序稳定 = 这份 txt 的 diff 只反映真实改动）。"""
    blocks = []
    for d in sorted(x for x in os.listdir(SKILLS) if os.path.isdir(os.path.join(SKILLS, x))):
        p = os.path.join(SKILLS, d, "SKILL.md")
        if not os.path.exists(p):
            continue
        txt = io.open(p, encoding="utf-8").read()
        m = re.match(r"---\n([\s\S]*?)\n---\n", txt)
        fm = m.group(1) if m else ""
        body = txt[m.end():] if m else txt
        name = desc = ""
        for line in fm.split("\n"):
            if line.startswith("name:"):
                name = line[5:].strip()
            elif line.startswith("description:"):
                desc = line[12:].strip()
        mt = re.search(r"^#\s+.+$", body, re.M)
        title = mt.group(0).lstrip("#").strip() if mt else ""
        blocks.append({
            "file": "data/skills/%s/SKILL.md" % d,
            "name": name or d,
            "title": title,
            "desc": desc,
            "opening": opening(body, mt.group(0) if mt else ""),
        })
    return blocks


HEAD = [
    "Cylit 技能描述汇总（源：data/skills/*/SKILL.md，共 %d 个技能）",
    "=" * 78,
    "",
    "怎么用：只改每个块里的 description 和 正文开头 两个字段（冒号后面那一段整体替换即可），",
    "别动 [NN] 编号、name、title 和 ==== 分隔线。改完说一声，我按编号写回各自的 SKILL.md。",
    "",
    "字段说明：",
    "  name        技能 id = 目录名，不要改",
    "  title       技能正文的 H1 标题（给读者看的）",
    "  description SKILL.md frontmatter 的 description —— 模型挑技能时看的就是这一行，",
    "              必须单行、以「。」收尾；这段文字决定「什么时候会用到这个技能」",
    "  正文开头    正文 H1 之后的第一段；若开头直接是小节，就取第一个小节（通常是「什么时候用」），",
    "              多句用 ｜ 连成一行。改它 = 改技能正文开头的措辞",
    "",
    "落地位置（两份，必须一起改）：",
    "  data/skills/<name>/SKILL.md          agent 真正读的那份；data/ 不进 git，部署（git reset）不覆盖它",
    "  backend/agent/skills/<name>/SKILL.md 进 git 的模板；新装/物化时从这里复制",
    "  改完我两处一起写，再单独把 data/skills 那份推上线（prod 的 data/ 是它自己的一份）。",
    "",
]


def render(blocks: list[dict]) -> str:
    out = [HEAD[0] % len(blocks)] + HEAD[1:]
    for i, b in enumerate(blocks, 1):
        out += [
            "=" * 78,
            "[%02d] %s   ·   description %d 字   ·   %s" % (i, b["name"], len(b["desc"]), b["file"]),
            "=" * 78,
            "name: " + b["name"],
            "title: " + b["title"],
            "description: " + b["desc"],
            "正文开头: " + b["opening"],
            "",
        ]
    return "\n".join(out) + "\n"


def main() -> None:
    blocks = collect()
    text = render(blocks)
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(text)
    print("写了 %s（%d 个技能 / %d 行）" % (os.path.relpath(OUT, ROOT), len(blocks), text.count("\n")))
    for b in blocks:
        print("  %-18s description %d 字" % (b["name"], len(b["desc"])))


if __name__ == "__main__":
    main()
