# Stage 3-A: 공개 정보 기반 제한 실행 루프

Stage 3-A는 공개 campaign에서 실행 가능한 후속 replay 행동을 열거하고, 제한된 선택기를 호출한 뒤 trusted controller가 기존 승인·실행·공개 경로를 반복 연결한다. 이 구현은 연결 검증용 고정 순서 baseline이다. 학습 모델, 효능 예측, assay 성공 판정으로 해석하지 않는다. Stage 3-B의 다른 선택기 비교와 Stage 3-C의 정답 기반 성능 평가는 포함하지 않는다.

## 모듈과 계약

| 위치 | 책임 |
| --- | --- |
| `src/assaypilot/run_loop.py` | typed selector DTO, `Selector` 계약, 고정 순서 및 seeded priority 선택기, 격리 프로세스, durable controller, 승인·실행·공개 조정, 복구와 요약 |
| `src/assaypilot/selector_worker.py` | 표준 입출력 JSON 계약을 처리하는 표준 라이브러리 전용 두 baseline selector worker |
| `src/assaypilot/run_loop_cli.py` | `start`와 `resume` 명령, snapshot/Oracle 적재, JSON 요약 출력 |
| `src/assaypilot/execution.py` | 공개 상태와 trusted 실행 상태의 단일 SQLite 읽기 경계, 공개 선행조건, schema v3 migration |
| `scripts/verify_stage3_run_loop.py` | 보존된 r2 및 확장 snapshot을 사용하는 개발자용 실제 replay 검증 |

선택기는 `Selector.select(view: SelectorView) -> SelectorProposal | dict` 계약을 따른다. `SelectorView`는 후보, 공개 assay 설명, 이미 공개된 Observation, 현재 예산, 실행 가능한 `(candidate_id, assay_id)` 행동, 이 run에서 이미 시도한 행동의 제한 요약과 `view_digest`만 담는다. evidence payload, curator 객체, Oracle, coordinator, SQLite 연결, 파일 경로 및 시도하지 않은 행동의 측정 존재 여부는 들어가지 않는다. selector view schema는 `assaypilot.selector-view.v1`이다.

`SelectorProposal`은 두 형태만 허용한다.

- `select`: trusted controller가 제안한 공개 `candidate_id`, `assay_id`, 한 줄의 제한된 사유를 검증한다. 선택기는 `run_id`, ID 생성, 비용, 승인 상태를 정할 수 없다.
- `stop`: `selector_stop` 또는 `no_executable_actions`를 반환한다. 나머지 종료 사유는 trusted controller가 계산한다.

`FixedOrderSelector`는 실행 가능 행동을 `(candidate_id, assay_id)` 사전순으로 정렬해 첫 행동을 선택한다. `SeededRandomPrioritySelector`는 현재 eligible action의 식별자와 정수 seed로 SHA-256 priority를 계산하고 `(digest_bytes, candidate_id, assay_id)` 사전순의 첫 행동을 고른다. 격리 worker는 trusted 설정에 고정된 같은 선택기 하나만 실행한다. 출력 크기는 16 KiB, 입력은 2 MiB, 선택 이유는 240자로 제한한다. 두 기준선의 정확한 계약과 호환성은 [Stage 3 baseline selector 문서](stage3_baseline_selectors.md)에 있다.

## 공개 상태와 행동 열거

`ExecutionCoordinator.get_loop_snapshot(run_id)`는 한 SQLite 읽기 transaction에서 현재 공개 campaign/Observation/evidence 참조/예산/version과 coordinator 실행 상태를 읽는다. 공개 Reader는 공개된 execution만 반환한다. trusted 실행 상태 tuple은 controller에만 돌아가며 selector에 직접 전달하지 않는다. 별도로 공개 getter를 호출해 시점이 다른 예산과 관측을 섞지 않는다.

controller는 공개 후보와 snapshot에서 지원하는 공개 후속 assay를 조합한다. 선행조건은 `ExecutionCoordinator.prerequisites_satisfied`로 현재 공개 Observation에 대해 평가한다. 가용 예산에 맞는 행동만 selector DTO에 넣는다. 이때 Oracle에 질의해 hidden 결과가 존재하는 후보를 골라내지 않는다. `no_record`, `failed`, `cancelled`, `released` 등 같은 run에서 이미 처리된 행동은 다시 제안하지 않는다. 공개 전 `ready_for_release`는 선택 대상에서 제외하고 trusted 복구 대상으로 둔다.

