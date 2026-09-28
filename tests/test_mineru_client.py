"""MinerU 客户端：提交→轮询→下载→安全解包（**全程离线**，用可编程假传输层）。

## 为什么这些用例默认不跑

用户口径（2026-09-28）：在线那套单独跑，别和主测试混在一起 —— 所以本文件整体打了
``@pytest.mark.online``，默认被 ``tests/conftest.py`` 跳过。要跑：

```
pytest --run-online tests/test_mineru_client.py
```

标了 ``@pytest.mark.mineru_live`` 的那条需要**真实 token**（放
``data/secrets/mineru.key`` 或 `PR_MINERU_TOKEN`），没 token 时自行 skip。

## 假传输层为什么值得写

在线解析的失败模式几乎全在"协议细节"上：鉴权头写错、PUT 多带了 Content-Type、
把网关的 ``success:false`` 当成业务 ``code``、结果包文件名带 uuid 前缀……
这些都能用几十行的假传输层钉死，而真连网验证（见文末 live 用例）只用来确认
"我们理解的协议和服务端一致"。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from backend.parsing.online.mineru_client import (
    MineruAuthError, MineruCancelled, MineruClient, MineruError, MineruQuotaError,
    MineruTimeoutError, hint_for, locate_bundle, mask_token, safe_extract_zip,
)

pytestmark = pytest.mark.online

APPLY_URL = "https://mineru.net/api/v4/file-urls/batch"
PUT_URL = "https://oss.example.com/put/abc"
POLL_URL_PREFIX = "https://mineru.net/api/v4/extract-results/batch/"


# --------------------------------------------------------------------------
# 假传输层 + 样例结果包
# --------------------------------------------------------------------------


class FakeTransport:
    """把 4 个动作都记下来，并按脚本返回（测试里直接改属性即可）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.apply_status = 200
        self.apply_body: dict = {
            "code": 0, "msg": "ok",
            "data": {"batch_id": "B-1", "file_urls": [PUT_URL]},
        }
        self.put_status = 200
        self.put_result: dict = {}
        # 轮询依次返回；用完后重复最后一个（"一直是 running" 就是只给一项）
        self.poll_bodies: list[dict] = []
        self.get_bodies: dict[str, dict] = {}
        self.get_status: dict[str, int] = {}
        # `/extract/task/{id}` 探测的默认返回：业务码 -60012 = 任务不存在（= 鉴权通过）
        self.probe_body: dict = {"code": -60012, "msg": "task not found"}
        self.zip_bytes = b""
        self.raise_exc: dict[str, Exception] = {}
        self.downloaded: list[str] = []

    # ---- 断言辅助 ----
    def calls_of(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]

    # ---- 协议 ----
    def post_json(self, url, body, headers, timeout):
        self.calls.append(("post", url, body, headers))
        if "post" in self.raise_exc:
            raise self.raise_exc["post"]
        return self.apply_status, self.apply_body

    def get_json(self, url, headers, timeout):
        self.calls.append(("get", url, headers))
        if "get" in self.raise_exc:
            raise self.raise_exc["get"]
        if url in self.get_bodies:
            return self.get_status.get(url, 200), self.get_bodies[url]
        if url.startswith(POLL_URL_PREFIX):
            if not self.poll_bodies:
                return 200, {"code": 0, "data": {"extract_result": []}}
            body = self.poll_bodies[0] if len(self.poll_bodies) == 1 \
                else self.poll_bodies.pop(0)
            return 200, body
        if "/extract/task/" in url:
            return 200, self.probe_body
        return 200, {}

    def put_file(self, url, path, headers, timeout):
        self.calls.append(("put", url, path, headers))
        self.put_result = {"path": Path(path).read_bytes(), "headers": headers}
        if "put" in self.raise_exc:
            raise self.raise_exc["put"]
        return self.put_status

    def download(self, url, dest, timeout):
        self.calls.append(("download", url, str(dest)))
        self.downloaded.append(url)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.zip_bytes)
        return len(self.zip_bytes)


