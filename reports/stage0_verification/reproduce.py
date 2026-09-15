"""drug 환경에서 예제·전체 테스트·오류 주입을 실행하고 증빙을 저장한다."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
import assaypilot
import pydantic
import pytest
from pydantic import ValidationError
from assaypilot.domain import PublicCampaign, Prerequisite, validate_public_campaign

metadata = {
    "executed_at_utc": datetime.now(timezone.utc).isoformat(),
    "python": sys.version, "executable": sys.executable,
    "pydantic": pydantic.__version__, "pytest": pytest.__version__,
    "imported_package": assaypilot.__file__,
    "cwd": str(ROOT), "PYTHONPATH": str(ROOT / "src"),
}
(OUT / "environment.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
env = os.environ.copy()
env["PYTHONPATH"] = str(ROOT / "src")
for filename, args in [
    ("example.log", ["examples/stage0_contract_demo.py"]),
    ("pytest.log", ["-m", "pytest", "-v", "--color=no", "--junitxml=reports/stage0_verification/pytest.xml"]),
]:
    result = subprocess.run([sys.executable, *args], cwd=ROOT, env=env,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (OUT / filename).write_text("command: " + " ".join([sys.executable, *args]) +
                               "\nPYTHONPATH=" + env["PYTHONPATH"] + "\n" + result.stdout +
                               f"\nexit_code: {result.returncode}\n")
    print(result.stdout)
    if result.returncode:
        raise SystemExit(result.returncode)


def fresh():
    """오류 주입이 원본 합성 예제에 영향을 주지 않도록 새 객체를 읽는다."""
    return PublicCampaign.model_validate_json((ROOT / "examples/synthetic_campaign.json").read_text())


cases = []
for name in ("unknown_assay_id", "self_cycle", "two_assay_cycle", "three_assay_cycle"):
    public = fresh()
    if name == "unknown_assay_id":
        public.observations[0].assay_id = "missing"
    else:
        public.assays[0].prerequisites = [Prerequisite(
            assay_id="screen" if name == "self_cycle" else "confirmation", kind="observed")]
        if name == "three_assay_cycle":
            public.assays[1].prerequisites = [Prerequisite(assay_id="interference", kind="observed")]
    result = validate_public_campaign(public)
    expected = "unknown_reference" if name == "unknown_assay_id" else "cycle"
    assert not result.ok and any(i.code == expected for i in result.issues)
    cases.append({"case": name, "ok": result.ok, "issues": result.model_dump(mode="json")["issues"]})

payload = fresh().model_dump()
payload["observations"][0]["assay_id"] = ""
try:
    PublicCampaign.model_validate(payload)
except ValidationError as exc:
    cases.append({"case": "empty_assay_id", "exception": "ValidationError",
                  "errors": exc.errors(include_url=False, include_context=False)})
else:
    raise AssertionError("empty assay ID was accepted")
(OUT / "negative_cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(cases, ensure_ascii=False, indent=2))
