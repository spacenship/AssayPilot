"""Compare two generated public/ trees by relative path and SHA-256."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("reproduced", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    before = hashes(args.baseline)
    after = hashes(args.reproduced)
    result = {
        "baseline": str(args.baseline),
        "reproduced": str(args.reproduced),
        "identical": before == after,
        "baseline_files": before,
        "reproduced_files": after,
        "missing_in_reproduced": sorted(set(before) - set(after)),
        "extra_in_reproduced": sorted(set(after) - set(before)),
        "changed": sorted(key for key in set(before) & set(after) if before[key] != after[key]),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
    return 0 if result["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
