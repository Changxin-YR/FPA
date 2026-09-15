"""算出四个内核文件的哈希，与 复核人 记录的冻结值比对。

用途：负责人 要求我交出"我改了哪几处"，复核人 用冻结哈希判定有没有静默丢失。
本脚本给出**当前**哈希；对不上说明文件在冻结之后被改过（t18/t20 本来就会合法地改
`invariants.py`），所以哈希只能证明"变没变"，不能证明"谁改的"——那需要 diff。

输出三种哈希，因为冻结值用的是哪种编码未知：
  · sha256 原始字节（前 12 位大写）
  · sha256 归一化换行后（CRLF -> LF）
  · sha256 归一化换行 + 去 BOM
"""

from __future__ import annotations

import hashlib
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "backend" / "fpa" / "kernel"

#: 复核人 记录的冻结哈希（负责人 转述）
RECORDED = {
    "capability.py": "6A40C72781FE",
    "invariants.py": "A2065A4E8467",
    "runner.py": "B2314FFBAE4B",
    "workflow.py": "975E4FABAEFB",
}


def digests(data: bytes) -> dict[str, str]:
    normalized = data.replace(b"\r\n", b"\n")
    stripped = normalized.lstrip(b"\xef\xbb\xbf")
    return {
        "raw": hashlib.sha256(data).hexdigest(),
        "lf": hashlib.sha256(normalized).hexdigest(),
        "lf+bom": hashlib.sha256(stripped).hexdigest(),
    }


def main() -> int:
    print(f"{'文件':<16} {'记录值':<14} {'raw':<14} {'lf':<14} {'lf+bom':<14} 判定")
    print("-" * 92)
    for name, recorded in RECORDED.items():
        path = BACKEND / name
        if not path.exists():
            print(f"{name:<16} 文件不存在")
            continue
        variants = digests(path.read_bytes())
        cells = {key: value[:12].upper() for key, value in variants.items()}
        verdict = "MATCH" if recorded in cells.values() else "changed / 编码不一致"
        print(f"{name:<16} {recorded:<14} {cells['raw']:<14} {cells['lf']:<14} "
              f"{cells['lf+bom']:<14} {verdict}")
    print()
    print("注意：哈希只能证明「变没变」，不能证明「谁改的」。")
    print("      t18/t20 会合法地改 invariants.py，所以它「changed」是预期的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
