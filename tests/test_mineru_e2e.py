"""在线解析的**端到端**验证：假 MinerU 服务端 → 真 MySQL/真 IR → 阅读器载荷。

默认不跑（``@pytest.mark.online``，且需要 MySQL，不可用时自行 skip）：

```
pytest --run-online tests/test_mineru_e2e.py
```

为什么值得写：客户端与构建器各自的单测都过了，仍然可能"合起来不通"——
任务处理器、`_sync_to_mysql`、`reader_service`、区域层是四个不同的消费方，
只有真跑一遍才知道在线解析出来的论文**能不能被读**。这里把 MinerU 服务端换成
假传输层（不联网），其余全是真的。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pymupdf as fitz
import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = [pytest.mark.online, pytest.mark.e2e]


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()
    from backend.services.model import model_service

    model_service.ensure_builtin_models()
    return True


@pytest.fixture
def temp_pdf(tmp_path) -> Path:
    """两页合成 PDF（内容唯一 → sha256 唯一，避免与已导入的论文"复用"）。"""
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 100), f"Online Parse Demo p{i + 1} {uuid.uuid4().hex[:8]}",
                         fontsize=13)
        page.insert_text((72, 140), "Abstract: synthetic document for the online engine.",
                         fontsize=9)
    target = tmp_path / "online-demo.pdf"
    doc.save(str(target))
    doc.close()
    return target


def _content_list() -> list[dict]:
    return [
        {"type": "title", "text": "1 Introduction", "page_idx": 0,
         "bbox": [70, 60, 520, 90], "text_level": 1},
        {"type": "text", "text": "This paper studies online parsing.",
         "page_idx": 0, "bbox": [70, 100, 520, 140]},
        {"type": "equation", "text": "$$E = mc^2$$", "page_idx": 0,
         "bbox": [200, 160, 400, 200], "text_format": "block"},
        {"type": "image", "img_path": "images/abc.jpg", "page_idx": 0,
         "bbox": [100, 240, 380, 420], "image_caption": ["Figure 1. Online figure."]},
        {"type": "table", "table_body": "<table><tr><td>A</td><td>1</td></tr></table>",
         "table_caption": ["Table 1. Numbers."], "page_idx": 1,
         "bbox": [80, 120, 500, 220], "num_rows": 1, "num_cols": 2},
        {"type": "text", "text": "Second page body.", "page_idx": 1,
         "bbox": [70, 300, 520, 340]},
    ]


def test_online_parse_end_to_end_produces_a_readable_paper(db_ready, temp_pdf,
                                                           monkeypatch):
    from backend.parsing.online.mineru_client import MineruClient
    from backend.services.paper import figure_regions, paper_service, reader_service
    from backend.services.paper import mineru_service
    from backend.services.task.handlers import handle_parse
    from backend.services.task.task_service import TaskContext, task_service
    from tests.test_mineru_client import FakeTransport, mineru_zip

    # ---- 假 MinerU：申请上传 → 上传 → 轮询（一次 running 再一次 done）→ 下载 ----
    transport = FakeTransport()
    transport.poll_bodies = [
        {"code": 0, "data": {"extract_result": [
            {"file_name": "online-demo.pdf", "state": "running",
             "extract_progress": {"extracted_pages": 1, "total_pages": 2}}]}},
        {"code": 0, "data": {"extract_result": [
            {"file_name": "online-demo.pdf", "state": "done",
             "full_zip_url": "https://cdn.example.com/result.zip"}]}},
    ]
    transport.zip_bytes = mineru_zip(
        content_list=json.dumps(_content_list(), ensure_ascii=False),
        prefix="taskuuid")
    monkeypatch.setattr(mineru_service, "read_token", lambda: "tok-abcdefghij")
    monkeypatch.setattr(mineru_service, "client", lambda token=None: MineruClient(
        token, transport=transport, sleep_fn=lambda _s: None, clock_fn=lambda: 0.0))

    # ---- 导入（不自动解析，由我们手动跑任务）----
    name = f"{temp_pdf.stem}.pdf"
    paper = paper_service.import_pdf(
        temp_pdf, name, title=None, auto_parse=False, reuse_existing=False,
        parse_engine="mineru")
    paper_id = paper["id"]
    task = task_service.create("PARSE", "system", paper_id,
                               params={"engine": "mineru"}, total_items=9)
    ctx = TaskContext(task_id=task["id"], task_type="PARSE", queue="system",
                      paper_id=paper_id, params={"engine": "mineru"}, session=None)

    try:
        out = handle_parse(ctx)
        assert out["engine"] == "mineru"
        assert out["counts"]["blocks"] >= 5
        assert out["counts"]["figures"] == 1 and out["counts"]["tables"] == 1
        assert out["counts"]["formulas"] == 1
        assert out["mineru"]["bbox_space"].startswith("归一化")

        paths = paper_service.paper_paths(paper_id)
        assert paths["ir"].is_file() and paths["markdown"].is_file()
        assert (paths["root"] / "mineru").is_dir()      # 原始产物留档（供人工核对）

        # ---- 下游消费方 1：论文行被回写 ----
        row = paper_service.get_paper(paper_id)
        assert row["status"] == "parsed" and row["page_count"] == 2

        # ---- 下游消费方 2：阅读器载荷（三视图真正的入口）----
        payload = reader_service.build_reader(paper_id)
        assert payload["paper"]["id"] == paper_id
        kinds = {b["type"] for b in payload["blocks"]}
        assert {"heading", "para", "formula", "figure", "table", "caption"} <= kinds
        assert [p["page"] for p in payload["pages"]] == [1, 2]
        assert payload["outline"], "目录应当能从在线解析的标题构建出来"

        # ---- 下游消费方 3：插图区域（在线 bbox 原样采用）----
        figure_regions.invalidate_cache()
        regions = figure_regions.all_regions(paper_id, force=True)
        page1 = [r for r in regions.get(1, []) if r["kind"] == "figure"]
        assert len(page1) == 1 and page1[0]["source"] == "online"
        assert page1[0]["number"] == "1"
        media = figure_regions.media_of_page(paper_id, 1)
        assert media and media[0]["kind"] == "figure"
        # 裁图能真的出图（600dpi 从 PDF 裁）
        crop = figure_regions.ensure_region_crop(paper_id, 1, 0)
        assert crop.is_file() and crop.stat().st_size > 0
    finally:
        paper_service.delete_paper(paper_id, hard=True)


def test_reparse_online_keeps_block_ids(db_ready, temp_pdf, monkeypatch):
    """重解析（同一个结果包）必须沿用旧块 ID —— 译文/高亮挂在这些 ID 上。"""
    from backend.parsing.online.mineru_client import MineruClient
    from backend.services.paper import paper_service
    from backend.services.paper import mineru_service
    from backend.services.task.handlers import handle_parse
    from backend.services.task.task_service import TaskContext, task_service
    from tests.test_mineru_client import FakeTransport, mineru_zip

    import json

    def run() -> dict:
        transport = FakeTransport()
        transport.poll_bodies = [{"code": 0, "data": {"extract_result": [
            {"file_name": f"{temp_pdf.stem}.pdf", "state": "done",
             "full_zip_url": "https://cdn/result.zip"}]}}]
        transport.zip_bytes = mineru_zip(
            content_list=json.dumps(_content_list(), ensure_ascii=False),
            prefix="taskuuid")
        monkeypatch.setattr(mineru_service, "client",
                            lambda token=None: MineruClient(
                                token, transport=transport,
                                sleep_fn=lambda _s: None, clock_fn=lambda: 0.0))
        task = task_service.create("PARSE", "system", paper_id,
                                   params={"engine": "mineru"}, total_items=9)
        ctx = TaskContext(task_id=task["id"], task_type="PARSE", queue="system",
                          paper_id=paper_id, params={"engine": "mineru"}, session=None)
        return handle_parse(ctx)

    monkeypatch.setattr(mineru_service, "read_token", lambda: "tok-abcdefghij")
    paper = paper_service.import_pdf(temp_pdf, f"{temp_pdf.stem}.pdf", title=None,
                                     auto_parse=False, reuse_existing=False,
                                     parse_engine="mineru")
    paper_id = paper["id"]
    try:
        first = run()
        from backend.parsing.ir.ir_reader import IRReader

        paths = paper_service.paper_paths(paper_id)
        ids_before = sorted(b["id"] for b in IRReader(paths["ir"]).blocks())
        second = run()
        ids_after = sorted(b["id"] for b in IRReader(paths["ir"]).blocks())
        assert ids_before == ids_after
        assert first["counts"]["blocks"] == second["counts"]["blocks"]
    finally:
        paper_service.delete_paper(paper_id, hard=True)
