"""回答里的配图清洗：站外图改走本站代理 + 本站受控图不代理；http 降级；危险 scheme 整条删。

2026-12 收尾后的口径（在"放行任意 https"之上再收一步）：

- 本站受控图（**任意论文**的 /api/v1/papers/<id>/{figures,pages}/…、/papers/<id>/…）→ 放行，
  规整成绝对路径，**不做代理**（它本来就在本站，代理一次纯属多一跳）；
- 站外 https → **改写成本站代理地址** /api/v1/webimg?url=<percent-encoded>：由后端抓一次
  落盘再由本站提供，浏览器不再直连第三方（否则用户 IP / referer 泄露给图片站）；
- http → 降级成链接 [alt](url)（https 站点上的 http 图必然混合内容裂图）；
- data: / blob: / javascript: / file: / 其它非 http(s) scheme → 整条删成纯文本；
- **流式**期间成形的图片 markdown 换成 alt（不闪原始 URL），半个片段原样保留不外推。

历史（留着，说明这条规则是被真机事故推着改的）：
- 2026-12-07：带 /cylit 前缀的合法裁图被当外链删；
- 2026-10-28（生产）：**另一篇**论文 attention-cleanroom 的裁图被删 —— 白名单只按当前论文拼前缀。
"""
from __future__ import annotations

from urllib.parse import quote

from backend.agent.citations import RefRegistry
from backend.agent.sanitize import StreamingAnswerSanitizer, strip_foreign_images

PAPER = "turboquant-online-vector-iclr-26"
OTHER = "attention-cleanroom"
REGION = f"/api/v1/papers/{PAPER}/figures/region/9/0?v=aca40daa&card=1"
OTHER_REGION = f"/api/v1/papers/{OTHER}/figures/region/3/0?v=7080945bc0"
REMOTE = "https://arxiv.org/html/1706.03762/assets/img.png"
PLACEHOLDER = "（配图已省略：某图）"


def proxied(url: str) -> str:
    """期望的代理地址（独立按 urllib 拼一遍，不借用被测实现的同一个函数）。"""
    return "/api/v1/webimg?url=" + quote(url, safe="")


def test_keeps_this_paper_figure_and_normalizes_to_absolute_path() -> None:
    """a) 本篇受控裁图：相对路径 / 带 /cylit / 写成整条 https 的，都放行并规整成 /api/v1/…。"""
    for url in (REGION,
                "/cylit" + REGION,
                f"https://fakenewsdetect.art/cylit{REGION}",
                "cylit" + REGION):
        out = strip_foreign_images(f"![第 9 页表 1]({url})", PAPER)
        assert out == f"![第 9 页表 1]({REGION})", (url, out)


def test_keeps_another_paper_figure() -> None:
    """b) **别的论文**的受控裁图同样放行（2026-10-28 生产日志里被删的就是这一张）。"""
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


def test_controlled_images_are_never_proxied() -> None:
    """本站受控图**不进代理**：地址里不出现 /webimg，浏览器也不用绕一跳。"""
    for url in (REGION, OTHER_REGION, f"/papers/{PAPER}/figures/x.png"):
        out = strip_foreign_images(f"![图]({url})", PAPER)
        assert "webimg" not in out, out
        assert url in out or f"/api/v1/papers/{PAPER}" in out, out


def test_foreign_https_image_is_rewritten_to_the_local_proxy() -> None:
    """c) 站外 https → /api/v1/webimg?url=…（alt 保留、整条 URL 百分号转义）。"""
    out = strip_foreign_images(f"![Transformer 架构图]({REMOTE})", PAPER)
    assert out == f"![Transformer 架构图]({proxied(REMOTE)})", out
    assert "配图已省略" not in out
    # 浏览器不再直连第三方：回答里**没有**那个域名开头的原始地址
    assert REMOTE not in out
    assert "https%3A%2F%2Farxiv.org%2Fhtml%2F1706.03762%2Fassets%2Fimg.png" in out


def test_protocol_relative_image_is_proxied_as_https() -> None:
    """协议相对地址（在我们这种 https 站点上就是 https）同样走代理，且补全成 https://。"""
    out = strip_foreign_images("![图](//cdn.example.com/a.png)", PAPER)
    assert out == f"![图]({proxied('https://cdn.example.com/a.png')})", out


def test_downgrades_http_image_to_a_plain_link() -> None:
    """d) http:// → [alt](url)：可点，但**不含** ![（否则就是混合内容裂图），也不进代理。"""
    url = "http://example.com/assets/x.png"
    out = strip_foreign_images(f"![旧的图]({url})", PAPER)
    assert out == f"[旧的图]({url})", out
    assert "![" not in out
    assert "webimg" not in out
    assert "配图已省略" not in out