def make_zip(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def mineru_zip(*, content_list: str = "[]", prefix: str = "u1",
               nested: str = "") -> bytes:
    """一个贴近真实的结果包：文件名带任务 uuid 前缀、图片在 ``images/`` 下。"""
    head = f"{nested}/" if nested else ""
    return make_zip({
        f"{head}{prefix}_content_list.json": content_list,
        f"{head}{prefix}_middle.json": '{"pdf_info": []}',
        f"{head}full.md": "# demo\n",
        f"{head}images/abc.jpg": b"\xff\xd8\xff\xe0jpeg",
    })


def client(transport: FakeTransport, **kw) -> MineruClient:
    kw.setdefault("sleep_fn", lambda _s: None)
    kw.setdefault("clock_fn", lambda: 0.0)
    return MineruClient("tok-1234567890", transport=transport, **kw)


def write_pdf(path: Path, payload: bytes = b"%PDF-1.4 demo") -> Path:
    path.write_bytes(payload)
    return path


def done_body(state: str = "done", **extra) -> dict:
    item = {"file_name": "paper.pdf", "state": state}
    item.update(extra)
    return {"code": 0, "msg": "ok", "data": {"batch_id": "B-1",
                                             "extract_result": [item]}}


# --------------------------------------------------------------------------
# 正常流程
# --------------------------------------------------------------------------


def test_full_flow_uploads_polls_downloads_and_locates_bundle(tmp_path):
    t = FakeTransport()
    t.poll_bodies = [
        done_body("running", extract_progress={"extracted_pages": 3, "total_pages": 22}),
        done_body("done", full_zip_url="https://cdn.example.com/r.zip"),
    ]
    t.zip_bytes = mineru_zip(content_list='[{"type":"text","text":"hi","page_idx":0}]',
                             nested="paper")
    pdf = write_pdf(tmp_path / "paper.pdf")
    stages: list[tuple[str, int, int]] = []

    bundle = client(t).parse_pdf(
        pdf, tmp_path / "raw", name="paper.pdf", data_id="p1",
        language="en", on_progress=lambda s, d, n: stages.append((s, d, n)))

    # 1) 申请上传链接：Bearer 鉴权 + 把可选参数放在 files[] 里（官方批量接口口径）
    post = t.calls_of("post")[0]
    assert post[1] == APPLY_URL
    assert post[3]["Authorization"] == "Bearer tok-1234567890"
    assert post[3]["Content-Type"] == "application/json"
    assert post[2]["files"] == [{"name": "paper.pdf", "data_id": "p1"}]
    assert post[2]["model_version"] == "pipeline" and post[2]["language"] == "en"

    # 2) PUT 原始字节：官方文档明确「无须设置 Content-Type」
    put = t.calls_of("put")[0]
    assert put[1] == PUT_URL and put[3]["Content-Length"] == str(pdf.stat().st_size)
    assert "Content-Type" not in put[3]
    assert t.put_result["path"] == pdf.read_bytes()

    # 3) 轮询与下载
    assert t.calls_of("get")[0][1] == f"{POLL_URL_PREFIX}B-1"
    assert t.downloaded == ["https://cdn.example.com/r.zip"]

    # 4) 解包 + 定位（文件名带 uuid 前缀、还嵌套了一层目录）
    assert bundle["batch_id"] == "B-1"
    assert bundle["content_list"].name == "u1_content_list.json"
    assert bundle["full_md"].name == "full.md"
    assert bundle["images_dir"].name == "images"
    assert bundle["zip_bytes"] == len(t.zip_bytes)

    # 5) 进度：提交/上传/轮询/下载四段都报过，且轮询带真实页数
    assert [s for s, _d, _n in stages][:4] == [
        "mineru_apply", "mineru_upload", "mineru_poll", "mineru_download"]
    assert ("mineru_poll", 3, 22) in stages


def test_empty_token_raises_auth_error(tmp_path):
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruAuthError) as err:
        MineruClient("", transport=FakeTransport()).parse_pdf(pdf, tmp_path / "raw")
    assert "设置" in str(err.value)


