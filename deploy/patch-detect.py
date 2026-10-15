#!/usr/bin/env python3
"""把 Detect 的「登录后落点」从 /Welcome 改成门户首页 /Portal/。

用户口径（2026-10-15）：登录完直接进门户，而不是先进 Detect 的欢迎页再自己找。
幂等：已经是 /Portal/ 就什么都不做。改前备份到 <文件>.bak-<时间戳>。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

# (文件, 原文, 新文) —— 原文必须**恰好出现一次**，否则报错退出，不做半吊子替换
EDITS: list[tuple[Path, str, str]] = [
    (Path("/root/Detect/Login.html"),
     "window.location.href = '/Admin';",
     "window.location.href = '/Portal/';"),
    (Path("/root/Detect/Login.html"),
     "window.location.href = '/Welcome';",
     "window.location.href = '/Portal/';"),
    # 已登录的人再访问 /Login：原来弹去 /Welcome，现在同样去门户（与上面的落点一致）
    (Path("/root/Detect/src/app.js"),
     "if (isLoginPage) return res.redirect('/Welcome');",
     "if (isLoginPage) return res.redirect('/Portal/');"),
]

stamp = time.strftime("%Y%m%d-%H%M%S")
changed = 0
for path, old, new in EDITS:
    if not path.is_file():
        print(f"[SKIP] 文件不存在：{path}", file=sys.stderr)
        continue
    text = path.read_text(encoding="utf-8")
    if new in text and old not in text:
        print(f"[OK]   {path.name} 已经是新落点")
        continue
    n = text.count(old)
    if n != 1:
        print(f"[FAIL] {path} 里 {old!r} 出现 {n} 次（要求恰好 1 次）", file=sys.stderr)
        continue
    shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{stamp}"))
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"[EDIT] {path}：{old} -> {new}")
    changed += 1

print(f"完成，改了 {changed} 处")
