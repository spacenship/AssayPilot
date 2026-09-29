# Stage 2A 구현·검증 보고서: 내부 후속 측정 조회

실행 환경: conda `drug`  
실제 검증일: 2026-09-28  
범위: 고정 snapshot의 hidden 후속 측정을 검증·색인하고 `(candidate_id, assay_id)` 단위로 내부 조회한다. 2-B/2-C 실행기 기능은 구현하지 않았다.

## 1. Stage 1 선행 항목 재검증

| 항목 | 구분 | 확인 결과와 근거 |
| --- | --- | --- |
| 후속 0건 정책 | 기존 검증 확인 | `test_pubchem_no_followup_builds_public_bundle_and_reports_missing`는 `data_kind="pubchem"` 유효 입력에서 후속 전체 제거 시 bundle 생성, `no_linked_followup` warning, curator 통계, public tree hash 동일성을 확인한다. `test_pubchem_missing_followup_file_is_not_treated_as_zero_coverage`는 입력 파일 누락을 별도 오류로 확인한다. |
| AID 2272와 counter 의미 | 기존 검증 확인 | `test_counter_assay_active_meaning_survives_build_serialization_and_load`는 synthetic counter role, Active의 설정 의미 및 counter `SuccessCondition`이 build·직렬화·`PublicBundleAdapter.load` 뒤 보존되는지 확인한다. `docs/stage1_data_pipeline.md`는 AID 2272 단일 농도 confirmatory와 AID 504468 dose-response SAR를 구분한다. config/evidence에 동일한 dose-response 오표기가 없다는 이전 source review를 확인했다. 코드·데이터 변경은 하지 않았다. |
| 공개 trace와 파일/version 인계 | 기존 검증 확인 | `test_primary_observations_have_minimal_public_raw_traces`, `test_real_pubchem_snapshot_configuration_is_versioned_and_explicit`, `test_adapter_rejects_unsupported_public_manifest_version`를 확인했다. `docs/stage2_data_handoff.md`는 실제 `curator/data_audit_report.json`, 전체 정규화 파일과 hidden 부분집합, `Candidate.source/source_id`↔SID 연결, 실제 버전 위치를 명시한다. |

위 선행 테스트를 다음 명령으로 실행했다.

```text
conda run -n drug python -m pytest -q tests/test_stage1_data_pipeline.py -k 'pubchem_no_followup_builds_public_bundle_and_reports_missing or pubchem_missing_followup_file_is_not_treated_as_zero_coverage or counter_assay_active_meaning_survives_build_serialization_and_load or real_pubchem_snapshot_configuration_is_versioned_and_explicit or primary_observations_have_minimal_public_raw_traces or adapter_rejects_unsupported_public_manifest_version'
6 passed, 44 deselected in 0.14s
```

## 2. 구현 내용

| 파일 | 내용 |
| --- | --- |
| `src/assaypilot/replay.py` | `load_replay_store(snapshot_root, public_campaign) -> ReplayStore`, `ReplayOracle.lookup(candidate_id, assay_id) -> ReplayLookupResult`, `ReplayLoadError`, `ReplayRequestError`를 추가했다. 기존 `Oracle` Protocol은 없어 별도 내부 조회 모듈로 구현했다. |
| `tests/test_replay.py` | 격리된 synthetic cache에서 만든 snapshot으로 성공·부재·다중 행·오류와 불변성을 검증한다. 실제 PubChem 측정 fixture가 아니다. |
| `scripts/verify_replay_snapshots.py` | 보존된 두 실제 revision의 loader/oracle와 확장 normalized↔hidden 부분집합을 오프라인 검증한다. |
| `docs/stage2_replay_lookup.md` | 호출 예시, 결과·오류·식별자·버전·hash 경계 및 2-B 연결점을 기록한다. |
| `docs/stage2_data_handoff.md` | 기존 데이터 감사 예시를 runtime lookup과 구분하고 2-A 내부 API 연결점을 추가했다. |

재사용한 기존 코드는 `PublicBundleAdapter`, `validate_public_campaign`, `CampaignConfig`, `NormalizedMeasurement`, `parse_numeric`이다. snapshot 인벤토리의 모든 등록 파일은 POSIX no-follow 열기와 streaming SHA-256으로 확인한다. config와 hidden 파일은 등록 여부와 해시를 확인한 bytes를 파싱한다. `NormalizedMeasurement.source_file_sha256`은 inventory에 든 `raw/<concise_cache_key>` 해시와 대조한다. 전체 normalized JSON은 적재 색인이나 조회에 사용하지 않는다.

연결은 공개 `Candidate.source="pubchem_sid"`, 정확한 `Candidate.source_id="SID:<sid>"`와 `NormalizedMeasurement.sid`를 사용한다. CID는 후보 연결 키가 아니다. 공개 campaign은 snapshot 내 public bundle의 전체 내용과 같아야 한다. hidden 파일은 배열을 `list[NormalizedMeasurement]`로 검증하고 미등록 assay/SID, primary 행, AID·raw trace·endpoint 불일치, 중복 measurement ID, SID→복수 CID 및 source file hash 충돌을 거절한다.

