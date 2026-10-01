# Stage 3-B 선택기 기준선 구현 및 검증 보고서

실행일: 2026-09-30
환경: conda `drug`
범위: `fixed_order` 유지, 격리 worker에 seeded selector 추가, 사전 등록한 두 snapshot 기준선 실행 및 결과 보존

## 구현

`seeded_random_priority`는 상태를 보존하는 PRNG 대신 action ID와 고정 seed로 priority를 재현한다. 버전은 `random-priority-v1`이다. `[version, seed, candidate_id, assay_id]`를 `ensure_ascii=False`, JSON compact separators로 직렬화한 UTF-8 bytes의 SHA-256 digest를 계산한다. `(digest_bytes, candidate_id, assay_id)` 오름차순 첫 action을 선택하고, digest 동률은 후보 ID와 assay ID로 해소한다. action 입력 순서, run ID, 현재 시각, Python hash 상태, hidden 데이터는 우선순위에 쓰지 않는다. 공개 결과 뒤에는 기존 eligibility 로직으로 갱신된 action 집합에 같은 규칙을 적용한다.

Trusted `RunLoopConfig`에 selector kind와 seeded selector의 정수 seed·알고리즘 버전을 연결했다. worker 시작 설정은 세션 중 고정된다. CLI의 `start`는 `--selector fixed_order|seeded_random_priority`를 받으며 seeded selector에는 `--seed`와 `--selector-algorithm-version`을 요구한다. 기존 fixed-order JSON은 seed/version key를 생략해 과거 canonical hash 모양을 유지한다. resume은 저장된 원본 설정 bytes의 SHA-256을 먼저 확인한 후 과거 fixed-order 설정을 해석하며, 저장된 selector binding과 다른 seed·종류·버전은 거절한다.

변경·추가된 Stage 3-B 관련 파일은 다음과 같다.

- `src/assaypilot/run_loop.py`, `src/assaypilot/selector_worker.py`, `src/assaypilot/run_loop_cli.py`
- `tests/test_run_loop.py`, `tests/test_run_loop_cli.py`
- `scripts/verify_stage3_baselines.py`
- `docs/stage3_run_loop.md`, `docs/stage3_baseline_selectors.md`
- `reports/stage3/baselines/20260930-seeded-priority-v1/`

## 사전 등록 및 실행 조건

기준선 실행 전에 plan을 새 결과 디렉터리에 exclusive-create 방식으로 등록했다. plan SHA-256은 `8f67b350f7660c10b1226f74163236af3c402cdf7b7f2b5ab30288d715f6a4a1`이며, snapshot public-tree hash와 구현 파일별 SHA-256, 실행 조건, selector seed/version 및 run ID가 포함돼 있다. 실행기는 시작 전에 이 hash들을 다시 검사한다.

두 snapshot 모두 초기 예산 `5 synthetic_credit`(assumed), `preserved-public-assay-cost-v1`, `bounded_replay`, 최대 실행 시간 300초, action retry 1회, release retry 2회, selector timeout 5초를 사용했다. r2는 공개 후보 5개와 `max_steps=5`, 확장 snapshot은 공개 후보 1,682개와 `max_steps=30`이다. 각 snapshot에서 fixed-order 1회와 seed 0–4를 각각 한 번 실행했다. 각 비교 run은 독립된 초기 상태와 임시 private SQLite DB를 사용했다.

사전 등록 및 실행 명령:

```bash
conda run -n drug python scripts/verify_stage3_baselines.py \
  --register-plan reports/stage3/baselines/20260930-seeded-priority-v1/plan.json

conda run -n drug python scripts/verify_stage3_baselines.py \
  --execute-plan reports/stage3/baselines/20260930-seeded-priority-v1/plan.json
```

실제 격리 Python worker에서 모든 기준선 선택을 수행했다. private canary는 worker에서 접근할 수 없었다. r2 seed 0은 durable step 2개 뒤 중단하고 같은 runtime DB/run ID로 재개했다. 별도 runtime DB와 run ID로 실행한 seed 0 uninterrupted control과 `(candidate_id, assay_id)` action 순서가 일치했다. 재개 후에도 중복 settlement나 observation은 없었다.

## 실제 snapshot 비교

`durable steps`와 `executions`는 실행기 summary의 selection steps와 unique executions다. 예산 열은 `spent / reserved / available` 순서다. 모든 예산 값의 단위는 `synthetic_credit`이다.

