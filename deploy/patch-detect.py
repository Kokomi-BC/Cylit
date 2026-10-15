#!/usr/bin/env python3
"""把 Detect 的「登录后去哪」统一改成门户首页 /Portal/。

用户口径（2026-10-15）：
  1. 登录成功之后直接进门户，而不是先进 Detect 的欢迎页；
  2. 已登录用户直接访问 https://fakenewsdetect.art/ ，也去门户；
  3. **普通用户也要能到引导页**（控制台对他们隐藏），不要被弹去 /Welcome。

⚠️ 这里有两份「登录页」，别再只改一份（我踩过）：
  - `/root/Detect/Login.html`      —— Vite 的**源**（多页入口），改了下次构建才生效；
  - `/root/Detect/dist/Login.html` —— Vite 的**构建产物**，
    而 `app.get('/Login')` 用 `res.sendFile('../dist/Login.html')` 发的是**这一份**。
  表单提交后的跳转是**内联在这份 HTML 里**的（不在 js 包里），压缩后长这样：
      "admin"===i.role?window.location.href="/Admin":window.location.href="/Welcome"
  只改源不改产物 = 线上一点变化都没有。

幂等：锚点找不到就当已经改好。改前备份到 <文件>.bak-<时间戳>。
每条的原文必须**恰好出现一次**，否则报错跳过 —— 宁可不动，也不做半吊子替换。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

LOGIN = Path("/root/Detect/Login.html")
DIST_LOGIN = Path("/root/Detect/dist/Login.html")
APP = Path("/root/Detect/src/app.js")

EDITS: list[tuple[Path, str, str]] = [
    # --- 1. 登录表单提交成功后的落点（管理员 / 普通用户两条分支）—— 源文件
    (LOGIN,
     "window.location.href = '/Admin';",
     "window.location.href = '/Portal/';"),
    (LOGIN,
     "window.location.href = '/Welcome';",
     "window.location.href = '/Portal/';"),

    # --- 2. 同一件事的**构建产物**：压缩后的三元表达式，两个分支都并成 /Portal/
    # 替换后外层仍是 `i.message ? … : window.location.href="/Portal/"`，语法合法。
    (DIST_LOGIN,
     '"admin"===i.role?window.location.href="/Admin":window.location.href="/Welcome"',
     'window.location.href="/Portal/"'),

    # --- 3. 已登录的人再访问 /Login
    (APP,
     "if (isLoginPage) return res.redirect('/Welcome');",
     "if (isLoginPage) return res.redirect('/Portal/');"),

    # --- 4. 已登录用户访问根路径 `/`
    # 这段锚点必须带上前后两行：`return res.redirect('/Welcome')` 在 app.js 里
    # 出现多次，只有这一处是在 `req.path === '/'` 的兜底分支里。
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

    # --- 5. 普通用户撞到 Detect 后台（/Admin）时别再弹去 /Welcome
    (APP,
     "        if (rows.length > 0 && rows[0].role === 'admin') next();\n"
     "        else res.redirect('/Welcome');",
     "        if (rows.length > 0 && rows[0].role === 'admin') next();\n"
     "        else res.redirect('/Portal/');"),

    # --- 6. app.get('/Login') 里那条「已登录就弹走」的分支
    # （正常会被安全中间件先截住，属于死代码；改掉只是不留不一致的落点）
    (APP,
     "            jwt.verify(token, SECRET_KEY);\n"
     "            return res.redirect('/Welcome');\n"
     "        } catch { }",
     "            jwt.verify(token, SECRET_KEY);\n"
     "            return res.redirect('/Portal/');\n"
     "        } catch { }"),
]

stamp = time.strftime("%Y%m%d-%H%M%S")
changed = 0
for path, old, new in EDITS:
    if not path.is_file():
        print(f"[SKIP] 文件不存在：{path}", file=sys.stderr)
        continue
    text = path.read_text(encoding="utf-8")
    if old not in text:
        print(f"[OK]   {path} 无需改动（{old.splitlines()[0][:40]}…）")
        continue
    n = text.count(old)
    if n != 1:
        print(f"[FAIL] {path} 里锚点出现 {n} 次（要求恰好 1 次），已跳过", file=sys.stderr)
        continue
    shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{stamp}"))
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"[EDIT] {path.name}：{old.splitlines()[0][:48]}… -> {new.splitlines()[0][:48]}…")
    changed += 1

print(f"完成，改了 {changed} 处")
