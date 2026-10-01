# Stage 2C: 공개 결과·근거·과금 확정

Stage 2C는 `ready_for_release`인 2-B 실행 결과를 공개 Observation과 EvidenceRef로 검증해 등록하고, 같은 SQLite transaction에서 실행 상태·현재 RunState·예산 settlement를 확정한다. 초기 Stage 1 bundle 파일은 변경하지 않는다. 실행별 runtime 공개 상태는 private runtime DB가 권위 원본이고, 에이전트 조회는 제한된 run-bound reader를 거친다.

## 실제 coordinator 계약

```python
ExecutionCoordinator(database_path, public, oracle,
                     cost_policy_version=..., clock=..., busy_timeout_seconds=5.0)
initialize_run(run_id, initial_budget) -> RunBudget
approve_action(run_id, action, *, approver_id, reason) -> GovernanceDecision
execute(run_id, request_id, action) -> ExecutionReceiptView
read_private_result(run_id, execution_id) -> ReplayLookupResult
release_result(run_id, execution_id) -> PublishedExecution
cancel_pending_release(run_id, execution_id, reason) -> ExecutionReceiptView
get_current_budget(run_id) -> BudgetState
get_public_state(run_id) -> PublicRunView
get_public_execution(run_id, execution_id) -> PublishedExecution
get_public_evidence(run_id, evidence_id) -> PublicEvidence
public_reader(run_id) -> PublicReader
```

`ExecutionReceiptView`는 2-B 실행 이력이며 domain `ExecutionReceipt`와 다르다. 공개 시 coordinator는 DB에서 최초 저장된 ActionRequest, approval, 실행 시각 및 예약 비용을 다시 읽는다. 중복 제출한 뒤의 action label을 최초 공개 결과에 덮어쓰지 않는다.

```python
decision = coordinator.approve_action(
    run_id, action, approver_id="trusted-caller", reason="reviewed replay request"
)
receipt = coordinator.execute(run_id, "request-001", action)
if receipt.status == "ready_for_release":
    published = coordinator.release_result(run_id, receipt.execution_id)
    current = coordinator.get_public_state(run_id)
    budget = coordinator.get_current_budget(run_id)
```

비용은 입력 요청에서 받지 않고 고정 public assay 설정에서 결정된다.

## 관측, 근거, 공개 시점

`ReplayLookupResult.measurements`의 각 `measurement_id`는 별도 Observation과 별도 EvidenceRef로 변환된다. 행을 평균·다수결·최초 행으로 축약하지 않는다. verdict가 상충해도 각 행을 보존한다. 0, 결측, 값·단위·비교 연산자, raw outcome 및 `not_reported` 반복/조건 표식을 유지한다. 공백 `Activity Outcome`은 정규화 계약에 맞춰 raw verdict `None`으로 비교하되 근거 payload의 원본 행에는 공백 문자열을 보존한다. Activity Outcome으로부터 IC50/EC50 같은 수치를 만들지 않는다.

Observation/Evidence ID는 run, 최초 execution, measurement ID를 입력으로 만든 안정 ID다. 후속 Observation의 `released_at`은 공개 transaction에 주입된 timezone-aware clock 시각이다. 원본 실험 날짜로 대체하지 않는다. 초기 PublicCampaign `as_of`와 Stage 1 bundle은 그대로 둔다.

새 상태는 transaction 안에서 만들어 검사한다. `validate_execution(action, receipt, result, current_public, as_of=published_at)`가 새 evidence 참조와 공개 시점을 검사한다. 이어 `validate_run_state(..., expected_budget_total=run_initial_budget)`가 현재 budget 및 전체 관측 참조를 검사한다. 기존 객체에 필드를 직접 대입하지 않으며 검증 실패 시 임시 객체를 버리고 DB transaction을 rollback한다.

공개 근거 payload에는 `measurement_id`, source row 식별자와 행 번호, 원본 파일 SHA-256, candidate ID, SID/AID/CID, raw outcome, 공식 protocol 위치, 허용 목록의 raw row 필드만 넣는다. 허용 raw 필드는 `AID`, `SID`, `CID`, 설정된 outcome 열, `Activity Name`, 설정된 endpoint 열이다. 임의의 private 열, 다른 행, curator 파일, coverage, 내부 경로와 DB 경로는 넣지 않는다. `source_file_sha256`은 upstream 파일 식별값이고 EvidenceRef payload의 SHA-256과 다르다.

초기 evidence payload는 검증된 ReplayStore에 적재된다. runtime evidence는 `published_evidence` 테이블에 canonical JSON 문자열과 실제 payload SHA-256으로 보존된다. runtime logical location 형식은 `runtime/<run-key>/<evidence-id>.json`이다. `get_public_evidence(run_id, evidence_id)`는 이 run에 속한 ID만 해석하며 location을 파일 경로로 열지 않는다. Observation의 `evidence_ids`, EvidenceRef ID/location, payload 내부 ID 및 반환 SHA-256을 서로 검사한다.

## 현재 상태와 선행조건

`PublicRunView`는 고정된 초기 PublicCampaign, 해당 run에서 성공 공개된 추가 Observation/EvidenceRef, 현재 state version/as_of 및 `RunState`를 합성한다. 이 합성 조회는 초기 bundle이나 원본 객체를 변경하지 않는다. run마다 초기 예산이 다를 수 있어 runtime validator에는 DB에 고정된 초기 run budget을 명시적으로 전달한다.

