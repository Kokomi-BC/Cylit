"""回答里的配图清洗：只放行**本论文自己的 region 裁图**，其余一律换成一句人话。

实测（2026-12-07）用户看到的是「（配图已省略：第 9 页表 1：…）」而不是那张表 —— 模型写的是
`/cylit/api/v1/papers/<id>/figures/region/9/0?v=…`（站点部署在 `/cylit/` 子路径下，它抄来了带前缀
的绝对地址），白名单只认 `/api/v1/papers/…`，于是合法裁图被当外链删掉。
"""
from __future__ import annotations

from backend.agent.sanitize import strip_foreign_images

PAPER = "turboquant-online-vector-iclr-26"
REGION = f"/api/v1/papers/{PAPER}/figures/region/9/0?v=aca40daa&card=1"


def test_keeps_the_region_crop_with_a_deployment_prefix() -> None:
    """带 `/cylit` 前缀 / 带域名 / 少开头 `/` 的同一张裁图都要放行（并重写成绝对路径）。"""
    for url in (REGION,
                "/cylit" + REGION,
                f"https://fakenewsdetect.art/cylit{REGION}",
                "cylit" + REGION):
        out = strip_foreign_images(f"![第 9 页表 1]({url})", PAPER)
        assert out.startswith("![第 9 页表 1]("), url
        assert "配图已省略" not in out, url
        assert out.endswith(REGION + ")"), out


def test_still_drops_foreign_images() -> None:
    """站外图 / 别的论文的裁图 / 非 region 的静态图，照旧换成一句人话。"""
    for url in ("https://arxiv.org/abs/1706.03762",
                "/api/v1/papers/other-paper/figures/region/9/0",
                f"/papers/{PAPER}/formula_crops/x@4x.png",
                "data:image/png;base64,AAAA"):
        out = strip_foreign_images(f"![某图]({url})", PAPER)
        assert out == "（配图已省略：某图）", (url, out)