새로 공개된 측정만 기존 공개 catalog에 추가해 격리 worker에 보낸다. 초기 후보·assay 설명·관측 catalog는 한 번 전송한다. 전체 evidence payload를 선택기 입력에 포함하지 않는다. 최초 public `state_version`과 selector view digest는 step과 함께 저장한다.

## Trusted 실행, 예산 및 오류

`RunLoopConfig`에는 run/snapshot ID, private runtime DB, 명시 예산과 단위, cost policy, `bounded_replay` 승인 정책, policy 주체 ID, selector 종류, 최대 step/duration, action/release retry 상한과 selector timeout을 기록한다. `seeded_random_priority`는 추가로 정수 `selector_seed`와 `random-priority-v1` `selector_algorithm_version`을 저장하고 fingerprint에 포함한다. 기존 `fixed_order` JSON에서 seed/version을 생략하던 저장 형식과 SHA-256은 유지한다. resume은 저장된 원본 JSON의 hash를 먼저 확인한 뒤 과거 fixed-order 설정을 구형 의미로 읽는다. selector 종류/seed/version이 resume 시점의 격리 worker와 다르면 시작을 거절한다. 다른 config binding 규칙은 바뀌지 않는다.

각 선택은 durable `loop_steps` 행과 trusted 생성 `action_id`/`request_id`를 먼저 만든 후 진행한다. controller는 매 행동에 `approve_action`을 호출하고 기존 `execute`에 저장된 같은 request ID를 전달한다. 결과가 `ready_for_release`면 동일 execution ID를 `release_result`에 전달한다. `no_record`는 해당 고정 snapshot에서 연결 행이 없다는 의미이며 inactive Observation을 만들지 않는다. `failed`도 Observation을 만들지 않는다. `records_found`는 공개 가능한 기록이 있다는 의미이며 assay의 성공 판정이 아니다.

approval 주체 ID는 오프라인 `bounded_replay` 정책을 나타내는 설정 문자열이다. 실제 사람의 검토, 인증 또는 wet-lab 지출 승인을 의미하지 않는다. 비용은 요청이 아니라 기존 공개 assay cost와 고정된 cost policy에서 가져온다. budget은 `Decimal`로 계산하고 실행 전 reserved, 공개 commit 때 spent로 정산한다.

일시적 실행 오류는 동일 durable step/request ID로 제한 재시도한다. 반복해도 같은 요청 ID와 coordinator 멱등성이 적용된다. selector protocol/schema/timeout 오류, 정책 거절, retry 소진은 제한된 code로 기록한다. 원시 예외·SQL·private measurement는 selector나 summary에 전달하지 않는다.

## 진행 기록, migration 및 재시작

private runtime SQLite의 현재 `PRAGMA user_version`은 `3`이다. Stage 3-A migration은 `loop_runs`와 `loop_steps`를 추가하며 기존 approval, reservation, execution, result, 공개 evidence, settlement 자료를 교체하거나 삭제하지 않는다. v0 신규 DB와 v1/v2 기존 DB를 v3으로 연다. 더 높은 미지원 버전은 `unsupported_database_version`으로 거절한다.

- `loop_runs`: canonical 설정과 SHA-256, 최초 시작 시각, 최초 UTC deadline, 상태, 종료 이유, 재개 여부, selector 호출 수
- `loop_steps`: step 번호, trusted action/request ID, 후보/assay, 공개 view version/digest, 선택 이유, 승인·실행·공개 상태, execution ID, retry 수, 관측 증가 수, 예산 checkpoint, 안전한 error code

SQLite coordinator 상태가 권위 원본이다. 제안 저장 직후, 승인 직후, 실행 commit 직후, 공개 commit 직후 loop checkpoint 전 중단을 같은 durable ID로 복구한다. 공개가 이미 commit됐으면 저장된 published execution을 읽고 다시 공개·settle하지 않는다. 공개 retry를 소진해도 reservation을 취소하지 않고 pending release로 재개 가능하게 둔다. 명시적 취소는 기존 `cancel_pending_release` API의 책임이다.

