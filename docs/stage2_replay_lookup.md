# Stage 2A: 내부 후속 측정 조회

Stage 2A는 고정된 Stage 1 snapshot과 그 안의 초기 `PublicCampaign`을 함께 검증한 뒤, 공개 후보와 후속 시험 ID로 보존된 `NormalizedMeasurement` 원본 행을 조회한다. 이 모듈은 신뢰된 실행기 내부용이다. `PublicBundleAdapter`와 공개 로딩 경로는 계속 curator 자료를 읽지 않는다.

## 호출 계약

```python
from pathlib import Path

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.domain import DataSource
from assaypilot.replay import ReplayOracle, load_replay_store

root = Path("data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all")
public = PublicBundleAdapter().load(DataSource(
    kind="public_bundle",
    location=str(root / "bundle/public/manifest.json"),
))
store = load_replay_store(root, public)  # snapshot당 한 번 로드
oracle = ReplayOracle(store)             # 실행 중 재사용

candidate_id = public.candidates[0].candidate_id
result = oracle.lookup(candidate_id, "mep2-confirmatory")
if result.status == "records_found":
    for measurement in result.measurements:
        print(measurement.measurement_id, measurement.raw_verdict, measurement.value)
elif result.status == "no_record":
    print("이 snapshot에서 이 후보·시험 조합에 연결된 기록이 없음")
```

정확한 공개 API 시그니처는 다음과 같다.

```python
load_replay_store(snapshot_root: str | Path, public_campaign: PublicCampaign) -> ReplayStore
ReplayOracle.lookup(candidate_id: str, assay_id: str) -> ReplayLookupResult
```

`ReplayLookupResult`는 `status`, `snapshot_id`, `campaign_id`, 요청한 `candidate_id`와 `assay_id`, 측정 튜플 `measurements`를 가진다. 상태는 `records_found` 또는 `no_record`다. 결과에는 다른 후보의 결과, 전체 coverage, 성공 판정이 포함되지 않는다. 여러 원본 행은 전부 반환하며 평균·첫 행 선택·다수결·중복 내용 제거를 하지 않는다. 측정은 `measurement_id` 오름차순으로 반환한다.

`NormalizedMeasurement` 모델을 재사용하므로 `raw_verdict`, 내부 매핑 verdict, 수치 0, 단위, 비교 연산자, 결측, `raw_row`, `source_row_id`, 원본 행 번호·파일 SHA-256, 반복/조건 식별자와 protocol 위치를 보존한다. 각 호출은 검증된 JSON 바이트에서 새 객체를 생성한다. 결과의 중첩 `raw_row`를 호출자가 바꾸어도 다음 조회나 `PublicCampaign`은 바뀌지 않는다.

## 식별자와 원본 일관성

candidate 연결은 공개 `Candidate.source == "pubchem_sid"`와 `Candidate.source_id == "SID:<sid>"`를 적재 시 SID→candidate ID 색인으로 만든 뒤 `NormalizedMeasurement.sid`와 대응한다. candidate ID를 분해하지 않고 CID로 연결하지 않는다. 동일 CID를 가진 서로 다른 SID는 서로 다른 후보 key로 유지한다.

적재 시 공개 campaign의 검증된 내용이 snapshot public bundle과 같은지 확인한다. config와 public assay 정의·campaign 계약도 비교한다. 각 후속 행에 대해 config의 assay/AID, 공개 SID 후보, raw row의 AID/SID/CID와 Activity Outcome, 설정된 endpoint·unit·verdict mapping을 확인한다. `source_file_sha256`은 snapshot `raw/<concise_cache_key>`의 등록 SHA-256과 비교한다. hidden 파일 안의 primary 행, 미등록 시험, 공개 후보에 없는 SID, 중복 `measurement_id`, 동일 SID의 여러 CID는 오류다.

## 스냅샷·버전·오류

