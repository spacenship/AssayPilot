# Stage 3-B selector 기준선과 비교 실행

Stage 3-B는 3-A의 trusted 승인·실행·공개·복구 흐름을 유지하면서 두 선택 기준선을 같은 공개 campaign에서 실행한다. `fixed_order`는 결정적 연결 기준이고 `seeded_random_priority`는 고정 seed로 계산한 의사난수 action 순서다. 어느 쪽도 분자 활성, assay 효능, 실험 성공 또는 일반화 성능을 예측하지 않는다.

## 선택기 계약

두 selector는 `Selector.select(SelectorView) -> SelectorProposal` 계약을 사용한다. controller가 현재 공개 후보와 공개 assay 정의, 이미 공개된 관측, budget, 현재 executable actions와 이 run에서 처리된 행동의 제한 요약을 전달한다. selection proposal은 후보와 assay, 한 줄 이유만 포함한다. controller가 eligibility와 proposal을 다시 확인하고 ID 생성, 승인, 비용, Oracle replay, publication과 settlement를 실행한다. 종료 및 공개 전 pending-result 처리는 trusted controller의 책임이다.

### `fixed_order`

현재 executable action을 `(candidate_id, assay_id)` 사전순으로 정렬하고 첫 행동을 고른다. 기존 selector와 실행 의미를 유지한다. 선택 사유는 안정 정렬 기준만 설명한다.

### `seeded_random_priority`

알고리즘 버전은 `random-priority-v1`이다. trusted 설정은 Python `bool`이 아닌 정수 `seed`와 명시적 algorithm version을 요구한다. float, 문자열, 누락 seed와 지원하지 않는 version은 거절한다.

각 현재 eligible action `(candidate_id, assay_id)`의 priority 입력은 다음 canonical JSON array다.

```python
json.dumps(
    ["random-priority-v1", seed, candidate_id, assay_id],
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")
```

SHA-256 digest bytes를 계산하고 `(digest_bytes, candidate_id, assay_id)` 오름차순의 첫 action을 선택한다. digest가 동률이면 후보 ID, 그다음 assay ID가 결정한다. 따라서 입력 배열 순서, Python hash seed, PRNG 상태, 현재 시각, run/request/action/execution UUID, hidden 결과와 전체 snapshot hash는 priority를 바꾸지 않는다. seed와 version, action ID가 같으면 run ID가 달라도 같은 priority다. eligible set이 공개된 Observation이나 prerequisite 충족으로 바뀌면 새 set에 동일 규칙을 적용한다. 처리된 행동은 기존 controller가 제외하고 빈 set은 기존 `no_executable_actions` stop 계약을 쓴다.

이 알고리즘은 fixed seed에서 계산한 해시 우선순위다. step마다 독립 uniform 난수를 추출하는 방법이라고 해석하지 않는다. 선택 이유는 “first eligible action by the fixed seeded SHA-256 priority”로 기록되며 생물학적 근거 또는 효능을 뜻하지 않는다.

## 설정, fingerprint와 resume

새 run의 `RunLoopConfig`에는 `selector_kind`, `selector_seed`, `selector_algorithm_version`이 저장된다. fixed-order canonical JSON은 호환성을 위해 seed/version key를 생략하므로 기존 Stage 3-A config SHA-256이 바뀌지 않는다. seeded run에는 두 key 모두 들어간다.

resume은 DB에 저장된 원본 UTF-8 config JSON의 SHA-256을 먼저 검증한다. 그다음 과거 fixed-order JSON에서 생략된 selector binding을 명시적으로 `fixed_order`와 null seed/version으로 읽고 canonical config hash를 확인한다. 설정 JSON을 조용히 다시 쓰거나 hash 검증을 건너뛰지 않는다. seeded run을 resume할 때 selector kind, seed, algorithm version이 저장 설정과 맞지 않으면 controller가 거절한다. worker는 시작 시 trusted selector binding을 한 번 받고 세션 동안 바꾸지 않는다.

CLI는 `fixed_order`를 기본으로 유지한다. seeded selector는 아래처럼 `--seed`와 `--selector-algorithm-version`을 모두 요구한다.

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

Resume uses the same `--snapshot`, `--runtime-db`, and `--run-id`; seed and version are recovered from the persisted config and cannot be changed. All production CLI selectors use the same isolated Python worker. It runs in an unshare user/mount/network namespace and read-only chroot, accepts public JSON only, and has no in-process fallback. Input/output bounds and proposal validation remain those of Stage 3-A.

## 사전 등록과 실행 자료

실제 비교는 `scripts/verify_stage3_baselines.py`가 수행한다. 실행하기 전에 verifier가 `plan.json`을 새 baseline directory에 exclusive-create로 기록한다. plan에는 snapshot revision과 public-tree hash, 공개 후보 수, source-file hash로 만든 implementation fingerprint, 실행 명령, budget/cost/approval/retry/deadline 조건, selector kind·seed·version과 각 run ID가 들어간다. 이미 파일이 있는 결과 directory는 거절되어 이전 산출물을 덮지 않는다.