def test_drops_dangerous_schemes_entirely() -> None:
    """e) data: / blob: / javascript: / file: / 其它 scheme → 整条删，URL 不出现在结果里。"""
    for url in ("data:image/png;base64,AAAA",
                "blob:https://fakenewsdetect.art/1234",
                "javascript:alert%281%29",
                "file:///C:/secret.png",
                "ftp://example.com/x.png"):
        out = strip_foreign_images(f"![某图]({url})", PAPER)
        assert out == PLACEHOLDER, (url, out)
        assert "http" not in out and "alert" not in out and "secret" not in out, out


def test_dangerous_schemes_are_never_proxied() -> None:
    """危险 scheme 不能因为"改写"而漏进代理参数里。"""
    out = strip_foreign_images("![某图](javascript:alert%281%29)", PAPER)
    assert out == PLACEHOLDER
    assert "webimg" not in out


def test_keeps_alt_when_the_markdown_carries_a_title() -> None:
    """带 title 的写法不丢 alt（老行为），地址照样进代理。"""
    out = strip_foreign_images(f'![结构图]({REMOTE} "Figure 1")', PAPER)
    assert out == f"![结构图]({proxied(REMOTE)})", out


def test_result_does_not_depend_on_which_paper_is_current() -> None:
    """放行/改写只看 scheme 与受控路径：换哪一篇当"当前论文"，结果都一样。"""
    text = f"![图]({OTHER_REGION})"
    alone = strip_foreign_images(text, PAPER)
    assert alone == strip_foreign_images(text, OTHER)
    assert alone == strip_foreign_images(text, "")
    remote = f"![图]({REMOTE})"
    assert strip_foreign_images(remote, PAPER) == strip_foreign_images(remote, "")


# --------------------------------------------------------------------------
# 流式：成形图片换 alt，半个片段不外推
# --------------------------------------------------------------------------


def test_streaming_replaces_a_complete_image_with_its_alt() -> None:
    """流式增量里**不含** ![，含 alt 文本（站外地址一个字符都不露）。"""
    san = StreamingAnswerSanitizer(RefRegistry(PAPER))
    out = san.feed(f"下面这张是架构图：\n\n![Transformer 架构图]({REMOTE})\n\n看它有三路输入。")
    out += san.flush()
    assert "![" not in out, out
    assert "Transformer 架构图" in out
    assert "img.png" not in out and "arxiv.org" not in out


def test_streaming_replaces_an_alt_less_image_with_a_placeholder() -> None:
    """没写 alt 时用「图片」占位，而不是把 URL 亮出来。"""
    san = StreamingAnswerSanitizer(RefRegistry(PAPER))
    out = san.feed(f"![](https://cdn.example.com/x.png){'填充文本' * 40}") + san.flush()
    assert "![" not in out
    assert "图片" in out
    assert "cdn.example.com" not in out


def test_streaming_holds_half_an_image_until_it_completes() -> None:
    """半个片段（还没等到右括号）**不外推**：补齐之后推的是 alt，前缀只增不退。"""
    san = StreamingAnswerSanitizer(RefRegistry(PAPER))
    head = san.feed("背景说明。" * 40)          # 200 字：先把流推过攒批阈值
    assert "背景说明" in head
    half = "![架构图](https://cdn.example.com/" + "a" * 240
    streamed = san.feed(half)
    assert "![" not in streamed
    assert "a" * 240 not in streamed            # 半个片段一个字都没推出去
    tail = san.feed(".png)") + san.flush()
    assert "![" not in tail
    assert "架构图" in tail                     # 补齐后推的是 alt
    assert "a" * 240 not in tail


def test_streaming_keeps_an_unfinished_fragment_verbatim_at_flush() -> None:
    """一直没补齐的片段：**原样保留**（不替换、不吞字），只是晚一点推。"""
    san = StreamingAnswerSanitizer(RefRegistry(PAPER))
    half = "看图：![半张图](https://cdn.example.com/a"
    out = san.feed(half) + san.flush()
    assert out == half, out


def test_streaming_prefix_is_monotonic_when_fed_character_by_character() -> None:
    """逐字喂：流式增量拼起来必须**逐字等于**显示文本，且任何时刻都不出现 ![。

    这条是"前缀只增不退"的机器证明：先把半个片段推出去、补齐时再缩回，拼出来的串
    就会比最终文本多字或者乱序，这里的等式立刻失败。
    """
    san = StreamingAnswerSanitizer(RefRegistry(PAPER))
    text = f"先看图：\n\n![架构图]({REMOTE})\n\n图里三路输入。"
    streamed = ""
    for char in text:
        streamed += san.feed(char)
        assert "![" not in streamed, streamed        # 任何时刻都不闪原始图片 markdown
    streamed += san.flush()
    assert streamed == san.shown, (streamed, san.shown)
    assert streamed == "先看图：\n\n架构图\n\n图里三路输入。"


def test_streaming_plain_text_is_untouched() -> None:
    """没有图片的正文与改造前完全一致（这条是回归护栏）。"""
    san = StreamingAnswerSanitizer(RefRegistry(PAPER))
    text = "第 3 页讲了向量量化的误差上界，见公式 (7)。"
    assert san.feed(text) + san.flush() == text
