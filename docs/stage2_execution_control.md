# Stage 2B: 승인·예산 예약·실행 이력

Stage 2B는 검증된 Stage 1 snapshot에서 공개 후보와 후속 시험 한 쌍을 조회하는 실행 제어 계층이다. 신뢰된 Python 호출자가 명시적으로 승인한 요청만 이미 적재된 `ReplayOracle`에 전달한다. 결과는 비공개 SQLite runtime DB에 저장한다. `ready_for_release`는 2-C 공개 대기 상태이고, 성공 공개 후 실행 상태는 `released`, 명시적 공개 취소 후에는 `cancelled`가 된다. 어느 상태도 assay active를 실험 성공이나 임상 효과로 해석하지 않는다. 공개·settlement 계약은 [stage2_result_release.md](stage2_result_release.md)에 기록했다.

## 기존 계약과 적용 시점

기존 도메인 계약을 그대로 사용한다.

```python
ActionRequest(
    action_id: ID,
    campaign_id: ID,
    candidate_id: ID,
    assay_id: ID,
    parameters: dict[str, JsonValue] = {},
)
ApprovedAction(
    action: ActionRequest,
    approval_id: ID,
    reviewed_at: AwareDatetime,
    reason: Text,
)
GovernanceDecision(
    action_id: ID,
    decision: Literal["approved", "rejected"],
    reason: Text,
    approved_action: ApprovedAction | None = None,
)
Cost(amount: Money, unit: Text, assumed: bool)
```

`Money`는 `Decimal`, decimal 문자열 또는 정수만 받고, 음수·비유한 수·`bool`·float를 거절한다. `BudgetState`는 `total`, `spent`, `reserved`, `unit`을 보존하며 `available`은 `total - spent - reserved`로 계산한다.

공개 후 결과를 검사하는 기존 함수의 정확한 계약은 다음과 같다.

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

이 검사는 공개용 `Observation`과 실행 receipt/result의 연결, 현재 공개 기준 시점, 후보·시험·근거 참조를 검사한다. Stage 2B 실행 중에는 비공개 `ReplayLookupResult`를 공개 receipt/result로 가장하지 않는다. 2-C `release_result`가 새 공개 객체와 근거를 만든 뒤 현재 공개 시각을 `as_of`로 전달해 호출한다.

## 실제 함수 계약과 호출 예

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
execute(run_id: str, request_id: str, action: ActionRequest) -> ExecutionReceiptView
read_private_result(run_id: str, execution_id: str) -> ReplayLookupResult
release_result(run_id: str, execution_id: str) -> PublishedExecution
cancel_pending_release(run_id: str, execution_id: str, reason: str) -> ExecutionReceiptView
get_current_budget(run_id: str) -> BudgetState
get_public_state(run_id: str) -> PublicRunView
```

아래 예시는 보존된 확장 snapshot에서 초기 공개 선행조건을 만족하는 후보 하나를 고르고, 해당 assay에 명시적으로 승인한 뒤 한 행동을 실행하는 흐름이다. SQLite 파일은 bundle 밖의 전용 private runtime 디렉터리에 둔다. 예산은 이 예시에서 한 건의 snapshot 설정 assay 비용과 같게 설정했다. 이 값의 `assumed` 여부를 유지하며 실제 실험 견적이라고 해석하지 않는다.

```python
from datetime import datetime, timezone
from pathlib import Path

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.domain import ActionRequest, Cost, DataSource, Verdict
from assaypilot.execution import ExecutionCoordinator
from assaypilot.replay import ReplayOracle, load_replay_store

root = Path("data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all")
public = PublicBundleAdapter().load(DataSource(
    kind="public_bundle", location=str(root / "bundle/public/manifest.json")
))
oracle = ReplayOracle(load_replay_store(root, public))
assay = next(item for item in public.assays if item.assay_id == "mep2-confirmatory")
candidate_id = next(
    observation.candidate_id for observation in public.observations
    if observation.assay_id == "mep2-primary" and observation.verdict is Verdict.ACTIVE
)
action = ActionRequest(
    action_id="bounded-followup-check",
    campaign_id=public.campaign.campaign_id,
    candidate_id=candidate_id,
    assay_id=assay.assay_id,
)