| Snapshot | Selector / seed | Durable steps | Executions | Released | No record | Failed | Observation 증가 | 예산 spent / reserved / available | Stop reason |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| r2 | fixed_order | 5 | 5 | 1 | 4 | 0 | 1 | 1 / 0 / 4 | `max_steps` |
| r2 | seeded / 0 | 5 | 5 | 1 | 4 | 0 | 1 | 1 / 0 / 4 | `max_steps` |
| r2 | seeded / 1 | 5 | 5 | 1 | 4 | 0 | 1 | 1 / 0 / 4 | `max_steps` |
| r2 | seeded / 2 | 5 | 5 | 1 | 4 | 0 | 1 | 1 / 0 / 4 | `max_steps` |
| r2 | seeded / 3 | 5 | 5 | 1 | 4 | 0 | 1 | 1 / 0 / 4 | `max_steps` |
| r2 | seeded / 4 | 5 | 5 | 1 | 4 | 0 | 1 | 1 / 0 / 4 | `max_steps` |
| 확장 | fixed_order | 30 | 30 | 4 | 26 | 0 | 4 | 4 / 0 / 1 | `max_steps` |
| 확장 | seeded / 0 | 28 | 28 | 5 | 23 | 0 | 5 | 5 / 0 / 0 | `budget_exhausted` |
| 확장 | seeded / 1 | 26 | 26 | 5 | 21 | 0 | 5 | 5 / 0 / 0 | `budget_exhausted` |
| 확장 | seeded / 2 | 20 | 20 | 5 | 15 | 0 | 5 | 5 / 0 / 0 | `budget_exhausted` |
| 확장 | seeded / 3 | 14 | 14 | 5 | 9 | 0 | 5 | 5 / 0 / 0 | `budget_exhausted` |
| 확장 | seeded / 4 | 24 | 24 | 5 | 19 | 0 | 5 | 5 / 0 / 0 | `budget_exhausted` |

`released`는 이 replay에서 공개된 기록 수이며 Active 발견 수가 아니다. `no_record`는 실행된 action의 replay lookup 결과로, 음성 관측이나 비활성 판정이 아니다. 실행 횟수와 공개 수의 차이는 고정된 snapshot, action 제한, 예산과 no-record 처리에 따른 결과다. 이 비교는 selector의 약효·결합·임상 성능이나 일반화 우위를 평가하지 않는다.

## 보존 산출물과 공개 경계

- `plan.json`: 사전 고정 조건, snapshot 및 implementation fingerprint, run 목록.
- `execution_summary.json`: 12개 기준선 집계, snapshot hash 불변 확인, seed 0 재개와 독립 control 순서 비교.
- `runs/<revision>/<run-id>/summary.json`: 실행 설정·fingerprint hash, selector 설정, loop 집계, 격리 worker 및 canary 확인, durable 자료 대조 결과.
- `runs/<revision>/<run-id>/trace.json`: `assaypilot.stage3b.public-run-trace.v1` 공개 trace. 비교 실행 12개와 uninterrupted control 1개의 trace가 있다.

13개 trace를 다시 읽어 공개 schema와 결과 필드 allowlist를 확인했다. raw 측정값, curator 배열, 미시도 action의 기록 존재 여부는 trace에 없다. 공개 Observation/evidence 식별자와 payload hash는 공개 reader 경계에서 얻은 자료만 참조한다. 두 원본 snapshot의 public-tree hash는 등록 당시와 실행 후 동일했다. 임시 private runtime DB는 각 실행 결과를 안전하게 추출한 뒤 삭제했다.

## 검증 결과

선택기·프로토콜 fixture 및 복구 테스트:

```bash
conda run -n drug pytest -q tests/test_run_loop.py tests/test_run_loop_cli.py
```

결과: `43 passed in 3.05s`. 여기에는 canonical UTF-8 hash와 입력 순서 독립성, 강제 digest 동률 처리, 잘못된 seed/version 거절, 기존 fixed config hash 호환, 다른 seed resume 거절, hidden follow-up 변경에 대한 초기 선택 불변성, 동적 eligible set, crash/retry/resume, 격리 seeded worker의 private canary 접근 거절이 포함된다.

실제 snapshot 회귀 검증:

```bash
conda run -n drug python scripts/verify_replay_snapshots.py
conda run -n drug python scripts/verify_execution_snapshots.py
conda run -n drug python scripts/verify_result_release_snapshots.py
conda run -n drug python scripts/verify_stage3_run_loop.py
```

네 verifier가 모두 통과했다. 공개 snapshot hash 및 기존 초기 공개 상태가 유지됐고, Stage 3-A run-loop의 실제 격리·resume·budget 및 publication 검증도 다시 통과했다.

추가 검증:

```bash
conda run -n drug pytest -q
conda run -n drug python -m compileall -q src/assaypilot scripts/verify_stage3_baselines.py
git diff --check
```

전체 결과: `259 passed in 7.31s`; compileall과 diff whitespace 검사는 통과했다. seeded selector를 `run_loop_cli start`에서 실제 worker로 실행하는 별도 1-step smoke test도 통과했다(1 execution, 1 released, 1 Observation, 예산 1 사용, `max_steps`). smoke test의 임시 DB와 canary는 임시 디렉터리 종료 시 제거됐다.

## 완료 범위 및 인계

Stage 3-B의 두 selector, isolated worker/CLI 연결, seed/version 보존과 resume 호환성, 사전 등록된 실제 snapshot 비교와 공개 summary/trace를 완료했다. run별 JSON과 3-C가 읽을 경로 및 schema는 `docs/stage3_baseline_selectors.md`에 기록돼 있다. 3-C의 hit/enrichment/recall, 성능 비교와 통계 검정은 수행하지 않았다. Stage 3-B 범위에서 남은 미해결 검증은 없다.