최대 시간은 최초 UTC deadline을 저장하고 resume에서 연장하지 않는다. 각 controller 호출은 남은 시간으로 monotonic deadline을 만들어 프로세스 내 시계 역행에도 한도를 적용한다. deadline 전 durable 선택이 없는 새 행동은 시작하지 않는다. deadline 중 미완료 step이 아직 실행되지 않았다면 미사용 승인을 취소하고 `deadline_expired_before_execution`으로 step을 기록한다. 이미 coordinator 실행이 commit됐다면 멱등성으로 기존 실행을 조정한다. pending 공개는 설정된 유한 `max_release_retries`만큼 마무리를 시도한다. deadline 동안 selector 실행으로 시간을 넘긴 경우 새 step은 기록하지 않는다. stop된 run의 중단 시간도 최초 deadline 계산에 포함된다.

동일 run의 단일 worker만 허용한다. runtime DB 옆 `.run-loop-locks/`의 run ID hash 파일에 OS `flock`을 잡으며 프로세스 종료 때 자동 해제된다. 다른 run은 독립 lock을 갖는다. loop lock을 가진 채 selector 호출 동안 DB 쓰기 transaction을 유지하지 않는다. runtime DB는 snapshot 디렉터리 밖에 있어야 하고 CLI는 실제 경로 해석 뒤 snapshot 하위 경로와 snapshot을 가리키는 symlink를 거절한다. DB는 `0600`, lock directory는 `0700`으로 제한한다.

## 종료 조건과 요약

기록된 `stop_reason`은 다음 조건을 구분한다.

| 이유 | 의미 |
| --- | --- |
| `max_steps` | durable 선택 step 수가 설정 상한에 도달 |
| `deadline` | 저장된 최초 실행 기한 도달 |
| `no_executable_actions` | 모든 지원 행동이 처리됐거나 남은 처리 행동 없음 |
| `prerequisites_unmet` | 미처리 행동은 있지만 현재 공개 선행조건이 충족되지 않음 |
| `budget_exhausted` | 선행조건을 만족하는 행동은 있으나 잔액으로 실행할 수 없음 |
| `selector_stop` | selector가 허용된 중단 응답을 반환 |
| `policy_error` | 제안이 현재 정책/실행 가능 행동과 맞지 않거나 trusted 승인이 거절됨 |
| `retry_exhausted` | 실행 또는 공개 retry 상한 도달. pending 공개는 재개 가능 상태로 남음 |
| selector error code | sandbox, timeout, protocol 또는 schema 오류 |
| `user_interrupt` | `KeyboardInterrupt` 또는 검증기에서 사용한 durable step 후 중단. 재개 가능 |

`LoopSummary`는 선택 step 수, 고유 execution 수, released/no_record/failed/거절 수, 추가 Observation 수, pending release 수, retry 및 selector 호출 수, spent/reserved/available, 종료 이유와 재개 여부를 각각 제공한다. 이 집계는 성능 평가, hit rate, 실험 성공률이 아니다.

## 격리된 selector 실행

`IsolatedSelector`는 고정 Python runtime과 stdlib worker를 임시 read-only chroot에서 실행한다. `unshare`의 user/mount/network namespace와 static BusyBox chroot를 사용한다. selector에는 public JSON line만 stdin으로 주고 proposal만 stdout으로 받는다. coordinator API는 child에 연결하지 않고 네트워크 namespace를 비운다. 사전 검사에서 제공한 private canary를 sandbox 절대경로로 읽을 수 없는지 실제 worker 시작 때 검사한다. 필요한 launcher를 쓸 수 없으면 `selector_sandbox_unavailable`로 종료하며 in-process fallback을 사용하지 않는다. production CLI와 baseline verifier 모두 이 경로를 사용한다. `FixedOrderSelector`와 `SeededRandomPrioritySelector`의 in-process 구현은 unit fixture와 알고리즘 검증에서 사용한다.

## CLI 사용

drug 가상환경에서 module CLI를 실행한다. CLI `--snapshot`에는 revision 디렉터리, `--runtime-db`에는 해당 snapshot 밖의 private SQLite 경로를 전달한다. r2 보존 snapshot은 공개 후보 5개와 `synthetic_credit` 단위를 사용한다. 아래 5 credit은 snapshot에서 지원하는 최대 후속 assay cost 1 credit을 기준으로 최대 5개 유료 행동에 해당하며 실제 실험비가 아니다.

