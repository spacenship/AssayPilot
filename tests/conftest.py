"""외부 자원 없이 로딩하는 합성 공개 캠페인 fixture."""
from pathlib import Path
import pytest
from assaypilot.domain import PublicCampaign


@pytest.fixture
def public():
    """매 테스트에 독립적인 합성 캠페인을 제공한다."""
    path = Path(__file__).resolve().parents[1] / "examples" / "synthetic_campaign.json"
    return PublicCampaign.model_validate_json(path.read_text())