def test_upload_link_without_batch_id_is_reported(tmp_path):
    t = FakeTransport()
    t.apply_body = {"code": 0, "msg": "ok", "data": {}}
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruError) as err:
        client(t).parse_pdf(pdf, tmp_path / "raw")
    assert "batch_id" in str(err.value)


def test_non_200_upload_is_reported(tmp_path):
    t = FakeTransport()
    t.put_status = 403
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruError) as err:
        client(t).parse_pdf(pdf, tmp_path / "raw")
    assert "上传" in str(err.value) and "403" in str(err.value)


# --------------------------------------------------------------------------
# 失败形态：三套响应都要认
# --------------------------------------------------------------------------


def test_gateway_auth_failure_is_not_mistaken_for_success(tmp_path):
    """网关失败是 ``{"success":false,"msgCode":"A0202"}``，**没有** ``code`` 字段。

    只看 ``code`` 的实现会把它当成功（code 缺省 → 通过），继续走后面的流程并报出
    一个更难懂的错误。这条把"必须先看网关那一套"钉住。
    """
    t = FakeTransport()
    t.apply_status = 200
    t.apply_body = {"success": False, "msgCode": "A0202", "msg": "user authenticate failed"}
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruAuthError) as err:
        client(t).parse_pdf(pdf, tmp_path / "raw")
    assert "A0202" in str(err.value)


def test_gateway_token_expired_maps_to_auth_error(tmp_path):
    t = FakeTransport()
    t.apply_body = {"success": False, "msgCode": "A0211", "msg": "token expired"}
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruAuthError) as err:
        client(t).parse_pdf(pdf, tmp_path / "raw")
    assert "过期" in str(err.value) and "90" in str(err.value)


@pytest.mark.parametrize("status,code,needle", [
    (401, None, "token"),
    (403, None, "token"),
    (404, None, "接口地址"),
    (200, -60006, "页数"),
    (200, -60005, "200MB"),
])
def test_error_codes_become_actionable_chinese(tmp_path, status, code, needle):
    t = FakeTransport()
    t.apply_status = status
    t.apply_body = ({"code": code, "msg": "server says no"} if code is not None
                    else {"msg": "denied"})
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruError) as err:
        client(t).parse_pdf(pdf, tmp_path / "raw")
    assert needle in str(err.value)


def test_quota_code_has_its_own_exception(tmp_path):
    t = FakeTransport()
    t.apply_body = {"code": -60018, "msg": "quota"}
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruQuotaError):
        client(t).parse_pdf(pdf, tmp_path / "raw")


def test_failed_state_carries_the_server_reason(tmp_path):
    t = FakeTransport()
    t.poll_bodies = [done_body("failed", err_msg="Unsupported file format")]
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruError) as err:
        client(t).parse_pdf(pdf, tmp_path / "raw")
    assert "Unsupported file format" in str(err.value)


def test_missing_zip_url_is_reported(tmp_path):
    t = FakeTransport()
    t.poll_bodies = [done_body("done")]
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruError) as err:
        client(t).parse_pdf(pdf, tmp_path / "raw")
    assert "结果包" in str(err.value)


def test_bundle_without_content_list_is_reported(tmp_path):
    t = FakeTransport()
    t.poll_bodies = [done_body("done", full_zip_url="https://cdn/r.zip")]
    t.zip_bytes = make_zip({"full.md": "# only markdown\n"})
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruError) as err:
        client(t).parse_pdf(pdf, tmp_path / "raw")
    assert "content_list" in str(err.value)


# --------------------------------------------------------------------------
# 等待：超时 / 取消
# --------------------------------------------------------------------------


def test_poll_timeout_uses_a_fake_clock(tmp_path):
    t = FakeTransport()
    t.poll_bodies = [done_body("running", extract_progress={"extracted_pages": 1,
                                                            "total_pages": 9})]
    ticks = {"n": 0.0}

    def clock() -> float:
        ticks["n"] += 0.5
        return ticks["n"]

    pdf = write_pdf(tmp_path / "p.pdf")
    cli = MineruClient("tok", transport=t, sleep_fn=lambda _s: None,
                       clock_fn=clock, poll_deadline=5.0)
    with pytest.raises(MineruTimeoutError) as err:
        cli.parse_pdf(pdf, tmp_path / "raw")
    assert "5" in str(err.value) or "分钟" in str(err.value)


