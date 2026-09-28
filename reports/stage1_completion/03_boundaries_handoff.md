# 1단계 마무리 05–06: 경계 검증·후속 감사·확장 후보군·2단계 인계

실행일: 2026-09-18

이 기록은 첨부 지침 `docs/assaypilot-stage1-finish-05-06.md`의 05(누락된 경계 테스트와 후속 데이터 감사 보완)와 06(확장 replay 후보군 및 2단계 인계 문서)을 실제 코드·데이터·검증에 적용한 결과다. 01–02의 SMILES 보존·RDKit 감사와 03–04의 snapshot·원본 trace·endpoint 의미 분리를 유지했다. 2단계 실행기, 예산 차감, 학습·평가 루프는 구현하지 않았다.

## 선행 결과와 보존 위치

| 자료 | 위치 |
| --- | --- |
| 01–02 구조/RDKit 보고서 | `reports/stage1_completion/01_structure_audit.md` |
| 03–04 snapshot/의미 보고서 | `reports/stage1_completion/02_snapshot_semantics.md` |
| 03–04 smoke revision | `data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/` |
| 03–04 이전 보존본 | `data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-before-03-04/` |
| 05–06 확장 revision | `data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/` |
| 2단계 연결 문서 | `docs/stage2_data_handoff.md` |

각 revision은 `raw/` response와 `.meta.json`, `bundle/public/`, `bundle/curator/`, `verification/`, `implementation/`, `snapshot_manifest.json`을 보존한다. `implementation/working_tree.patch`, `implementation/changed_files/`, `implementation/RESTORE.txt`로 해당 실행 소스를 복구할 수 있다. 구현은 검증 당시 commit `5298c52aa52e73d726e21f3aa4803964cf21fc29`와 dirty 상태를 manifest에 기록한다.

## 변경 내용

### 05-A 공개/미공개 경계

- `tests/test_stage1_data_pipeline.py`에 후속 판정·수치·행 순서·비공개 설명 텍스트 변경과 전체 후속 행 제거 fixture를 추가했다.
- 변형 전후 `public/` 전체 상대 경로·바이트 SHA-256을 비교한다. 후보, Observation, primary evidence trace와 public manifest가 동일하고, follow-up 0건은 `no_linked_followup` 진단 및 curator 미측정 통계로만 남는다.
- 유효한 PubChem 경로에서도 선택 후보의 follow-up이 0건이면 public bundle을 만들고 `no_linked_followup` warning과 `unmeasured_selected_candidates`를 남긴다. 파일 누락·파싱 오류·잘못된 행은 여전히 오류로 중단한다.
- public evidence에는 primary 관측별 `SID`, `AID`, 원본 `Activity Outcome`, source row ID·번호·파일 SHA-256·최소 raw row가 남는다. 후속 결과·후속 분포·curator 경로는 공개하지 않는다.

### 05-B 측정 의미·정체성

- 같은 `(assay_id, SID)`의 여러 원본 행을 평균·덮어쓰기·제거하지 않고 `NormalizedMeasurement`로 각각 보존한다.
- 상충 verdict, SID↔CID 충돌, CID 하나에 여러 SID를 별도 감사 통계로 기록한다. SID가 서로 다른 CID를 가리키면 build 오류다.
- `replicate_id`와 `condition_id`가 원본에 없으면 `not_reported:<stable id>`를 사용한다. 이는 실제 반복 조건을 확인했다는 뜻이 아니다.
- 기존 수치 0·비교 연산자·결측·endpoint/unit 검사를 유지하고, category-only assay에서 `Activity Name`과 `Activity Value*`가 실제로 비어 있는지 계속 검사한다.
- synthetic counter assay에서 `AssayRole.COUNTER`, Active/Inactive 의미와 명시된 counter `SuccessCondition`이 build 후 public `campaign.json`과 `PublicBundleAdapter.load` 뒤에도 동일한지 검증한다. Active를 자동 성공으로 계산하는 엔진은 추가하지 않았다.

### 05-C 시점·파일·수집 실패

