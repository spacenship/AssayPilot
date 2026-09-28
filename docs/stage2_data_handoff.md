# 2단계 데이터 인계

이 문서는 1단계에서 실제로 생성한 공개 campaign과 개발자용 측정 자료를 2단계 실행기가 읽을 수 있도록 연결점을 고정한다. 2단계 실행기, 학습, 예산 차감, 승인 저장소는 이 문서에서 구현하지 않는다.

## 입력과 공개 경계

- 작은 smoke 입력: `data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/bundle/public/manifest.json`
- 확장 primary-only 입력: `data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/bundle/public/manifest.json`
- 공개 로더: `assaypilot.data.adapter.PublicBundleAdapter.load(DataSource(...))`
- 공개 검사: `assaypilot.data.audit.PublicCampaignAuditor.audit(PublicCampaign)`
- 공개 패키지는 `public/manifest.json`의 허용 파일과 SHA-256만 읽는다. `curator/`, `raw/`, fetch cache는 에이전트 입력으로 노출하지 않는다.

```python
from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.domain import DataSource

public = PublicBundleAdapter().load(DataSource(
    kind="public_bundle",
    location="data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/bundle/public/manifest.json",
))
```

후속 입력은 확장 revision의 다음 두 실제 파일이다.

```text
data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/bundle/curator/normalized_measurements.json
data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/bundle/curator/hidden_followup_measurements.json
```

두 파일은 공개 Adapter가 읽지 않으며, 2단계의 승인 경계 안에서만 `NormalizedMeasurement`로 검증해 읽는다. 앞의 public loader 블록과 다음 블록을 저장소 루트에서 순서대로 실행하면 실제 확장 bundle을 검증 로딩한다.

```python
from pathlib import Path
from pydantic import TypeAdapter

from assaypilot.data.schemas import NormalizedMeasurement

bundle = Path("data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/bundle")
measurement_list = TypeAdapter(list[NormalizedMeasurement])
all_measurements = measurement_list.validate_json(
    (bundle / "curator/normalized_measurements.json").read_bytes()
)
hidden_followup = measurement_list.validate_json(
    (bundle / "curator/hidden_followup_measurements.json").read_bytes()
)
assert all_measurements and len(hidden_followup) == 295
assert all(item.assay_id == "mep2-confirmatory" for item in hidden_followup)
print(len(all_measurements), len(hidden_followup))
```

`normalized_measurements.json`은 primary와 confirmatory/follow-up을 포함한 **전체 정규화 행**이다. `hidden_followup_measurements.json`은 그중 선택 후보 SID에 연결된 primary 이외의 행만 담은 인계용 부분집합이다. 따라서 “후속 측정은 hidden 파일에만 있다”라고 해석하지 않으며, public 패키지에는 두 파일 모두 노출하지 않는다.

초기 `PublicCampaign.as_of`는 `2026-09-15T00:00:00Z`이며 초기 Observation의 `released_at`도 이 시점이다. 이후 결과는 초기 bundle에 직접 대입하지 않고 2단계에서 새 객체를 만든 뒤 `validate_execution(..., as_of=<현재 실행 시점>)` 또는 관련 배치 검사를 통과했을 때 교체해야 한다.

## 후속 측정 형식

`curator/normalized_measurements.json`은 `assaypilot.data.schemas.NormalizedMeasurement`의 JSON 배열이다. 주요 필드는 다음과 같다.

- `measurement_id`: assay와 원본 행으로 만든 중간 ID
- `assay_id`, `aid`, `sid`, `cid`: 시험과 PubChem 식별자. SID를 관측 연결 기준으로 유지하며 CID가 같아도 SID를 합치지 않는다.
- `raw_verdict`, `verdict`, `value`, `unit`, `comparison`: 원본 판정과 수치 측정을 분리한다. category-only AID 2016/2272는 값·단위가 없다.
- `replicate_id`, `condition_id`: 원본에 구분 정보가 없으면 `not_reported:<stable id>`로 기록한다. 이는 실제 반복 조건이 확인되었다는 뜻이 아니다.
- `source_row_id`, `source_row_number`, `source_file_sha256`, `raw_row`: 원본 행을 다시 확인하는 추적 정보
- `protocol_location`, `original_smiles`: 공식 assay와 PubChem `SMILES` 보존값

초기 공개 Observation은 `PublicCampaign.observations`에만 있으며 public evidence의 primary trace가 SID/AID/raw outcome/source row를 연결한다. 후속 측정은 curator의 전체 정규화 파일과 선택 후보 부분집합 파일에 보존되며, 공개 evidence에는 포함하지 않는다.

## 후보 ID와 SID 연결

`build_campaign`은 선택 primary 측정의 SID를 `candidate_by_sid: dict[int, str]`로 먼저 고정한 뒤 같은 SID의 primary Observation에 `candidate_id`를 넣는다. 직렬화된 후보에는 이 관계가 `Candidate.source="pubchem_sid"`, `Candidate.source_id="SID:<sid>"`로 보존되고, 측정 자료의 기준 필드는 `NormalizedMeasurement.sid`이다. 2단계는 CID 문자열이나 임의의 ID 문자열 분해로 연결하지 말고 이 명시된 source 종류와 정확한 `SID:<sid>` 값의 대응을 사용해야 한다.

```python
def candidate_id_for_sid(campaign, sid: int) -> str:
    source_id = f"SID:{sid}"
    matches = [candidate.candidate_id for candidate in campaign.candidates
               if candidate.source == "pubchem_sid" and candidate.source_id == source_id]
    if len(matches) != 1:
        raise ValueError(f"SID is not mapped to exactly one public candidate: {sid}")
    return matches[0]

mapped_candidate_ids = {
    candidate_id_for_sid(public, measurement.sid) for measurement in hidden_followup
}
assert len(mapped_candidate_ids) == 295
print(len(mapped_candidate_ids))
```