def test_cancel_stops_waiting_without_touching_the_server(tmp_path):
    t = FakeTransport()
    t.poll_bodies = [done_body("running")]
    pdf = write_pdf(tmp_path / "p.pdf")
    with pytest.raises(MineruCancelled) as err:
        client(t).parse_pdf(pdf, tmp_path / "raw", should_stop=lambda: True)
    # 文案必须说清"服务端任务不会因此撤销"，否则用户以为点了取消就不占额度了
    assert "撤销" in str(err.value) or "不会" in str(err.value)


def test_poll_picks_the_result_matching_our_file(tmp_path):
    t = FakeTransport()
    t.poll_bodies = [{
        "code": 0, "data": {"extract_result": [
            {"file_name": "other.pdf", "state": "failed", "err_msg": "boom"},
            {"file_name": "paper.pdf", "state": "done",
             "full_zip_url": "https://cdn/r.zip"},
        ]}}]
    t.zip_bytes = mineru_zip()
    pdf = write_pdf(tmp_path / "p.pdf")
    bundle = client(t).parse_pdf(pdf, tmp_path / "raw", name="paper.pdf")
    assert bundle["batch_id"] == "B-1"


# --------------------------------------------------------------------------
# verify：怎么判断 token 有效
# --------------------------------------------------------------------------


def test_verify_ok_when_business_code_comes_back():
    """拿到业务码（这里是 -60012 任务不存在）就说明穿过了鉴权层。"""
    t = FakeTransport()
    cli = MineruClient("tok-abcdefghij", transport=t)
    res = cli.verify()
    assert res["ok"] is True and res["state"] == "valid"
    assert res["token_masked"] == "tok-…ghij"
    # 探测用的是解析任务查询接口（不创建任务、不消耗配额）
    assert "/extract/task/" in t.calls_of("get")[0][1]


def test_verify_reports_expired_token():
    class T(FakeTransport):
        def get_json(self, url, headers, timeout):
            self.calls.append(("get", url, headers))
            return 200, {"success": False, "msgCode": "A0211", "msg": "expired"}

    res = MineruClient("tok-abcdefghij", transport=T()).verify()
    assert res["ok"] is False and res["state"] == "expired"
    assert "90" in res["message"]


def test_verify_reports_http_401():
    class T(FakeTransport):
        def get_json(self, url, headers, timeout):
            return 401, {"msg": "unauthorized"}

    res = MineruClient("tok-abcdefghij", transport=T()).verify()
    assert res["ok"] is False and res["state"] == "invalid"
    assert "token" in res["message"]


def test_verify_reports_network_failure():
    t = FakeTransport()
    t.raise_exc = {"get": RuntimeError("connection refused")}
    res = MineruClient("tok-abcdefghij", transport=t).verify()
    assert res["ok"] is False and res["state"] == "network"
    assert "connection refused" in res["message"]


def test_verify_without_token_says_where_to_put_it():
    res = MineruClient(None, transport=FakeTransport()).verify()
    assert res["ok"] is False and res["state"] == "missing"
    assert "设置" in res["message"]


def test_verify_rejects_a_non_json_200():
    """HTTP 200 但不是 JSON（走错地址拿到门户 HTML）不能报成"有效"。"""
    class T(FakeTransport):
        def get_json(self, url, headers, timeout):
            return 200, {}

    res = MineruClient("tok-abcdefghij", transport=T()).verify()
    assert res["ok"] is False and res["state"] == "error"


# --------------------------------------------------------------------------
# 安全解包 / 产物定位 / 掩码
# --------------------------------------------------------------------------