- 초기 공개 Observation의 `released_at`은 `CampaignConfig.initial_as_of`이며, 미공개 `NormalizedMeasurement`에는 공개 시각을 넣지 않는다.
- build는 임시 출력 디렉터리에 먼저 작성하고 성공 때만 교체한다. 이미 정상 output이 있으면 덮어쓰지 않으며, 작성 도중 실패하면 임시 output만 제거하고 기존 결과를 보존한다.
- 수집기는 429/5xx에 대해 제한된 retry와 2 requests/sec 제한을 적용한다. 숫자와 HTTP-date `Retry-After`를 파싱하고 대기 상한을 둔다.
- 응답·metadata를 임시 파일에 함께 쓴 뒤 교체한다. response 또는 metadata 쓰기·교체가 실패하면 기존 cache/meta를 복구하고 `.partial`·`.backup`을 제거한다. 빈 응답, HTML, 잘못된 content type, 불완전 concise CSV, timeout과 최종 오류는 정상 cache로 취급하지 않는다.

이번 보완 중 atomic cache rollback은 기존 파일을 먼저 옮긴 뒤 metadata 교체가 실패하는 경로뿐 아니라 첫 임시 response 쓰기부터 실패하는 경로에서도 원본을 유지하도록 수정했다. 이 두 실패 지점을 테스트로 확인했다.

### 05-D 개발자용 감사 통계

`DataAuditReport`에 다음을 추가했다. 모두 `bundle/curator/data_audit_report.json`에만 기록하며 PublicCampaign, 공개 evidence와 `inspect` 결과에는 넣지 않는다.

- assay별 원본·포함·제외·오류 행 수와 verdict 분포
- 고유 SID/CID, SID에 여러 CID, CID에 여러 SID, 반복·상충 `(assay_id, SID)` 그룹
- primary 원본 행, primary active 원본 행, primary tested/active 고유 SID, 최종 선택 수
- 선택 후보와 연결된 follow-up SID 수, 후보×시험 조합 수, follow-up verdict 분포, 미측정 선택 후보 수

후속 전체 분포와 선택 후보에 연결된 분포를 분리한다. 미측정은 inactive, inconclusive 또는 실패로 바꾸지 않는다.

## 06 확장 후보군

기존 빠른 smoke 설정과 후보 5개 revision은 변경하지 않았다.

```text
examples/stage1_configs/pubchem_tor_mep2_snapshot.json
data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/
```

별도 확장 설정은 다음과 같다.

```text
examples/stage1_configs/pubchem_tor_mep2_primary_active_all.json
data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/
```

확장 선정 함수 `assaypilot.data.selection.select_primary_sids`를 fetch와 build가 함께 사용한다. 입력은 primary 행의 SID, CID, outcome과 고정 seed뿐이며 confirmatory/follow-up 존재 여부를 받지 않는다. 고정 snapshot의 AID 2016 primary Active는 **원본 행 1,682개**이고, 이 행에서 계산한 고유 Active SID도 1,682개다. 후속 AID 2272가 있는 295개만으로 후보를 줄이지 않았다.

확장 revision의 실제 감사 수치는 다음과 같다.

| 항목 | 값 | 분모/단위 |
| --- | ---: | --- |
| primary raw rows | 326,763 | AID 2016 행 |
| primary Active raw rows | 1,682 | AID 2016 행 |
| primary tested unique SID | 326,763 | SID |
| primary Active unique SID | 1,682 | SID |
| selected candidates | 1,682 | SID |
| all normalized rows | 328,519 | AID 2016 + 2272 행 |
| linked follow-up candidate SID | 295 | 선택 SID |
| linked follow-up candidate×assay | 295 | `(SID, assay)` |
| follow-up verdict | Active 30 / Inactive 265 | 295 측정 |
| selected candidates without follow-up | 1,387 | 1,682 선택 SID |
| unique SID / CID | 326,773 / 326,653 | 전체 포함 측정 |
| CID shared by multiple SIDs | 115 | CID 그룹 |
| SID with multiple CIDs | 0 | SID 그룹 |
| repeated/conflicting assay-SID groups | 0 / 0 | 현재 원본 |
| structure audit | 1,682 / 1,682 passed | RDKit candidates |

