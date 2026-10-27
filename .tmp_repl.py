
import io, re
p = "tests/test_page_translate.py"
src = io.open(p, encoding="utf-8").read()
start = src.find("def test_block_mode_ignores_stale_a_page_when_retranslating")
assert start > 0
# 找到该测试之后的下一个顶层 def（本文件里它现在是最后一个）
rest = src[start:]
m = re.search(r"\n\n\ndef ", rest)
end = start + (m.start() if m else len(rest))
io.open(p, "w", encoding="utf-8", newline="\n").write(src[:start] + io.open('.tmp_newtest.txt', encoding='utf-8').read().strip('
') + src[end:])
print("replaced", end - start, "chars")
