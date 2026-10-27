
import subprocess, sys, importlib.util, tempfile, os, json
# 取 HEAD 版本与工作区版本的 prompt 长度对比
src = subprocess.run(["git", "show", "HEAD:backend/services/translation/page_translate.py"], capture_output=True, text=True, encoding="utf-8").stdout
tmp = os.path.join(tempfile.gettempdir(), "pt_head.py")
open(tmp, "w", encoding="utf-8").write(src)
sys.path.insert(0, ".")
import backend.services.translation.page_translate as cur
spec = importlib.util.spec_from_file_location("pt_head", tmp)
head = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(head)
except Exception as e:
    print("head import failed:", e)
for name in ("SYSTEM_PROMPT", "TRANSCRIBE_SYSTEM", "TRANSLATE_SYSTEM", "BLOCK_TRANSLATE_SYSTEM"):
    a = len(getattr(head, name)); b = len(getattr(cur, name))
    print(f"{name}: {a} -> {b} ({(b-a)/a*100:+.1f}%)")
print("total:", sum(len(getattr(head, n)) for n in ("SYSTEM_PROMPT","TRANSCRIBE_SYSTEM","TRANSLATE_SYSTEM","BLOCK_TRANSLATE_SYSTEM")),
      "->", sum(len(getattr(cur, n)) for n in ("SYSTEM_PROMPT","TRANSCRIBE_SYSTEM","TRANSLATE_SYSTEM","BLOCK_TRANSLATE_SYSTEM")))
