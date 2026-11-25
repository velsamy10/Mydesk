import re
from pathlib import Path

root = Path(__file__).resolve().parent
html = (root / "public" / "index.html").read_text(encoding="utf-8")
py_path = root / "nifty_monitor.py"
py = py_path.read_text(encoding="utf-8")
m = re.search(r'HTML = r""".*?"""\r?\n\r?\n\r?\nclass LocalMonitorHandler', py, re.S)
if not m:
    m = re.search(r'HTML = r""".*?"""\r?\n\r?\nclass LocalMonitorHandler', py, re.S)
if not m:
    raise SystemExit("HTML block not found")
out = py[: m.start()] + 'HTML = r"""' + html + '"""\n\n\nclass LocalMonitorHandler' + py[m.end() :]
py_path.write_text(out, encoding="utf-8")
print("synced", len(html))