`load_replay_store`는 하나의 신뢰된 snapshot root만 받는다. manifest의 모든 `full_sha256` 등록 파일을 POSIX no-follow 파일 열기로 스트리밍 해시 검증한다. config와 hidden 파일은 등록된 SHA-256을 확인한 동일 bytes를 파싱한다. public 경로의 symlink도 Adapter에 넘기기 전에 차단하며, campaign 내용 검증은 기존 `PublicBundleAdapter`에 맡긴다. 전체 인벤토리에 `normalized_measurements.json`이 포함된 경우 해당 파일은 바이트 해시만 스트리밍 확인한다. 그 배열을 replay 색인으로 파싱하지 않고, 매 조회에서도 다시 읽지 않는다.

- `bundle/public/manifest.json`은 `schema_version=1.0.0`이며 `PublicBundleAdapter`가 지원 version과 file hash를 확인한다.
- 공개 `PublicCampaign` envelope는 `0.1.0`이다. config는 `CampaignConfig.schema_version=1.0.0`이다.
- `snapshot_manifest.json`은 독립 `schema_version`이 없는 보존 manifest이며 `snapshot_id`, `campaign_id`, `full_sha256` 인벤토리를 가진다.
- `hidden_followup_measurements.json`과 전체 `normalized_measurements.json`은 버전 envelope 없는 JSON 배열이다. 배열 각 원소를 `NormalizedMeasurement`로 검증한다. 별도 배열 버전은 현재 존재하지 않는다.

경로 이탈·symlink, 누락·해시 불일치, 지원하지 않는 manifest/config 구조, 잘못된 JSON·레코드·참조, 다른 campaign 입력은 `ReplayLoadError`로 거절한다. 잘못된 candidate/assay 및 primary 조회는 `ReplayRequestError`다. 오류는 `no_record`로 변환하지 않는다. manifest 해시는 보존 파일의 변경 탐지값이다. `snapshot_manifest.json` 자체의 서명이나 외부 인증은 없으므로 이를 악의적으로 교체한 snapshot의 진위를 보증하지 않는다. POSIX `O_NOFOLLOW`와 `O_DIRECTORY`가 없는 환경에서는 안전 적재를 거절한다.

`no_record`는 오직 이 snapshot에서 해당 공개 후보·후속 시험 조합의 연결 기록이 없음을 뜻한다. 실제 실험 미수행, inactive, inconclusive 또는 실패라는 의미가 아니다. counter assay의 `active`도 원본 판정과 설정 의미로 보존할 뿐 성공으로 재해석하지 않는다.

## 실제 snapshot 검증

고정 revision을 변경하지 않고 실행한 개발자 검증 명령은 다음과 같다.

```bash
conda run -n drug python scripts/verify_replay_snapshots.py
```

검증기는 r2 5개 후보/1개 후속 행과 확장 1,682개 후보/295개 후속 행을 실제 loader와 oracle로 조회한다. 확장 snapshot에서는 `normalized_measurements.json`의 전체 328,519개 레코드를 일회성 개발자 검증에서 `NormalizedMeasurement`로 읽어, 선택 SID에 연결된 nonprimary 행이 hidden 배열과 ID·전체 필드 기준으로 동일한지 비교한다. 이 대조용 전체 파일은 replay loader의 조회 자료가 아니다. 결과와 원본 verdict 분포는 개발자 보고서에만 기록한다.

## 다음 단계 연결점과 미구현 경계

2-B의 명시적 승인·예산 예약·실행 이력·중복 요청 방지·private result 보존은 [stage2_execution_control.md](stage2_execution_control.md)에 구현했다. 실행기는 snapshot/store/oracle을 한 번 적재한 다음, 승인·초기 공개 선행조건·비용·잔액 검증을 통과한 경우에만 `ReplayOracle.lookup(candidate_id, assay_id)`를 내부 호출한다.

2-B는 `Observation`/`EvidenceRef` 생성·공개, `released_at` 할당, `RunState` 교체, settlement, OS/container 접근 격리, agent tool/API 노출을 구현하지 않는다. 공개 결과 단계는 새 객체를 만든 뒤 기존 참조·시점 validator를 적용하고 검증 성공 시에만 교체해야 한다. 그 공개·settlement 경계는 2-C 책임이다.
