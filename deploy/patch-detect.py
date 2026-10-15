#!/usr/bin/env python3
"""把 Detect 的「已登录后去哪」统一改成门户首页 /Portal/。

用户口径（2026-10-15）：
  1. 登录成功之后直接进门户，而不是先进 Detect 的欢迎页；
  2. 已登录用户直接访问 https://fakenewsdetect.art/ ，也去门户。

幂等：已经是 /Portal/ 就什么都不做。改前备份到 <文件>.bak-<时间戳>。
每条的原文必须**恰好出现一次**，否则报错跳过 —— 宁可不动，也不做半吊子替换。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

LOGIN = Path("/root/Detect/Login.html")
APP = Path("/root/Detect/src/app.js")

EDITS: list[tuple[Path, str, str]] = [
    # --- 1. 登录表单提交成功后的落点（管理员 / 普通用户两条分支）
    (LOGIN,
     "window.location.href = '/Admin';",
     "window.location.href = '/Portal/';"),
    (LOGIN,
     "window.location.href = '/Welcome';",
     "window.location.href = '/Portal/';"),
    # 已登录的人再访问 /Login
    (APP,
     "if (isLoginPage) return res.redirect('/Welcome');",
     "if (isLoginPage) return res.redirect('/Portal/');"),
    # --- 2. 已登录用户访问根路径 `/`
    # 这段锚点必须带上前后两行：`return res.redirect('/Welcome')` 在 app.js 里
    # 出现 3 次，只有这一处是在 `req.path === '/'` 的兜底分支里。
    (APP,
     "                jwt.verify(token, SECRET_KEY);\n"
     "                return res.redirect('/Welcome');\n"
     "            } catch {}\n"
     "        }\n"
     "        return res.redirect('/Login');",
     "                jwt.verify(token, SECRET_KEY);\n"
     "                return res.redirect('/Portal/');\n"
     "            } catch {}\n"
     "        }\n"
     "        return res.redirect('/Login');"),
]

stamp = time.strftime("%Y%m%d-%H%M%S")
changed = 0
for path, old, new in EDITS:
    if not path.is_file():
        print(f"[SKIP] 文件不存在：{path}", file=sys.stderr)
        continue
    text = path.read_text(encoding="utf-8")
    if old not in text:
        print(f"[OK]   {path.name} 无需改动")
        continue
    n = text.count(old)
    if n != 1:
        print(f"[FAIL] {path} 里锚点出现 {n} 次（要求恰好 1 次），已跳过", file=sys.stderr)
        continue
    shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{stamp}"))
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"[EDIT] {path}：{old.splitlines()[0][:52]}… -> {new.splitlines()[0][:52]}…")
    changed += 1

print(f"完成，改了 {changed} 处")
