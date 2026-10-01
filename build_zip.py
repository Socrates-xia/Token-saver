"""把技能包打成可分发的 zip。

产物：dist/token-saver-<版本>.zip，条目全部挂在 token-saver/ 目录下。
刻意排除 __pycache__ / *.pyc —— 包内所有 Python 入口都带 -B，
分发物里不该出现字节码。
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = Path.home() / ".workbuddy" / "skills" / "token-saver"
OUT = ROOT / "dist"

ver = re.search(r"^version:\s*(\S+)\s*$",
                (SRC / "SKILL.md").read_text(encoding="utf-8").split("---")[1],
                re.M).group(1)
OUT.mkdir(exist_ok=True)
dest = OUT / f"token-saver-{ver}.zip"

SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIX = {".pyc", ".pyo", ".zip"}
SKIP_NAMES = {".DS_Store"}

n = 0
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for p in sorted(SRC.rglob("*")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX or p.name in SKIP_NAMES:
            continue
        if p.is_dir():
            continue
        z.write(p, f"token-saver/{p.relative_to(SRC).as_posix()}")
        n += 1

data = dest.read_bytes()
deep = max(len(Path(i).parts) for i in zipfile.ZipFile(dest).namelist())
print(f"产物 : {dest}")
print(f"条目 : {n} 个文件 / {deep} 层深")
print(f"体积 : {len(data) / 1024:.1f} KB")
print(f"SHA256: {hashlib.sha256(data).hexdigest()}")
