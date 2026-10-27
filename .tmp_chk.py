
import sys
sys.path.insert(0, ".")
import backend.services.translation.page_translate as pt
for phrase in ["唯一保留的横线", "arXiv 预印本标记", "分栏并排", "不要一列一行", "脚注横线"]:
    print(repr(phrase), "in A:", phrase in pt.TRANSCRIBE_SYSTEM, "| in B:", phrase in pt.TRANSLATE_SYSTEM, "| fused:", phrase in pt.SYSTEM_PROMPT)
i = pt.TRANSCRIBE_SYSTEM.find("页眉页脚")
print("...", pt.TRANSCRIBE_SYSTEM[i-20:i+260])
