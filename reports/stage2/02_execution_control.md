# Stage 2B 구현·검증 보고서: 승인·예산 예약·실행 이력

> 이 보고서는 Stage 2B 검증 시점의 기록이며, 당시에는 2-C가 구현되지 않았다. 현재 2-C 구현과 검증 결과는 [03_result_release.md](03_result_release.md)를 참고한다.

실행일: 2026-09-28  
실행 환경: conda `drug`  
범위: Stage 2A의 고정 snapshot `ReplayOracle`을 신뢰된 Python 실행기 안에서 명시적으로 승인하고, Decimal 비용을 예약한 뒤 조회·비공개 결과·실행 이력을 원자적으로 보존한다. Observation/EvidenceRef 공개, 최종 과금 확정, OS/container 격리와 2-C는 구현하지 않았다.

## 1. 선행 계약과 검증

구현 전 확인한 Stage 2A 인터페이스는 다음과 같으며 실행기에서도 그대로 사용한다.

```python
store = load_replay_store(snapshot_root, initial_public_campaign)
oracle = ReplayOracle(store)
result = oracle.lookup(candidate_id, assay_id)
```

snapshot/store/oracle은 실행기 생성 전에 한 번 검증·적재한다. 실행 요청마다 전체 snapshot을 다시 해시하거나 store를 새로 만들지 않는다. 실행기는 oracle store의 snapshot ID·campaign ID뿐 아니라 초기 `PublicCampaign.model_dump_json()` 직렬화 결과의 SHA-256도 확인해 run을 고정한다.

2-A에서 요구한 경계도 현재 코드의 테스트로 확인했다.

- `test_result_type_rejects_status_measurement_count_mismatch`: `records_found` + 빈 측정과 `no_record` + 측정이 모두 거절된다.
- `test_counter_replay_preserves_active_inconclusive_missing_and_inequality`: counter Active/Inactive, Inconclusive, 결측 판정과 지원되는 부등호 comparator가 조회 결과에서 유지된다. Active를 성공으로 재계산하지 않는다.
- `test_public_copy_loads_without_curator_files`: public bundle은 curator 파일 없이 로드된다.
- `test_runtime_database_is_private_and_public_loader_ignores_it`: runtime DB mode `0600`과 public loader의 DB 비의존성을 확인하고, 공개 파일과 내용이 바뀌지 않음을 검사한다.

Stage 1 선행 테스트도 전체 회귀 실행에 포함됐다. 관련 항목은 `test_pubchem_no_followup_builds_public_bundle_and_reports_missing`, `test_pubchem_missing_followup_file_is_not_treated_as_zero_coverage`, `test_counter_assay_active_meaning_survives_build_serialization_and_load`, `test_real_pubchem_snapshot_configuration_is_versioned_and_explicit`, `test_primary_observations_have_minimal_public_raw_traces`, `test_adapter_rejects_unsupported_public_manifest_version`다. 이전 2-A 보고서 `01_replay_lookup.md`는 당시 결과를 보존했으며, 이번 보고서가 현재 검증을 별도로 기록한다.

## 2. 구현

`ExecutionCoordinator`는 다음 실제 계약을 제공한다.

```python
ExecutionCoordinator(
    database_path: str | Path,
    public: PublicCampaign,
    oracle: ReplayLookup | ReplayOracle,
    *,
    cost_policy_version: str,
    clock: Callable[[], datetime],
    busy_timeout_seconds: float = 5.0,
)

initialize_run(run_id: str, initial_budget: Cost) -> RunBudget
approve_action(run_id: str, action: ActionRequest, *, approver_id: str, reason: str) -> GovernanceDecision
reject_action(run_id: str, action: ActionRequest, *, approver_id: str, reason: str) -> GovernanceDecision
execute(run_id: str, request_id: str, action: ActionRequest) -> ExecutionReceiptView
read_private_result(run_id: str, execution_id: str) -> ReplayLookupResult
```

행동은 공개 candidate와 지원되는 follow-up assay의 고정 replay 조회 하나다. 비용은 요청 payload에서 받지 않고 초기 공개 assay 설정에서 취하며, 비용 정책 버전도 run에 고정한다. 정확한 `Decimal` 문자열로 예산을 보존하고 다음 관계를 매 변경에서 검사한다.

```text
available = initial_budget - spent - reserved
initial_budget >= 0, spent >= 0, reserved >= 0, available >= 0
```