유효한 요청의 응답은 `records_found` 또는 `no_record`다. 전자는 요청된 모든 행을 measurement ID 순으로 반환한다. 원본 raw label, 내부 verdict, 0, unit, comparator, 결측, source row/provenance 필드는 유지한다. 매 응답은 내부 보존 bytes에서 새 모델 객체를 만들어 nested `raw_row` 수정이 후속 조회에 영향을 주지 않게 한다. `no_record`는 해당 고정 snapshot 안의 기록 부재뿐이다. 파일·hash·schema·reference 오류는 `ReplayLoadError`, 잘못된 요청/unknown 후보·지원하지 않는 assay는 `ReplayRequestError`이며 부재로 숨기지 않는다.

## 3. Synthetic fixture 테스트

명령:

```text
conda run -n drug python -m pytest -q tests/test_replay.py
24 passed in 0.41s
```

| 검증 내용 | 실제 검사 |
| --- | --- |
| 정상 조회와 방어적 복사 | 후보 SID/AID/raw outcome, active verdict, 수치, unit/comparison 및 source trace가 유지된다. 반환된 `raw_row`를 수정한 뒤 재조회와 PublicCampaign 원본이 그대로다. |
| no record 및 잘못된 요청 | hash-valid 빈 hidden 배열은 `no_record`; unknown candidate, primary 및 unknown assay는 요청 오류다. |
| 반복·상충·결정성 | 같은 후보·assay의 active와 inactive 원본 두 행을 함께 반환하고 measurement ID 기준 순서는 hidden 배열 역순에서도 같다. |
| SID/CID 연결 | 다른 SID의 두 행이 같은 CID를 가져도 각 후보 조회로 분리된다. 수치 0과 `=` 비교자도 보존된다. 중복 candidate ID, 중복 SID 매핑, 미지원 source, 잘못된 SID source 형식은 거절한다. |
| snapshot 경계 | hash 불일치, hidden 누락, hash-valid JSON 손상, manifest 경로 이탈, 무관 inventory 파일 변조, raw source hash 충돌, hidden symlink, 다른 campaign 입력을 거절한다. |
| reference와 schema | 미등록 assay, hidden primary, AID 불일치, 공개되지 않은 SID, 중복 measurement ID, 미지원 config version을 거절한다. |
| 결과 모순 | `no_record`에 측정이 들어간 결과 객체를 거절한다. |
| 전체 배열 비사용 | 잘못된 JSON인 전체 정규화 배열은 inventory hash가 갱신된 fixture에서 해시 확인만 받고, hidden 배열만으로 조회가 완료된다. |

## 4. 고정 실제 snapshot 검증

명령:

```text
conda run -n drug python scripts/verify_replay_snapshots.py
```

결과:

| snapshot | 공개 후보 | 조회 상태(후보×시험) | 후속 행 | verdict |
| --- | ---: | --- | ---: | --- |
| `revision-20260917-r2` | 5 | `records_found=1`, `no_record=4` | 1 | Inactive 1 |
| `revision-20260918-primary-active-all` | 1,682 | `records_found=295`, `no_record=1,387` | 295 | Active 30, Inactive 265 |

확장 revision의 전체 `normalized_measurements.json` 328,519행을 별도 개발자 검증에서 `NormalizedMeasurement`로 로딩했다. 공개 후보의 SID와 nonprimary assay 기준으로 추린 부분집합 295행은 hidden 295행과 `measurement_id` 및 전체 모델 필드가 모두 일치했다. 이는 개발자용 부분집합 검증이며 replay loader는 normalized 배열을 조회용으로 파싱하지 않는다.

두 revision 모두 snapshot full inventory 해시 검증과 public Adapter 검증을 통과했다. 조회 전후 public tree의 상대 경로별 SHA-256이 동일했고, campaign 객체와 초기 observation의 `released_at == public.as_of`도 유지됐다. 스냅샷 파일은 수정하거나 덮어쓰지 않았다.

## 5. 전체 회귀와 남은 경계

전체 회귀 명령과 결과:

```text
conda run -n drug python -m pytest -q
165 passed in 0.99s
```

이번 단계에서 구현하지 않은 것은 승인·예산·DB·실행 이력·중복 실행 방지·학습/선택 루프·Observation/EvidenceRef 생성과 공개·released_at 할당·RunState 교체·API/agent-tool 노출·OS 또는 컨테이너 수준 접근 격리다. manifest SHA-256은 등록된 파일의 변경 탐지이지 manifest 자체의 서명이나 악의적 manifest 교체에 대한 인증이 아니다. 다음 실행기는 승인·예산 검증 후 `ReplayOracle.lookup(candidate_id, assay_id)`를 내부 호출하고, 공개 전에 새 객체를 검증한 뒤 교체해야 한다.
