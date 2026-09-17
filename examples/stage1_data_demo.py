"""네트워크 없이 fixture cache에서 1단계 public bundle을 만든다."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.data.audit import PublicCampaignAuditor
from assaypilot.data.build import build_campaign, load_config
from assaypilot.domain import DataSource


ROOT = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="assaypilot-stage1-") as temporary:
    workspace = Path(temporary)
    cache = workspace / "cache"
    shutil.copytree(ROOT / "stage1_fixture", cache)
    report = build_campaign(load_config(ROOT / "stage1_configs" / "synthetic_linked.json"), cache, workspace / "bundle")
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(workspace / "bundle/public/manifest.json")))
    audit = PublicCampaignAuditor().audit(campaign)
    print(f"data_kind=synthetic campaign={campaign.campaign.campaign_id} candidates={len(campaign.candidates)} primary_observations={len(campaign.observations)}")
    print(f"public_contract_checked=True hidden_followup_measurements={report.hidden_followup_measurements} chemical_audit_ok={audit.ok}")
    if any(issue.code == "rdkit_unavailable" for issue in audit.issues):
        print("chemical audit skipped: install assaypilot[chem] to enable RDKit parsing")
    elif not audit.ok:
        raise SystemExit("public campaign audit failed")
