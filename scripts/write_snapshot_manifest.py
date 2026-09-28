"""Write developer-only provenance and SHA-256 inventory for a Stage 1 snapshot."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(args: list[str], repo_root: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout.strip()


def changed_paths(repo_root: Path) -> list[Path]:
    """HEAD 대비 변경·추가된 경로를 찾아 source artifact 목록을 만든다."""
    diff_paths = git(["diff", "--name-only", "HEAD"], repo_root).splitlines()
    status_lines = git(["status", "--porcelain=v1", "--untracked-files=all"], repo_root).splitlines()
    paths = set(diff_paths)
    for line in status_lines:
        if len(line) >= 4:
            path = line[3:]
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[-1]
            paths.add(path)
    return [Path(path) for path in paths if path]


def preserve_implementation(root: Path, repo_root: Path, diff: bytes) -> dict[str, object]:
    """실행에 사용한 patch와 변경 source/config를 snapshot 안에 복사한다."""
    implementation = root / "implementation"
    implementation.mkdir(parents=True, exist_ok=True)
    patch_path = implementation / "working_tree.patch"
    patch_path.write_bytes(diff)
    source_roots = {"src", "scripts", "examples", "tests"}
    copied: list[dict[str, str]] = []
    for relative in sorted(changed_paths(repo_root)):
        if not relative.parts or relative.parts[0] not in source_roots:
            continue
        source = repo_root / relative
        if not source.is_file():
            continue
        target = implementation / "changed_files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append({
            "path": str(relative),
            "artifact": str(target.relative_to(root)),
            "sha256": digest(source),
        })
    restore = (
        "From the AssayPilot repository root, apply implementation/working_tree.patch "
        "against the recorded git commit, then copy each implementation/changed_files "
        "entry to its repo-relative path. The patch contains tracked changes; the copied "
        "files contain untracked/new source and config files.\n"
    )
    (implementation / "RESTORE.txt").write_text(restore)
    return {
        "patch": str(patch_path.relative_to(root)),
        "patch_sha256": digest(patch_path),
        "changed_files": copied,
        "restore_instructions": str((implementation / "RESTORE.txt").relative_to(root)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    repo_root = args.repo_root.resolve()
    config_data = json.loads(args.config.read_text())
    files = {
        str(path.relative_to(root)): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "snapshot_manifest.json"
    }
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=repo_root, check=True, capture_output=True
    ).stdout
    dependencies = {}
    for name in ("pydantic", "httpx", "rdkit"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = "unavailable"
    manifest = {
        "snapshot_id": root.name,
        "campaign_id": config_data["campaign_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline_snapshot": str(args.baseline),
        "implementation": {
            "git_commit": git(["rev-parse", "HEAD"], repo_root),
            "git_status_short": git(["status", "--short"], repo_root),
            "git_diff_sha256": hashlib.sha256(diff).hexdigest(),
            "python": sys.version,
            "platform": platform.platform(),
            "dependencies": dependencies,
        },
        "official_relation_source": {
            "request_path": "assay/aid/504468/description/JSON",
            "source_id": "PubChem AID:504468",
            "raw_file": "raw/aid504468_description.json",
            "role": "developer-only relation evidence; not an executed assay",
        },
        "full_sha256": files,
    }
    manifest["implementation_artifacts"] = preserve_implementation(root, repo_root, diff)
    manifest["full_sha256"] = {
        str(path.relative_to(root)): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "snapshot_manifest.json"
    }
    (root / "snapshot_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
