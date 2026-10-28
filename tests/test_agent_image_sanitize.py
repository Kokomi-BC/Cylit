"""回答里的配图清洗：放行任意 https 图 + 本站受控图；`http:` 降级成链接；危险 scheme 整条删。

2026-12 用户拍板的新口径（取代"只放行本轮读过的论文"那版）：
- 本站受控图（**任意论文**的 `/api/v1/papers/<id>/{figures,pages}/…`、`/papers/<id>/…`）→ 放行，
  并规整成绝对路径（`/cylit` 部署前缀、缺前导 `/`、写成整条 https 的，都要削成正路）；
- 站外 https → 放行，**原样保留**（不改域名、不猜它是不是真图）；
- http → 降级成链接 `[alt](url)`（https 站点上的 http 图必然是混合内容裂图）；
- `data:` / `blob:` / `javascript:` / `file:` / 其它非 http(s) scheme → 整条删成纯文本，URL 不留在回答里。

历史（留着，说明这条规则是被真机事故推着改的）：
- 2026-12-07：带 `/cylit` 前缀的合法裁图被当外链删；
- 2026-10-28（生产）：**另一篇**论文 `attention-cleanroom` 的裁图被删 —— 当时白名单只按当前论文拼前缀。
"""
from __future__ import annotations

from backend.agent.sanitize import strip_foreign_images

PAPER = "turboquant-online-vector-iclr-26"
OTHER = "attention-cleanroom"
REGION = f"/api/v1/papers/{PAPER}/figures/region/9/0?v=aca40daa&card=1"
OTHER_REGION = f"/api/v1/papers/{OTHER}/figures/region/3/0?v=7080945bc0"
REMOTE = "https://arxiv.org/html/1706.03762/assets/img.png"
PLACEHOLDER = "（配图已省略：某图）"


def test_keeps_this_paper_figure_and_normalizes_to_absolute_path() -> None:
    """a) 本篇受控裁图：相对路径 / 带 `/cylit` / 写成整条 https 的，都放行并规整成 `/api/v1/…`。"""
    for url in (REGION,
                "/cylit" + REGION,
                f"https://fakenewsdetect.art/cylit{REGION}",
                "cylit" + REGION):
        out = strip_foreign_images(f"![第 9 页表 1]({url})", PAPER)
        assert out == f"![第 9 页表 1]({REGION})", (url, out)


def test_keeps_another_paper_figure() -> None:
    """b) **别的论文**的受控裁图同样放行（2026-10-28 生产日志里被删的就是这一张）。

    带 `/cylit` 前缀、缺前导 `/` 的畸形写法一起钉住：先规整成绝对路径再放行。
    """
    for url in (OTHER_REGION,
                "/cylit" + OTHER_REGION,
                "cylit" + OTHER_REGION):
        out = strip_foreign_images(f"![引用 transformer 的图]({url})", PAPER)
        assert out == f"![引用 transformer 的图]({OTHER_REGION})", (url, out)
        assert "配图已省略" not in out


def test_accepts_every_controlled_route_of_any_paper() -> None:
    """受控图三条路线（region 裁图 / 整页图 / 静态裁图）对**任意**论文都放行。"""
    for pid in (PAPER, OTHER):
        for url in (f"/api/v1/papers/{pid}/figures/region/8/1?v=575fa11dc0&card=1",
                    f"/api/v1/papers/{pid}/pages/8/image?dpi=150",
                    f"/papers/{pid}/formula_crops/p03-b05@4x.png"):
            out = strip_foreign_images(f"![图]({url})", PAPER)
            assert out == f"![图]({url})", (pid, url, out)


def test_keeps_foreign_https_image_verbatim() -> None:
    """c) 站外 https → 放行，且原样保留（域名 / 查询串一个字符都不改）。"""
    out = strip_foreign_images(f"![Transformer 架构图]({REMOTE})", PAPER)
    assert out == f"![Transformer 架构图]({REMOTE})", out
    assert "配图已省略" not in out
    # 协议相对地址在我们这种 https 站点上同样是 https，原样放行
    out = strip_foreign_images("![图](//cdn.example.com/a.png)", PAPER)
    assert out == "![图](//cdn.example.com/a.png)", out


def test_downgrades_http_image_to_a_plain_link() -> None:
    """d) `http://` → `[alt](url)`：可点，但**不含** `![`（否则就是混合内容裂图）。"""
    url = "http://example.com/assets/x.png"
    out = strip_foreign_images(f"![旧的图]({url})", PAPER)
    assert out == f"[旧的图]({url})", out
    assert "![" not in out
    assert "配图已省略" not in out


def test_drops_dangerous_schemes_entirely() -> None:
    """e) `data:` / `blob:` / `javascript:` / `file:` / 其它 scheme → 整条删，URL 不出现在结果里。"""
    for url in ("data:image/png;base64,AAAA",
                "blob:https://fakenewsdetect.art/1234",
                "javascript:alert%281%29",
                "file:///C:/secret.png",
                "ftp://example.com/x.png"):
        out = strip_foreign_images(f"![某图]({url})", PAPER)
        assert out == PLACEHOLDER, (url, out)
        assert "http" not in out and "alert" not in out and "secret" not in out, out


def test_old_foreign_url_assertion_is_now_allowed_but_guardrail_stays() -> None:
    """f) 老断言"站外 / 别的论文被删成（配图已省略：…）"按新口径改成**放行**；

    危险 scheme 那条**保留**（护栏不整体删掉 —— 四条 scheme 见上一个测试）。
    """
    for url in ("https://arxiv.org/abs/1706.03762",
                "/api/v1/papers/other-paper/figures/region/9/0",
                "/papers/other-paper/formula_crops/x@4x.png"):
        out = strip_foreign_images(f"![某图]({url})", PAPER)
        assert "配图已省略" not in out, (url, out)
    # 护栏还在：危险 scheme 照旧整条删
    assert strip_foreign_images("![某图](data:image/png;base64,AAAA)", PAPER) == PLACEHOLDER


def test_keeps_alt_when_the_markdown_carries_a_title() -> None:
    """带 title 的写法不丢 alt（老行为）。"""
    out = strip_foreign_images(f'![结构图]({REMOTE} "Figure 1")', PAPER)
    assert out == f"![结构图]({REMOTE})", out


def test_result_does_not_depend_on_which_paper_is_current() -> None:
    """放行只看 scheme / 受控路径：**换哪一篇当"当前论文"，结果都一样**。

    （老版本这条验的是"allowed_paper_ids 被忽略"；那个形参 2026-12 已删 ——
    现在连"当前论文"也不影响放行，它只进日志。）
    """
    text = f"![图]({OTHER_REGION})"
    alone = strip_foreign_images(text, PAPER)
    assert alone == strip_foreign_images(text, OTHER)
    assert alone == strip_foreign_images(text, "")