후속 coverage는 후보 선택 뒤 감사한 결과다. 이를 이용해 후보를 재선정하거나 seed를 재탐색하지 않았다. 확장 데이터는 후속 측정이 1건보다 많다는 사실을 보여주지만 여러 assay 유형의 모델 평가나 biological binding/efficacy 결론에 충분하다고 주장하지 않는다.

## 실제 실행과 검증

### 테스트와 synthetic 경계 fixture

모든 Python 명령은 conda `drug` 환경에서 실행했다.

```bash
conda run -n drug python -m pytest -q
```

실행 결과:

```text
141 passed in 0.66s
```

추가·수정 테스트는 다음을 포함한다.

- 후속 label/수치/순서/설명 변경과 후속 전체 삭제 후 public tree SHA-256 불변
- `test_pubchem_no_followup_builds_public_bundle_and_reports_missing`: `data_kind="pubchem"`의 유효한 primary-only 입력을 warning으로 생성하고 public SHA-256·미측정 통계를 확인
- `test_pubchem_missing_followup_file_is_not_treated_as_zero_coverage`: 후속 파일 누락은 정상적인 0건으로 삼지 않고 오류로 유지
- `test_counter_assay_active_meaning_survives_build_serialization_and_load`: counter role, verdict meaning, configured success condition의 build·직렬화·로딩 보존 확인
- `test_adapter_rejects_unsupported_public_manifest_version`: 지원하지 않는 public manifest schema version을 `ValueError`로 거절
- 같은 SID·시험 반복 및 상충 행 보존, 반복/상충 통계
- 초기 `released_at` 검증, public-only Adapter와 실제 RDKit Auditor
- 누락 reference, hash 불일치, 경로 이탈 거절(기존 테스트 유지)
- build 출력 작성 중 실패와 기존 성공 output 보호
- 429/5xx·timeout 유한 retry, numeric/HTTP-date `Retry-After`
- 빈/HTML/불완전 CSV 거절
- cache response/meta 교체 전·후 실패에서 원본 cache 보호

이 테스트의 PubChem HTTP 경로는 mock response를 사용한다. 실제 네트워크 fetch와 확장 bundle 검증은 아래 별도 로그에 기록한다.

### 실제 PubChem fetch/build

확장 config의 `structure_fetch_from_primary=true`, `structure_batch_size=100`으로 AID 2016/2272 concise와 description은 고정 cache를 재사용하고, 선택된 1,682 primary CID의 `SMILES` 및 `ConnectivitySMILES`를 각각 17개 batch로 수집했다. 실제 response SHA-256은 다음과 같다.

```text
raw/compounds_smiles.csv        0528909a3cfa62fd261272ea862b3f15782dc3343d0eb08c9b662efb179bf0ed
raw/compounds_connectivity.csv 9db71d3a1e617fb84d9f5870cff62ac81b186c78c7ba2cd2f50000a52a632112
```

실제 수집 명령은 다음과 같고, 최종 재확인은 모든 response를 동일 SHA-256으로 재사용했다.

```bash
conda run -n drug python -m assaypilot.data fetch \
  examples/stage1_configs/pubchem_tor_mep2_primary_active_all.json \
  data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/raw
```

수집 로그: `data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/verification/fetch.log`.

실제 build report에는 오류가 없고, `selected_candidates=1682`, `hidden_followup_measurements=295`다. build 로그는 `verification/build.log`다.

오프라인 build 명령은 다음과 같다. 새 output 경로를 사용해 기존 성공 bundle을 덮어쓰지 않았다.

```bash
conda run -n drug python -m assaypilot.data build \
  examples/stage1_configs/pubchem_tor_mep2_primary_active_all.json \
  data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/raw \
  data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/verification/reproduced_bundle
```

```text
campaign=pubchem-tor-mep2-20260915-primary-active-all candidates=1682 assays=mep2-primary,mep2-confirmatory observations=1682 audit_ok=True
chemical_audit_backend=rdkit candidates=1682 structures_present=1682 checked=1682 passed=1682 failed=0
```