이 매핑 블록은 앞의 두 로딩 블록에서 만든 `public`과 `hidden_followup`을 이어서 사용한다.

## 조회 단위와 중복

후보 단위는 `(campaign_id, candidate_id)`이고 시험 결과 조회 단위는 `(candidate_id, assay_id)`이다. 동일 SID·시험의 여러 원본 행은 원본 측정으로 남기며 평균·덮어쓰기를 하지 않는다. 현재 원본에는 `replicate_id`와 `condition_id`를 구별할 정보가 없으므로 반복/조건의 과학적 동일성을 주장할 수 없다. SID→CID 충돌은 build 오류이고, 동일 CID의 여러 SID는 감사 통계에 남긴다.

## 스키마 버전과 결과 근거

실제 버전 기록 위치는 서로 다르다.

- `bundle/public/manifest.json`의 `schema_version`은 `1.0.0`이며 `PublicBundleAdapter`가 `kind`와 함께 확인한다.
- `bundle/public/campaign.json`의 `schema_version`은 `PublicCampaign` envelope의 `0.1.0`이다. 지원하지 않는 값은 Pydantic 검증에서 거절된다.
- `CampaignConfig`와 `curator/data_audit_report.json`은 각각 `schema_version=1.0.0`을 가진다.
- `curator/normalized_measurements.json`과 `hidden_followup_measurements.json`은 JSON 배열이고 파일 envelope나 독립 schema version 필드가 없다. 현재는 bundle manifest/config의 버전을 고정하고 각 원소를 `NormalizedMeasurement`로 검증하는 것이 장치의 범위다. 2단계에서 이 두 파일의 별도 버전 협상이 필요하면 새 envelope 계약으로 명시해야 한다.

지원하지 않는 public manifest 버전·kind, 누락/경로 이탈/해시 불일치는 `PublicBundleAdapter.load`가 `ValueError`로 거절한다. 기존 r2와 확장 revision의 파일을 덮어쓰지 않고, 새로운 스키마가 필요하면 새 revision으로 보존한다.

## 결과 근거와 시점

공개 Observation의 `evidence_ids`는 `public/evidence/*.json`의 `EvidenceRef.location`으로 연결된다. 수신된 후속 Observation은 원본 근거 payload와 `source_row_*` 식별자를 등록한 뒤 참조 검사를 받아야 한다.

- `initial_as_of`: 초기 공개 snapshot의 기준 시점
- 원본 실험 날짜: 현재 concise export에 별도 확정 필드가 없으면 미지정으로 둔다.
- `released_at`: 공개할 때 부여하는 시점. 후속 결과는 현재 실행 시점을 명시적으로 전달하고 미래 관측을 거절한다.

## 부재와 오류의 구분

- 후속 행이 없음: 이 snapshot에서 선택 후보 SID에 연결된 후속 기록이 없는 상태다. 이를 근거로 실제 실험이 수행되지 않았다고 단정하지 않으며, inactive나 실패로 바꾸지 않는다.
- `Activity Outcome=Inactive`: 원본에 기록된 inactive 판정
- `Activity Outcome=Inconclusive`: 공식 assay 범주이며 별도 의미로 유지한다.
- 파일·파싱·구조 오류: `DataAuditReport.issues`의 오류로 남기고 정상 bundle을 만들지 않는다.
- 확장 자료에서 primary-active 1,682개 중 후속 관측이 연결된 SID는 295개, 이 snapshot에서 연결 기록이 없는 SID는 1,387개다. 이 통계는 curator 전용이며 public campaign에 넣지 않는다.

## 1단계 자료의 범위와 한계

확장 후보군은 AID 2016의 Active **원본 행 1,682개**에서 고유 SID를 계산한 결과 1,682개를 모두 사용한다. AID 2272 후속 존재 여부로 후보를 줄이지 않았다. 후속 coverage는 295 SID(Active 30, Inactive 265)이고 이 snapshot에서 연결 기록이 없는 SID는 1,387개다. 이는 replay 입력의 범위와 선택 편향을 설명하는 감사 통계이며 모델 성능·binding·efficacy 검증이 아니다.

구조는 PubChem CID `SMILES`를 100개 batch로 수집하고 RDKit sanitize를 수행했다. 확장 감사는 1,682/1,682 구조 통과다. 원본 assay·SMILES cache, batch response와 metadata는 `revision-20260918-primary-active-all/raw/`에 보존한다.

## 2단계가 맡을 책임

1. 승인·예산 처리 후 선택된 후속 자료를 `NormalizedMeasurement`에서 Observation으로 변환하고 공개한다.
2. 초기 `as_of`를 실행 시각으로 덮어쓰지 않고, 현재 실행 시점을 `validate_execution(..., as_of=...)`에 전달해 `released_at`을 검사한다.
3. 공개 근거 payload와 `EvidenceRef`를 등록하고 해시·참조 검사를 통과시킨다.
4. 한 행동에 여러 측정이 연결될 때의 실행 의미, 미측정 행동의 처리와 과금 정책을 결정한다.
5. 파일·도구 접근 격리, 접수·실행 이력, 중복 실행 방지, `RunState`의 검증 후 교체를 구현한다.

실제 실행기나 학습·평가 루프는 이 단계에서 만들지 않았다. 특히 후속 coverage가 충분하지 않으므로 여러 assay 유형의 성능 비교를 자동으로 승인할 수 없다.