coordinator = ExecutionCoordinator(
    Path("private-runtime/execution.sqlite"), public, oracle,
    cost_policy_version="public-assay-cost-v1",
    clock=lambda: datetime.now(timezone.utc),
)
one_action_budget = Cost(
    amount=assay.cost.amount, unit=assay.cost.unit, assumed=assay.cost.assumed
)
run = coordinator.initialize_run("example-run-2b", one_action_budget)
decision = coordinator.approve_action(
    run.run_id, action, approver_id="trusted-caller",
    reason="explicit review for one bounded replay lookup",
)
receipt = coordinator.execute(run.run_id, "request-2b-001", action)
if receipt.status == "ready_for_release":
    private_result = coordinator.read_private_result(run.run_id, receipt.execution_id)
    # Pass private_result to the trusted 2-C boundary; do not expose it here.
elif receipt.status == "no_record":
    # This means no linked row in this snapshot, not an inactive result.
    pass
```

초기 로딩은 `load_replay_store(snapshot_root, public)`로 snapshot당 한 번 수행하고 `ReplayOracle`을 실행 중 재사용한다. `ExecutionCoordinator` 생성 시 store의 snapshot ID, campaign ID 및 초기 `PublicCampaign.model_dump_json()` 직렬화 SHA-256 일치를 다시 확인한다. 위 예시의 candidate는 공개 `Observation`에서 선택하며 SID/CID 또는 candidate ID 문자열을 파싱하지 않는다.

## 승인·행동·비용 규칙

- 한 행동은 고정 snapshot에 있는 `(candidate_id, follow-up assay_id)` 조회 하나다. 모든 연결 측정 행이 한 결과에 포함된다. primary 조회, 임의 assay, 자유 형식 parameter, 임의 경로와 신규 실험은 거절한다.
- 비용은 요청에서 받지 않는다. snapshot의 공개 `AssaySpec.cost`와 실행기 초기화 때 고정한 `cost_policy_version`을 사용한다. 비용 단위가 campaign 예산 단위와 달라지면 거절한다. snapshot 비용이 `assumed=True`라면 실제 실험비로 표현하지 않는다.
- 승인 발급은 `approve_action(..., approver_id=..., reason=...)` 호출로만 이뤄진다. `ActionRequest`에 `approved=True`를 넣을 수 없으며, 요청자가 보낸 승인 유사 필드는 권한이 아니다. 테스트용 approver 이름은 인증 시스템을 의미하지 않는다.
- 승인 digest는 run/snapshot/campaign/candidate/assay, 고정 action kind, 빈 parameters, Decimal 금액 문자열, 단위와 정책 버전을 묶는다. 비용이나 대상이 바뀌면 기존 승인을 쓸 수 없다. 승인 자체는 예산을 예약하지 않는다.
- 도메인 `ApprovedAction`에는 만료 필드가 없으므로 Stage 2B는 임의 만료 규칙을 추가하지 않는다. `cancel_approval`은 실행 전 승인을 취소하며, 이미 terminal인 실행은 바꾸지 않는다. 승인·실행 시각은 timezone-aware여야 하고 실행 시각이 승인 시각보다 앞서면 거절한다.
- 승인 시와 실행 직전에 초기 공개 관측 및 해당 run에서 공개 commit된 runtime 관측으로 선행조건을 확인한다. 비공개 replay 결과나 다른 run의 관측은 조건을 만족시키지 않는다. 실행 시 검사한 public `state_version`을 execution에 저장한다.

## 예산, 결과 상태, 오류

모든 금액은 `Decimal`로 계산하고 DB에는 문자열로 기록한다.

```text
available = initial_budget - spent - reserved
initial_budget >= 0, spent >= 0, reserved >= 0, available >= 0
```

실행은 `BEGIN IMMEDIATE` transaction 안에서 예약하고, 이미 메모리에 적재된 read-only Oracle을 호출한 다음 이력·결과·ledger를 함께 commit한다.

| Oracle 결과 | receipt 상태 | reserved/spent | private 결과 |
| --- | --- | --- | --- |
| `records_found` | `ready_for_release` | assay 비용 유지 / `spent` 증가 없음 | 모든 연결 측정을 보존 |
| `no_record` | `no_record` | 예약 전액 해제 / `spent` 증가 없음 | 빈 측정과 부재 상태를 내부 저장 |
| 알려진 `ReplayError` | `failed` | 예약 전액 해제 / `spent` 증가 없음 | 안전한 오류 코드만 보존 |
| DB 또는 예상 밖 예외 | receipt 없음, transaction rollback | 예약·ledger·결과 모두 rollback | 실행 기록 없음 |

실행 receipt는 `execution_id`, `request_id`, `action_id`, 상태, 그 실행 직후의 run 예약·가용액, 단위 및 timezone-aware 시각만 포함한다. 원본 측정, verdict, 전체 coverage와 curator 경로는 포함하지 않는다. `read_private_result`는 해당 run/snapshot/candidate/assay가 맞고 상태가 `ready_for_release`인 결과만 `ReplayLookupResult`로 다시 검증해 방어적 복사본을 돌려준다. `no_record`와 `failed` 결과는 이 handoff reader로 읽을 수 없다.

알려진 lookup 오류는 `failed`로 기록하지만, DB 쓰기 오류와 예상 밖 예외는 성공적인 부재로 바꾸지 않고 전체 transaction을 취소한다. 일반 receipt에는 내부 원문 오류 문자열을 넣지 않는다. Stage 2C 시점의 SQLite `user_version=2`는 v1의 approval/reservation/execution/private result를 보존하면서 공개 상태·근거·settlement 테이블을 transaction migration으로 추가했다. Stage 3-A는 이 schema를 v3으로 확장해 loop run/step 기록을 추가한다. 현재 migration과 지원 버전은 [Stage 3-A 실행 루프 문서](stage3_run_loop.md#진행-기록-migration-및-재시작)에 적었다. 미지원 미래 버전은 거절한다. DB 및 sidecar 파일은 private runtime 디렉터리에 보관하고 mode `0600`으로 제한한다. 이 mode만으로 동일 권한 프로세스를 격리한다고 보지 않는다. 공개 Adapter는 runtime DB를 입력으로 사용하지 않는다.

## 중복·재시작 정책

- `(run, request_id)`는 동일 payload에서 같은 실행 receipt를 반환한다. 같은 request ID를 다른 action으로 재사용하면 `IdempotencyConflictError`다.
- 다른 request ID로 같은 `(run, snapshot, candidate, assay, action kind)`를 보내면 최초 실행에 매핑된 receipt를 돌려준다. 추가 승인·예약·Oracle 호출은 없다. `action_id`는 기록 label이므로 중복 action identity에는 포함하지 않는다.
- `ready_for_release`, `no_record`, `failed`는 terminal이다. 새로운 request ID를 주어 terminal failed/no_record를 다시 조회하거나 신규 반복 행동을 만들 수 없다. 다른 run은 독립 action 및 예산이다.
- SQLite unique key와 `BEGIN IMMEDIATE`가 동일 action 및 예산 변경을 직렬화한다. 잠금 대기는 유한 timeout을 쓴다. 승인·run·terminal result·private result는 DB를 재열어 복구한다.
- 보장하는 것은 저장된 결과와 예산 효과의 중복 방지다. 프로세스가 Oracle 호출 뒤 transaction commit 전에 중단되면 read-only lookup을 재호출할 수 있다. Oracle 함수 호출 자체의 장애 시 정확히 한 번 실행을 보장하지 않는다. 실제 외부 실험을 넣는다면 durable outbox와 작업 복구 정책이 별도로 필요하다.

## 2-C 공개 이후 상태

`ready_for_release` 결과는 `release_result(run_id, execution_id)`에서 원래 실행과 승인·비용을 다시 확인한 뒤 공개한다. 새 객체 검증, Observation별 evidence 보존, DB 원자 공개와 settlement, `cancel_pending_release`, current-state getter 및 제한된 `PublicReader`는 [stage2_result_release.md](stage2_result_release.md)에 기술했다. 실행기는 trusted Python boundary이고 로그인·웹 서비스는 제공하지 않는다. 실제 local namespace/chroot 접근 테스트와 전체 Stage 2C 검증 결과는 [03_result_release.md](../reports/stage2/03_result_release.md)를 참고한다.
