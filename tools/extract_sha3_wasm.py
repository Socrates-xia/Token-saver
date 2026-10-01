"""从 openclaw-zero-token 源码里抽出内嵌的 DeepSeek SHA3 WASM 模块。

该模块是 DeepSeek 官方前端加载的那份（导出 wasm_solve /
wasm_deepseek_hash_v1），拿它当 PoW 求解器可以完全绕开浏览器。

用法：
    python tools/extract_sha3_wasm.py <deepseek-web-client.ts 路径> [输出路径]
"""
from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import settings  # noqa: E402


def extract(src_path: Path) -> bytes:
    src = src_path.read_text(encoding="utf-8")
    m = re.search(r'SHA3_WASM_B64\s*=\s*"([A-Za-z0-9+/=]+)"', src)
    if not m:
        raise SystemExit("没找到 SHA3_WASM_B64 常量")
    return base64.b64decode(m.group(1))


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else (settings.ASSETS_DIR / "sha3_wasm.wasm")
    raw = extract(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    print(f"wasm 大小 {len(raw)} 字节（magic={raw[:4]!r}）→ {out}")
    for sym in (b"wasm_solve", b"wasm_deepseek_hash_v1",
                b"__wbindgen_add_to_stack_pointer", b"__wbindgen_export_0"):
        print(f"  含 {sym.decode()}: {sym in raw}")


if __name__ == "__main__":
    main()