신뢰된 호출자가 `approve_action`을 호출해야 승인된다. 승인 digest는 run·snapshot·campaign·candidate·assay·action kind·빈 parameters·비용·단위·정책 버전에 묶인다. 거절·미승인·취소·잘못된 후보/시험·primary 조회·선행조건 불일치·부족 예산은 Oracle 호출과 예약 전에 거절된다.

SQLite `BEGIN IMMEDIATE` transaction에서 동일 run의 request/action key와 예산을 직렬화한다. 예약, Oracle 결과, execution row, private result와 ledger를 함께 commit한다. `records_found`는 `ready_for_release`로 저장하고 비용을 reserve 상태로 유지한다. `no_record`는 distinct 상태로 저장하고 예약을 해제한다. 알려진 replay 오류는 오류 코드만 담은 `failed`로 기록하고 예약을 해제한다. DB 쓰기 또는 예상 밖 예외는 transaction을 rollback한다. 성공 조회만으로 `spent`를 증가시키는 settlement API는 없다.

request ID가 같은 payload로 재전송되면 동일 receipt를 반환하고, 다른 payload면 conflict다. 새 request ID여도 같은 run·snapshot·candidate·assay 행동이면 기존 실행에 매핑해 Oracle 재호출을 막는다. 다른 run은 독립적이다. 승인, 예산, terminal 결과와 private result는 SQLite를 다시 열어 복원한다. 실행 DB 파일 mode는 `0600`이다. 이는 신뢰된 Python 내부 경계이며 인증 또는 OS/container 파일 접근 격리를 구현한 것은 아니다.

결과 공개 경계에 재사용할 기존 validator 시그니처는 다음과 같다.

```python
validate_execution(
    action: ActionRequest,
    receipt: ExecutionReceipt,
    result: ExecutionResult,
    public: PublicCampaign,
    *,
    as_of: datetime,
) -> AuditResult
```

2-B에는 공개 `ExecutionReceipt`, `ExecutionResult`와 새 `Observation`이 없으므로 이를 호출하지 않는다. 결과는 `read_private_result(run_id, execution_id)`에서만 2-C의 신뢰된 내부 호출자에게 넘긴다.

## 3. Fixture 테스트

전체 회귀 명령:

```text
conda run -n drug python -m pytest -q
196 passed in 2.17s
```

실행기 주요 테스트와 검사 내용:

| 테스트 | 확인 내용 |
| --- | --- |
| `test_approval_is_explicit_and_denial_or_cancel_never_calls_oracle` | 명시적 승인 필요, 거절·취소 때 Oracle 호출과 예약 없음 |
| `test_cost_authority_cannot_be_supplied_by_request_or_changed_after_approval` | 요청자 비용 무시, 승인 후 비용 변조 거절 |
| `test_initial_public_prerequisite_is_rechecked_before_reservation` | 실행 직전 초기 public 선행조건 재검사 |
| `test_records_found_reserves_one_cost_and_keeps_all_measurements_private` | 한 행동 비용만 reserve, `spent=0`, 결과 비공개 보존 |
| `test_no_record_releases_reservation_without_fabricating_observation` | 예약 전액 해제, 부재 상태 보존, 가짜 Observation 없음 |
| `test_known_lookup_error_is_failed_not_no_record_and_releases_cost` | 알려진 오류를 `failed`로 구분하고 예약 해제 |
| `test_zero_cost_exact_budget_and_decimal_string_roundtrip` | 0 비용·정확히 일치하는 예산·Decimal 문자열 보존 |
| `test_request_idempotency_conflict_and_action_deduplication` | 동일 request 재사용, 변경 payload conflict, 새 request의 같은 행동 중복 방지 |
| `test_database_reopen_restores_approval_execution_and_private_result` | DB 재개방 뒤 승인·실행·결과 복원 |
| `test_unexpected_oracle_exception_rolls_back_and_same_request_can_retry` | 예상 밖 Oracle 오류에서 부분 상태 없이 rollback |
| `test_private_result_write_failure_rolls_back_reservation_and_ledger` | private result 저장 실패에서 예약·ledger rollback |
| `test_two_connections_submit_same_action_once` | 동시 같은 행동을 한 번만 실행 |
| `test_concurrent_actions_compete_against_reserved_decimal_budget` | 동시 예산 경쟁에서도 예약액 불변식 유지 |

이 항목들은 snapshot fixture를 사용하는 자동화 테스트다. 실제 PubChem 측정값을 테스트 fixture로 가장하지 않는다.

## 4. 보존된 실제 snapshot 검증

기존 2-A snapshot 검증:

```text
conda run -n drug python scripts/verify_replay_snapshots.py
```