이 결과는 `verification/inspect_bundle.log`에 있고, curator를 제외하고 `bundle/public`만 복사한 뒤의 같은 결과는 `verification/inspect_public_copy.log`에 있다. 공개 JSON에서 비공개 후속 텍스트가 노출되지 않는 것도 확인했다.

### 고정 cache 오프라인 재현

동일 raw/config로 `verification/reproduced_bundle`을 만든 뒤 `verification/public_comparison.json`으로 비교했다. 결과는 다음과 같다.

```json
{"identical": true, "missing_in_reproduced": [], "extra_in_reproduced": [], "changed": []}
```

재현 inspect도 RDKit 1,682/1,682 통과이며 `verification/inspect_reproduced.log`에 있다. `public/`의 campaign, 두 evidence, manifest SHA-256이 모두 동일하다.

## 이번 05–06 후속 점검

- 유효한 PubChem 입력에서 follow-up 0건을 public bundle 생성 가능 상태로 통일했다. `data_audit_report.json`에 warning과 연결 후속 0건·미측정 통계를 남기며, cache 누락·파싱·AID/판정 오류는 계속 중단한다. 실제 r2 config/cache에서 AID 2272 행을 전부 제거한 임시 재생 결과도 public tree가 동일했다.
- `docs/stage1_data_pipeline.md`의 AID 2272 설명을 보존된 공식 description의 single-concentration confirmatory 의미로 고쳤고, AID 504468의 dose-response SAR와 분리했다. 설정과 public evidence에는 같은 오표기가 없었으며 수치 endpoint를 재구성하지 않았다.
- `docs/stage2_data_handoff.md`에 실제 curator 파일 경로, `NormalizedMeasurement` 검증 로딩, `Candidate.source/source_id`와 SID 매핑, 버전 기록 위치·미지원 버전 처리, 전체 정규화 행과 hidden 부분집합의 차이를 기록했다. 실제 확장 bundle에서 328,519개 행과 hidden 295개를 검증 로딩하고 295개 candidate ID 매핑을 확인했다.

## 상태와 남은 책임

| 1단계 항목 | 상태 | 근거/제약 |
| --- | --- | --- |
| 코드·설정·원본 snapshot 보존 | 완료 | 두 revision의 raw, metadata, implementation, manifest |
| 공개/미공개 경계 | 완료 | mutation/removal SHA test, public-only load |
| 원본 관측 trace | 완료 | primary evidence의 SID/AID/raw row/source hash |
| endpoint·판정 의미 | 완료 | `02_snapshot_semantics.md`, category-only 실제 검사 |
| 경계 실패 처리 | 완료 | 141개 테스트, atomic/retry/build failure 경로 |
| 실제 구조 감사 | 완료 | RDKit 1,682/1,682 |
| 확장 후보군 | 완료 | primary-only 1,682 unique SID, follow-up 독립 선택 |
| 2단계 인계 | 완료 | `docs/stage2_data_handoff.md` |
| 2단계 실행기·학습·예산 | 미구현 | 이번 지침의 명시적 제외 범위 |

2단계는 `PublicBundleAdapter.load(DataSource(...))`로 초기 public manifest를 읽고, 승인된 `NormalizedMeasurement`를 새 Observation 객체로 만든 뒤 현재 실행 시점을 `validate_execution(..., as_of=...)`에 전달해야 한다. 초기 `as_of`를 덮어쓰거나 미래 관측을 허용해서는 안 된다. EvidenceRef 등록, 반복 측정의 실행 의미, 미측정 처리·과금, 격리·이력·중복 실행 방지는 `docs/stage2_data_handoff.md`에 남긴 인계 결정 사항이다.

이번 자료는 재현 가능한 1단계 데이터 준비와 경계 검증을 완료했지만, 1,387개 미측정 후보와 원본에서 구분되지 않는 조건·반복 정보가 있다. 따라서 이 snapshot만으로 assay 성능, binding, efficacy, safety 또는 모델 일반화를 판정하지 않는다.
