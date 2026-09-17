"""공개 bundle manifest만 읽는 CampaignAdapter 구현."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from assaypilot.domain import DataSource, PublicCampaign


class PublicBundleAdapter:
    """public/manifest.json 아래의 허용 목록과 해시가 맞는 campaign만 로드한다."""

    supported_kind = "public_bundle"

    def load(self, source: DataSource) -> PublicCampaign:
        """공개 manifest만 읽고 curator/raw 경로·지원하지 않는 kind를 거절한다."""
        if source.kind != self.supported_kind:
            raise ValueError(f"unsupported DataSource.kind: {source.kind!r}")
        manifest_path = Path(source.location).resolve()
        if manifest_path.name != "manifest.json" or manifest_path.parent.name != "public":
            raise ValueError("public_bundle location must be a public/manifest.json file")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema_version") != "1.0.0" or manifest.get("kind") != "assaypilot_public_bundle":
            raise ValueError("unsupported public manifest")
        files = manifest.get("files")
        campaign_file = manifest.get("campaign_file")
        if not isinstance(files, dict) or campaign_file != "campaign.json" or campaign_file not in files:
            raise ValueError("invalid public manifest file list")
        root = manifest_path.parent
        for relative, expected_hash in files.items():
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                raise ValueError("invalid public manifest entry")
            path = (root / relative).resolve()
            if root not in path.parents or not path.is_file():
                raise ValueError(f"manifest file escapes public bundle or is missing: {relative!r}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected_hash:
                raise ValueError(f"public file SHA-256 mismatch: {relative!r}")
        campaign = PublicCampaign.model_validate_json((root / campaign_file).read_text())
        if campaign.campaign.campaign_id != manifest.get("campaign_id"):
            raise ValueError("manifest campaign_id mismatch")
        return campaign