| snapshot | 후보 | candidate×assay 조회 상태 | hidden 후속 행 | 추가 확인 |
| --- | ---: | --- | ---: | --- |
| `revision-20260917-r2` | 5 | records_found 1 / no_record 4 | 1 | 등록 해시, 초기 released_at, public hash 유지 |
| `revision-20260918-primary-active-all` | 1,682 | records_found 295 / no_record 1,387 | 295 | normalized 328,519행 중 hidden 부분집합 295행 전체 일치, public hash 유지 |

2-B 실제 경로 검증:

```text
conda run -n drug python scripts/verify_execution_snapshots.py
```

두 snapshot 각각에서 초기 public 선행조건을 만족하는 기록 존재 1건과 부재 1건을 선택해 임시 private runtime DB에서 명시 승인·실행했다. 각 snapshot의 기록 존재는 `ready_for_release`, 부재는 `no_record`였고, 동일 request 재전송 및 DB 재개방 뒤 같은 execution이 반환됐다. retry/reopen은 Oracle을 다시 부르지 않았다. 기록 존재 결과의 private `ReplayLookupResult`는 실행 직전 Oracle 결과와 모든 measurement 필드가 일치했다. 부재는 별도 private 상태로 저장됐고 ready-result reader를 통해 읽히지 않았다.

검증 예산과 행동 비용은 각 snapshot config의 assay 비용 **1 설정 단위**를 사용했고 `assumed=true`였다. 이는 예제 실행의 제한 예산일 뿐 실제 실험 견적이 아니다. 각 검증 run에서 `ready_for_release`는 해당 비용을 reserve하고 spent를 0으로 유지했으며, `no_record`는 reserve를 해제했다. 원시 측정값은 verifier 출력과 보고서에 포함하지 않았다.

각 snapshot 실행 전후 `bundle/public/` 파일별 SHA-256을 비교했고, 로드된 `PublicCampaign` 및 초기 Observation도 비교했다. 모두 동일했다. 기존 r2·확장 revision 파일은 수정하거나 덮어쓰지 않았으며 runtime DB는 임시 디렉터리에 만들었다.

## 5. 변경 파일

- `src/assaypilot/execution.py`: 2-B coordinator, 승인·예산·transaction·중복 처리·private handoff.
- `src/assaypilot/replay.py`: execution run이 초기 `PublicCampaign.model_dump_json()` 직렬화 결과의 SHA-256 및 snapshot store에 묶이도록 public digest 보존.
- `tests/test_execution.py`: 승인, 비용, 예산, 오류, 원자성, 중복, 동시성, 복원과 private/public 분리 테스트.
- `tests/test_replay.py`: 결과 상태/측정 모순 및 counter 판정 의미 보존 fixture 검사.
- `scripts/verify_execution_snapshots.py`: 두 보존 snapshot의 실제 2-B private runtime 실행 검증.
- `docs/stage2_execution_control.md`: 함수 계약·예시·정책·2-C 연결 문서.
- `docs/stage2_replay_lookup.md`, `docs/stage2_data_handoff.md`: 현재 2-A/2-B 구현과 남은 2-C 경계를 반영.
- `reports/stage2/02_execution_control.md`: 이 구현 및 검증 기록.

## 6. 남은 사항과 2-C 시작점

2-C는 `ExecutionCoordinator.read_private_result(run_id, execution_id)`로 `ready_for_release` 결과만 읽는다. 새 Observation과 EvidenceRef를 만들고 원본 근거 payload·hash·candidate·assay 참조를 검사하며, 현재 공개 시점을 `validate_execution(..., as_of=...)`에 전달한다. 전체 새 PublicCampaign/RunState 검증이 성공한 뒤 관측 공개와 함께 해당 execution의 `reserved`를 정확히 한 번 `spent`로 확정해야 한다.

공개 검증 실패는 일부 Observation 또는 일부 과금이 남지 않도록 원자 처리한다. 일시 실패는 같은 execution과 reserve를 유지해 복구하고, 정책에 정의된 명시적 영구 취소 때만 예약을 해제한다. `no_record`는 snapshot 안의 연결 행 부재이고 `failed`는 조회 오류이므로 어느 것도 공개 실험 판정으로 변환하지 않는다.

아직 하지 않은 작업은 Observation/EvidenceRef 공개·`released_at` 할당·settlement·현재 공개 상태를 반영한 선행조건 검증·RunState의 새 객체 검증 후 교체·인증과 OS/container 격리·외부 실험·학습·자동 선택·UI/API 서버다. 따라서 2-B의 결과는 승인되어 실행되고 비공개로 저장된 상태이지 agent 공개나 생물학적 성공 판정이 아니다.
