import re
from pathlib import Path

root = Path(__file__).resolve().parent
text = (root / "nifty_monitor.py").read_text(encoding="utf-8")
m = re.search(r'HTML = r"""(.*?)"""\n\nclass LocalMonitorHandler', text, re.S)
if not m:
    m = re.search(r'HTML = r"""(.*?)"""\n\nclass Handler', text, re.S)
if not m:
    m = re.search(r'HTML = r"""(.*?)"""\n\n\nclass (?:LocalMonitor)?Handler', text, re.S)
if not m:
    raise SystemExit("HTML block not found")
out = root / "public" / "index.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(m.group(1), encoding="utf-8")
print("wrote", out, "chars", len(m.group(1)))
