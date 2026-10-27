
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import load_path_stores, merged_page
from backend.services.translation.page_translate import split_blocks
pid = "attention-cleanroom"
a, b = load_path_stores(pid)
pa, pb = a.all_pages(), b.all_pages()
bad = []
tot_rows = 0
for pno in sorted(pa, key=lambda x: int(x)):
    am = str(pa[pno].get("original_md") or "")
    bm = str(pb.get(pno, {}).get("translation_md") or "")
    rows = (merged_page(pid, pno) or {}).get("rows") or []
    tot_rows += len(rows)
    na, nb = len(split_blocks(am)), len(split_blocks(bm))
    empty_zh = sum(1 for r in rows if str(r.get("original") or "").strip() and not str(r.get("translation") or "").strip())
    flags = []
    if na != nb: flags.append(f"块数 {na}/{nb}")
    if empty_zh: flags.append(f"空译{empty_zh}")
    if "<br>" in am: flags.append("含<br>")
    if flags: bad.append((pno, flags))
print("pages", len(pa), "/", len(pb), "| total rows", tot_rows)
print("problems:", bad if bad else "none")
m1 = merged_page(pid, 1) or {}
rows = m1.get("rows") or []
print("p1 rows", len(rows), "| has standalone ---:", [r.get("id") for r in rows if str(r.get("original")).strip() == "---"])
print("p1 footnote-ish rows:", [r.get("id") for r in rows if str(r.get("original")).lstrip().startswith(("--", "*", chr(8224), chr(8225), "31st"))])