기준 조건은 두 보존 snapshot 모두 budget `5 synthetic_credit`, `bounded_replay`, cost policy `preserved-public-assay-cost-v1`, max duration 300초, action retry 1, release retry 2, selector timeout 5초다. r2는 `max_steps=5`, 확장 snapshot은 `max_steps=30`이다. 각각 `fixed_order` 1회와 seed 0–4를 한 번씩 실행한다. 각 run은 별도 초기 상태와 새로운 임시 private SQLite DB를 사용한다. verifier는 실제 격리 worker와 매 worker 시작 때 private canary 접근 거절 검사를 수행한다. 임시 runtime DB는 public summary/trace를 안전하게 추출한 뒤 삭제된다.

이 baseline revision은 `reports/stage3/baselines/20260930-seeded-priority-v1/`에 저장한다. 실제 수행 순서는 다음과 같다.

```bash
conda run -n drug python scripts/verify_stage3_baselines.py \
  --register-plan reports/stage3/baselines/20260930-seeded-priority-v1/plan.json

conda run -n drug python scripts/verify_stage3_baselines.py \
  --execute-plan reports/stage3/baselines/20260930-seeded-priority-v1/plan.json
```

`plan.json`은 실행 전에 고정한다. verifier는 실행 전에 implementation file hash와 public tree hash를 다시 확인하며 mismatch가 있으면 중단한다. 각 run은 다음 두 파일을 만든다.

- `runs/<snapshot-revision>/<run-id>/summary.json`: config/fingerprint hash, 실행 시각, selector 설정, 최종 `LoopSummary`, interruption/resume 여부, 격리/canary 확인, DB request/settlement/lookup 대조 결과.
- `runs/<snapshot-revision>/<run-id>/trace.json`: `assaypilot.stage3b.public-run-trace.v1` 공개 step trace.

Trace는 step/action/request/execution ID, 순서대로 선택된 candidate/assay ID, 당시 public view version/digest, selector reason, status 및 제한된 error code, step 완료 시 durable budget checkpoint, 공개 Observation ID와 evidence ID/public payload hash, 최종 stop reason을 가진다. trace 추출기는 runtime DB에서 private result를 복사하지 않고 publication reader가 반환한 released execution과 evidence만 참조한다. `no_record`는 hidden 데이터의 음성 label이 아니다. Snapshot 안에서 lookup 가능한 공개 후속 기록이 없다는 replay 상태며 Observation을 만들지 않는다.

`execution_summary.json`은 run별 필수 표 열(durable steps, unique executions, released, no_record, failed, Observation 증가, spent/reserved/available, stop reason)과 recovery control 비교를 제공한다. verifier는 이를 durable loop steps, execution requests, settlement, replay lookup, 현재 budget 및 publication reader와 대조하고 public snapshot tree hash가 유지됐는지 검사한다.

R2 seed-0 baseline run은 durable step 2개 뒤 `user_interrupt`로 멈추고 같은 DB/run ID로 재개한다. 별도 DB와 run ID로 동일 seed의 uninterrupted control을 실행해 `(candidate_id, assay_id)` 전체 순서를 비교한다. 이는 run ID가 priority에 들어가지 않고 복구 후에도 같은 공개 피드백에서 같은 순서를 제공하는지 검증한다.

`max_steps`와 action exhaustion이 동시에 될 수 있으면 controller는 deadline을 우선 확인한 뒤 step 한도를 확인한다. 따라서 deadline이 먼저 도달하지 않았다면 step 수가 상한에 도달한 종료 사유 `max_steps`가 `no_executable_actions`보다 먼저 기록된다.

## 3-C 인계 산출물

3-C는 다음 경로를 읽을 수 있다.

```text
reports/stage3/baselines/<baseline-id>/plan.json
reports/stage3/baselines/<baseline-id>/execution_summary.json
reports/stage3/baselines/<baseline-id>/runs/<revision>/<run-id>/summary.json
reports/stage3/baselines/<baseline-id>/runs/<revision>/<run-id>/trace.json
```

`plan.json`은 실행 전 고정 조건을 확인하고, `summary.json`은 run aggregate를, `trace.json`은 선택·공개 순서 및 공개 근거 식별자를 제공한다. 비교 단계는 durable count와 금액, stop reason이 요약/trace와 맞는지 확인할 수 있다. stage 3-B는 hit, enrichment, recall, 통계적 유의성 또는 selector 성능 우위를 계산하지 않는다. 실행당 결과 수 차이는 이 고정 snapshot과 bounded replay에서 관측된 공개 기록 상태이며 과학적 효과 판단이 아니다.