```bash
conda run -n drug python -m assaypilot.run_loop_cli start \
  --snapshot data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2 \
  --runtime-db /tmp/assaypilot-private/r2.sqlite \
  --run-id stage3a-r2-local-001 \
  --budget 5 --budget-unit synthetic_credit --budget-assumed \
  --cost-policy-version preserved-public-assay-cost-v1 \
  --approval-policy bounded_replay \
  --approver-id local-stage3a-bounded-replay-policy \
  --selector fixed_order --max-steps 5 --max-duration-seconds 300 \
  --max-action-retries 1 --max-release-retries 2 \
  --selector-timeout-seconds 5
```

`start`는 매번 새 run ID를 쓴다. 중단한 동일 실행은 같은 snapshot·runtime DB·run ID로 재개한다. 저장된 configuration/binding을 불러오기 때문에 새 예산이나 정책을 인수로 전달하지 않는다.

Seeded priority를 시작할 때는 정수 seed와 명시적 알고리즘 버전을 함께 전달한다. fixed-order 기본 설정에는 이 두 인수를 전달하지 않는다.

```bash
conda run -n drug python -m assaypilot.run_loop_cli start \
  --snapshot data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2 \
  --runtime-db /tmp/assaypilot-private/r2-seed-0.sqlite \
  --run-id stage3b-r2-seed-0 \
  --budget 5 --budget-unit synthetic_credit --budget-assumed \
  --cost-policy-version preserved-public-assay-cost-v1 \
  --approval-policy bounded_replay \
  --approver-id local-stage3b-bounded-replay-policy \
  --selector seeded_random_priority --seed 0 \
  --selector-algorithm-version random-priority-v1 \
  --max-steps 5 --max-duration-seconds 300 \
  --max-action-retries 1 --max-release-retries 2 \
  --selector-timeout-seconds 5
```

```bash
conda run -n drug python -m assaypilot.run_loop_cli resume \
  --snapshot data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2 \
  --runtime-db /tmp/assaypilot-private/r2.sqlite \
  --run-id stage3a-r2-local-001
```

확장 후보 검증은 `revision-20260918-primary-active-all`을 입력하고 `--max-steps 30`으로 제한한다. 유한 예산 5 credit은 유지한다. 완료 검증기는 임시 private runtime DB를 사용해 두 보존 revision을 각각 실행하고 같은 run을 2 step 이후 재개한다.

```bash
conda run -n drug python scripts/verify_stage3_run_loop.py
```

실제 run JSON 요약은 stdout에만 출력된다. private 측정과 curator 데이터는 공개 bundle, selector DTO, summary에 복사되지 않는다.

## 3-B/3-C 인계

Stage 3-B는 `Selector` protocol에 고정 순서와 seeded priority 기준선을 연결했다. trusted DTO, 정책, 승인, 실행 및 공개 복구 경로는 그대로 둔다. 동일 조건 실행 설정과 공개 trace는 [selector 비교 설정과 결과](stage3_baseline_selectors.md#사전-등록과-실행-자료)에 정의한다. r2에서는 durable step 한도와 action exhaustion이 함께 가능한 시점에 deadline이 아니면 기존 검사 순서에 따라 `max_steps`를 우선 기록한다.

Stage 3-C가 사용할 공개 요약과 trace는 `reports/stage3/baselines/<baseline-id>/runs/<revision>/<run-id>/summary.json` 및 `trace.json`에 있다. trace에는 durable step/action/request/execution ID, 후보·assay 선택 순서, 공개 view version/digest, 선택 이유, 상태, step별 budget checkpoint, 공개 Observation ID와 evidence ID/hash, 최종 stop reason만 있다. private runtime DB, curator arrays, 미시도 후보의 hidden 행이나 측정값은 포함하지 않는다. 결과의 JSON schema, run 집계 대조 방법, evidence hash 확인 결과는 [baseline 문서](stage3_baseline_selectors.md#3-c-인계-산출물)에 적었다. 정답 기반 평가는 이번 Stage 3-B 범위에 포함하지 않는다.
