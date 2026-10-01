"""Private process entry point for one persistent Stage 4 run."""
from __future__ import annotations

import argparse
from pathlib import Path

from assaypilot.stage4_service import run_worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assaypilot-stage4-worker")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    return run_worker(args.runtime_root, args.run_id, resume=args.resume)


if __name__ == "__main__":
    raise SystemExit(main())