def test_safe_extract_refuses_path_traversal(tmp_path):
    zip_path = tmp_path / "bad.zip"
    zip_path.write_bytes(make_zip({"../escape.txt": "x"}))
    with pytest.raises(MineruError) as err:
        safe_extract_zip(zip_path, tmp_path / "out")
    assert "不安全" in str(err.value) or "越界" in str(err.value)


def test_safe_extract_refuses_absolute_path(tmp_path):
    zip_path = tmp_path / "abs.zip"
    zip_path.write_bytes(make_zip({"/tmp/evil.txt": "x"}))
    with pytest.raises(MineruError):
        safe_extract_zip(zip_path, tmp_path / "out")


def test_safe_extract_refuses_symlink_entries(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)        # S_IFLNK
        zf.writestr(info, "/etc/passwd")
    zip_path = tmp_path / "link.zip"
    zip_path.write_bytes(buf.getvalue())
    with pytest.raises(MineruError) as err:
        safe_extract_zip(zip_path, tmp_path / "out")
    assert "符号链接" in str(err.value)


def test_safe_extract_caps_entry_count_and_total_size(tmp_path):
    big = tmp_path / "big.zip"
    big.write_bytes(make_zip({f"f{i}.bin": b"x" * 10 for i in range(6)}))
    with pytest.raises(MineruError) as err:
        safe_extract_zip(big, tmp_path / "o1", max_entries=3)
    assert "条目过多" in str(err.value)
    with pytest.raises(MineruError) as err2:
        safe_extract_zip(big, tmp_path / "o2", max_total_bytes=20)
    assert "解压后" in str(err2.value)


def test_safe_extract_writes_normal_entries(tmp_path):
    zip_path = tmp_path / "ok.zip"
    zip_path.write_bytes(mineru_zip(nested="paper"))
    n = safe_extract_zip(zip_path, tmp_path / "out")
    assert n == 4
    assert (tmp_path / "out" / "paper" / "images" / "abc.jpg").is_file()


def test_locate_bundle_prefers_v1_over_v2_and_shallow_copy(tmp_path):
    root = tmp_path / "raw"
    (root / "outer").mkdir(parents=True)
    (root / "u1_content_list.json").write_text("[]", encoding="utf-8")
    (root / "u1_content_list_v2.json").write_text("[]", encoding="utf-8")
    (root / "outer" / "u2_content_list.json").write_text("[]", encoding="utf-8")
    (root / "full.md").write_text("# x", encoding="utf-8")
    (root / "images").mkdir()
    got = locate_bundle(root)
    assert got["content_list"].name == "u1_content_list.json"
    assert got["full_md"].name == "full.md"
    assert got["images_dir"] == root / "images"


def test_locate_bundle_falls_back_to_v2(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    (root / "u1_content_list_v2.json").write_text("[]", encoding="utf-8")
    assert locate_bundle(root)["content_list"].name == "u1_content_list_v2.json"


def test_mask_token_never_leaks_the_middle():
    assert mask_token("abcdefghijklmnop") == "abcd…mnop"
    assert mask_token("short") == "sh…rt"
    assert mask_token(None) is None
    assert "cdefghijkl" not in mask_token("abcdefghijklmnop")


def test_hint_for_unknown_code_keeps_server_text():
    text = hint_for("-99999", "whatever")
    assert "-99999" in text and "whatever" in text


# --------------------------------------------------------------------------
# 真连网冒烟（需要 token；`pytest --run-online -m mineru_live`）
# --------------------------------------------------------------------------


@pytest.mark.mineru_live
def test_live_token_is_accepted():
    """拿真实 token 验一次：只做鉴权探测，**不创建解析任务、不消耗配额**。

    没配 token 时 skip：这条存在的意义是"我们理解的鉴权协议和服务端一致"，
    而不是让 CI 依赖外网。
    """
    from backend.core.config import get_settings

    token = get_settings().mineru_token
    if not token:
        pytest.skip("未配置 MinerU token（data/secrets/mineru.key 或 PR_MINERU_TOKEN）")
    res = MineruClient(token).verify()
    assert res["ok"] is True, res
    assert res["latency_ms"] > 0
