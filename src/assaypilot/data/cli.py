"""fetch/build/inspect의 얇은 argparse 진입점."""
from __future__ import annotations

import argparse
from pathlib import Path

from assaypilot.domain import DataSource

from .adapter import PublicBundleAdapter
from .audit import PublicCampaignAuditor
from .build import BuildError, build_campaign, load_config
from .pubchem import fetch_campaign


def main(argv: list[str] | None = None) -> int:
    """명령별 데이터 계층 함수를 호출하고 공개 inspect는 public만 읽는다."""
    parser = argparse.ArgumentParser(prog="assaypilot-data")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("config", type=Path); fetch.add_argument("cache_dir", type=Path)
    fetch.add_argument("--refresh", action="store_true")
    build = commands.add_parser("build")
    build.add_argument("config", type=Path); build.add_argument("cache_dir", type=Path); build.add_argument("output_dir", type=Path)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    if args.command == "fetch":
        responses = fetch_campaign(load_config(args.config), args.cache_dir, refresh=args.refresh)
        for response in responses:
            print(f"{'reused' if response.reused else 'fetched'} {response.path} {response.sha256}")
        return 0
    if args.command == "build":
        try:
            report = build_campaign(load_config(args.config), args.cache_dir, args.output_dir)
        except BuildError as exc:
            print(exc.report.model_dump_json(indent=2))
            return 2
        print(report.model_dump_json(indent=2))
        return 0
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(args.manifest)))
    auditor = PublicCampaignAuditor()
    audit = auditor.audit(campaign)
    print(f"campaign={campaign.campaign.campaign_id} candidates={len(campaign.candidates)} "
          f"assays={','.join(assay.assay_id for assay in campaign.assays)} observations={len(campaign.observations)} "
          f"audit_ok={audit.ok}")
    summary = auditor.chemical_summary
    if summary is not None:
        print(f"chemical_audit_backend={summary.backend} candidates={summary.candidates} "
              f"structures_present={summary.structures_present} checked={summary.checked} "
              f"passed={summary.passed} failed={summary.failed}")
    for issue in audit.issues:
        print(f"{issue.code} {issue.target_id} {issue.field}: {issue.reason}")
    return 0 if audit.ok else 2
