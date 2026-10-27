
import sys
sys.path.insert(0, ".")
import backend.services.translation.page_translate as pt
for name in ("SYSTEM_PROMPT", "TRANSCRIBE_SYSTEM", "TRANSLATE_SYSTEM", "BLOCK_TRANSLATE_SYSTEM"):
    print(name, len(getattr(pt, name)))
print("edge A len", len(pt.render_edge_facts({"head": True, "tail": True}, path="a")))