승인 및 실행 직전 선행조건 검사는 해당 run의 초기 관측과 DB에 commit된 runtime 공개 관측만 사용한다. private `ready_for_release` 결과, 아직 공개 transaction을 통과하지 않은 관측, 다른 run의 관측은 조건을 만족시키지 않는다. 실행 이력에는 검사 시점의 `prerequisite_state_version`을 저장한다. fixture 테스트는 후속 결과가 private일 때 다음 assay를 거절하고 공개된 뒤에만 허용되는 흐름을 검사한다.

## 공개 transaction과 비용

성공 공개는 `BEGIN IMMEDIATE` transaction 안에서 execution과 run binding을 다시 확인하고, private result·원래 action/approval·reservation을 검증한다. 새 Observation/EvidenceRef와 DTO를 만든 뒤 validator, state version, budget invariant를 검사하고 다음을 함께 저장한다.

- `published_evidence`, `published_executions`
- runtime `observations_json`, `state_version`, 현재 `as_of`
- execution의 `release_status='released'`
- 해당 execution 비용의 reserved 감소, spent 증가
- execution당 고유 `release_settlements` 한 건

총 예산과 available은 settlement 전후 동일하고 비용만 reserved에서 spent로 이동한다. 여러 Observation이 한 execution에 연결돼도 한 번만 과금한다. 0 비용 공개도 settlement·state transition을 기록한다. 이미 공개된 execution 재요청은 최초 timestamp/ID/결과를 반환한다. validator·증거·ledger·commit 실패는 전부 rollback해 ready 결과와 예약을 남긴다.

`cancel_pending_release(run_id, execution_id, reason)`은 `ready_for_release`에서만 명시적으로 취소한다. 해당 execution 예약만 한 번 풀고 관측, 공개 evidence, settlement 또는 spent를 만들지 않는다. 중복 취소는 같은 terminal receipt를 반환하며 이미 released인 결과는 취소할 수 없다. reason/취소 시각은 private runtime DB에 보관한다. `cancel_approval`은 실행 전 승인 취소이고 이 기능과 구별된다.

## SQLite 마이그레이션

Stage 2C 당시 private runtime SQLite의 `PRAGMA user_version`은 `2`였다. 그 migration은 기존 v1 실행 테이블을 지우지 않고 `BEGIN IMMEDIATE` 안에서 execution의 release/version 열을 추가하고 `run_public_state`, `published_evidence`, `published_executions`, `release_settlements`를 생성했다. 기존 run마다 초기 state row를 만들고 v1 approval, reservation, execution, private result와 ledger를 보존했다. v0 신규 DB도 이 stage의 schema로 생성했다. Stage 3-A는 여기에 `loop_runs`와 `loop_steps`를 추가해 현재 schema를 v3으로 올린다. v1/v2 데이터 보존과 미지원 미래 버전 거절은 [Stage 3-A 실행 루프 문서](stage3_run_loop.md#진행-기록-migration-및-재시작)와 회귀 테스트에서 다룬다. 이 문서의 아래 migration 동작은 Stage 2C 시점의 기록이다.

DB와 `-wal`/`-shm`은 private runtime 디렉터리에 둔다. DB 파일 mode `0600`만으로 동일 OS 권한 주체를 격리했다고 보지 않는다.

## 제한된 공개 조회 채널과 파일 격리

trusted 코드가 `reader = coordinator.public_reader(run_id)`를 만들면 reader는 해당 run에 고정된다. `serve_public_stdio(reader, source, sink)`의 JSON 요청은 다음 네 형태만 받는다.

```json
{"op":"state"}
{"op":"budget"}
{"op":"execution","execution_id":"..."}
{"op":"evidence","evidence_id":"..."}
```

요청은 최대 4 KiB, 응답은 최대 8 MiB다. run ID, 경로, 임의 method, 승인, Oracle, private result 읽기를 요청할 수 없다. 공개 전 execution/evidence와 다른 run의 기록은 generic `request_rejected`로 거절되며 내부 경로나 오류 원문은 반환하지 않는다. 응답 상한은 1,682 후보 확장 public state의 실제 1.60 MB JSON stdio 응답으로 확인했다.

실제 격리 테스트는 `unshare` user/mount/network namespace와 static BusyBox를 이용한다. 자식 reader를 제한된 read-only root 안에서 chroot 실행하고, trusted parent가 고정 reader를 통해 허용된 JSON 요청만 처리한다. 테스트는 성공 public state 조회와 임시 private canary, runtime DB, curator 파일, symlink 탈출, 상위 경로, 허용되지 않은 `shell` verb 접근 실패를 실행한다.

```bash
conda run -n drug pytest -q tests/test_execution.py::test_chrooted_public_reader_process_cannot_reach_private_files
```

이는 로컬 프로세스 격리 테스트다. 웹 서버, 인증 서비스, 컨테이너 배포 체계는 만들지 않았다.

## 검증 연결

- Fixture 및 회귀: `conda run -n drug pytest -q`
- 2-A 보존 snapshot: `conda run -n drug python scripts/verify_replay_snapshots.py`
- 2-B 실행 사례: `conda run -n drug python scripts/verify_execution_snapshots.py`
- 2-C 공개·settlement 사례: `conda run -n drug python scripts/verify_result_release_snapshots.py`
- 세부 결과와 테스트명: [03_result_release.md](../reports/stage2/03_result_release.md)

실제 r2와 확장 snapshot verifier는 기록 존재와 `no_record`를 별도 임시 run/DB에서 실행한다. 기록 존재에서는 원본 측정 수만큼 observation/evidence를 등록하고 한 번 정산하며 재시도·DB 재개방 결과가 동일한지 확인한다. `no_record`에서는 공개 관측과 settlement가 늘지 않는다. 양쪽 모두 초기 public campaign 직렬화 및 `bundle/public/` 파일 hash가 그대로인지 확인한다. private data 파일은 재생성하거나 덮어쓰지 않는다.
