#!/usr/bin/env python3
"""把门户 / Cylit 的 location 片段挂进 detect.conf 的 443 server 块。

幂等：已经 include 过就不动。改前自动备份到 detect.conf.bak-<时间戳>。
"""
from __future__ import annotations

import re
import shutil
import sys
import time
from pathlib import Path

CONF = Path("/etc/nginx/sites-available/detect.conf")
ANCHOR = "    client_max_body_size 100M;"
INCLUDES = [
    "    # ===== 服务门户 + Cylit（由 /root/deploy/patch-nginx.py 注入）=====",
    "    include /etc/nginx/snippets/portal.conf;",
    "    include /etc/nginx/snippets/cylit.conf;",
]


def main() -> int:
    text = CONF.read_text(encoding="utf-8")

    if "snippets/portal.conf" in text and "snippets/cylit.conf" in text:
        print("already patched, nothing to do")
        return 0

    if text.count(ANCHOR) != 1:
        print(f"anchor not unique ({text.count(ANCHOR)} hits): {ANCHOR!r}", file=sys.stderr)
        return 1

    # 锚点必须落在 fakenewsdetect.art 的 443 server 块里
    head = text[: text.index(ANCHOR)]
    if head.count("listen 443 ssl http2;") != 1:
        print("anchor is not inside the (single) 443 server block", file=sys.stderr)
        return 1

    bak = CONF.with_suffix(f".conf.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CONF, bak)

    new = text.replace(ANCHOR, ANCHOR + "\n" + "\n".join(INCLUDES), 1)
    CONF.write_text(new, encoding="utf-8")
    print(f"patched {CONF} (backup: {bak})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
