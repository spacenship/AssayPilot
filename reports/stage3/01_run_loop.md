# Stage 3-A 구현 및 검증 보고서

실행일: 2026-09-30  
환경: conda `drug`  
범위: 공개 정보 기반 제한 실행 루프, trusted 승인·실행·공개 조정, 복구, CLI, 실제 보존 snapshot 검증

## 구현 범위

Stage 3-A에서 공개 campaign의 실행 가능 행동을 열거하고, 고정 순서 selector의 제안을 trusted 정책으로 검증한 뒤 기존 `approve_action` → `execute` → `release_result` 경로에 연결했다. 각 durable step은 trusted `action_id`와 `request_id`를 사용한다. `no_record`와 `failed`는 Observation으로 만들지 않는다. 실행 가능한 행동, 공개 관측, 예산과 이미 시도한 행동의 허용 요약만 selector 입력에 포함한다. selector에 Oracle, DB, curator, evidence payload, 파일 경로 또는 시도하지 않은 행동의 기록 존재 여부를 전달하지 않는다.

고정 순서 selector는 `(candidate_id, assay_id)` 사전순의 첫 행동을 제안한다. 이는 연결·복구를 검증하는 baseline이며 생물학적 예측이나 실험 성공 판정이 아니다. Stage 3-B의 selector 비교와 Stage 3-C의 정답 기반 평가는 구현하지 않았다.

## 선행 확인과 데이터베이스 migration

- `ExecutionCoordinator.get_loop_snapshot`은 SQLite 읽기 transaction 하나에서 공개 campaign, Observation/evidence 참조, 예산, version 및 trusted 실행 상태를 읽는다. 공개 Reader의 공개 범위는 확장하지 않았다.
- 최초 승인·실행·공개 기준 시각의 timezone-aware 조건과 공개 기준 시각 역행 검사를 보완하고 회귀 테스트를 추가했다. 최초 `as_of`는 유지하며 재공개는 기존 publication 의미를 따른다.
- runtime SQLite schema를 `user_version=3`으로 올리고 `loop_runs`, `loop_steps`를 추가했다. v0/v1/v2에서 v3으로 올리며 기존 승인, 예약, 실행, 결과, 공개 근거 및 정산 자료는 보존한다. 더 높은 미지원 버전은 거절한다.
- 저장소의 보존 snapshot 아래에서 기존 runtime SQLite DB는 발견되지 않았다. v1 migration은 기존 v1 fixture로 확인했다. v2 migration은 공개·정산 데이터가 있는 생성 DB에서 loop 테이블만 제거하고 user_version을 2로 설정한 fixture로 확인했다. 이는 보존된 실제 Stage 2C 운영 DB를 migration한 결과가 아니다.

## 주요 파일

- `src/assaypilot/run_loop.py`: typed selector DTO/protocol, 고정 순서 selector, 격리 worker launcher, bounded 정책, durable loop controller, 복구 및 요약.
- `src/assaypilot/selector_worker.py`: stdlib JSON selector worker.
- `src/assaypilot/run_loop_cli.py`: `start`/`resume`, 저장된 설정 검증, snapshot 내부 및 snapshot을 가리키는 경로에 대한 runtime DB 차단.
- `src/assaypilot/execution.py`: 일관된 공개/trusted 읽기 경계, prerequisite 공유 검사, schema v3 migration 및 시각 검증.
- `tests/test_run_loop.py`, `tests/test_run_loop_cli.py`, `tests/test_execution.py`: selector 경계, 예산·선행조건, 시간 제한, crash/retry/resume, lock, migration 및 공개 상태 검증.
- `scripts/verify_stage3_run_loop.py`: 두 보존 snapshot에서 임시 runtime DB로 loop 및 같은 run 재개를 확인하는 개발자 검증기.
- `docs/stage3_run_loop.md`: 실제 DTO·정책·deadline·복구·sandbox·CLI·3-B/3-C 인계 계약.
- `docs/stage2_execution_control.md`, `docs/stage2_result_release.md`: Stage 2C schema v2를 역사적 상태로 표시하고 현재 schema v3 문서에 연결.

## Fixture 및 회귀 테스트

Stage 3-A focused 검증 명령:

```bash
conda run -n drug pytest -q tests/test_run_loop.py tests/test_run_loop_cli.py \
  tests/test_execution.py::test_schema_v1_database_migrates_without_losing_pending_execution \
  tests/test_execution.py::test_schema_v2_database_adds_loop_tables_without_losing_publication \
  tests/test_execution.py::test_future_runtime_database_version_is_rejected \
  tests/test_execution.py::test_approval_and_execution_reject_clock_regression_from_current_public_state \
  tests/test_execution.py::test_run_clock_must_be_timezone_aware
```

결과: `31 passed in 2.94s`.

전체 회귀 명령:

```bash
conda run -n drug pytest -q
```

결과: `242 passed in 5.86s`.

테스트에는 공개 입력만 이용한 안정 정렬과 hidden 결과 변형 불변성, 다중 실행 및 prerequisite 공개 경계, no-record/failed의 관측 비생성, 정확·부족·0 예산, 선택기 malformed/대형/timeout, 등록되지 않은 후보·시험과 다른 run, 네 crash 경계의 동일 ID 복구, 공개 재시도와 reservation 보존, 설정 충돌, 완료 run 재개, deadline/사용자 중단, 같은 run worker lock, 다른 run 분리, 실제 격리 selector와 private canary 접근 거절, v1/v2 migration fixture 및 clock 역행 검사가 포함된다.

추가 정적 검증:

```text
conda run -n drug python -m compileall -q src/assaypilot scripts/verify_stage3_run_loop.py  # 통과
git diff --check                                                                      # 통과
conda run -n drug python -m assaypilot.run_loop_cli --help                             # 통과
```

## 기존 snapshot 검증

Stage 3-A 실행 전후 별도 기존 검증기를 실행했다.

```bash
conda run -n drug python scripts/verify_replay_snapshots.py
conda run -n drug python scripts/verify_execution_snapshots.py
conda run -n drug python scripts/verify_result_release_snapshots.py
```

모두 통과했다. r2 snapshot은 후보 5개에서 `records_found=1`, `no_record=4`; 확장 snapshot은 후보 1,682개에서 `records_found=295`, `no_record=1,387`을 확인했다. 확장 normalized measurement 328,519개 중 hidden subset은 295개다. 공개 bundle 파일 hash는 변하지 않았다. 해당 2-A/2-B/2-C 검증의 기존 보고서 수치를 현재 Stage 3-A 실행 결과로 간주하지 않았다.

## 실제 Stage 3-A snapshot 실행

`conda run -n drug python scripts/verify_stage3_run_loop.py`는 r2와 확장 snapshot 각각에 별도 임시 runtime DB를 사용했다. 각 run을 2 step 뒤 durable 중단하고 같은 run ID로 재개했다. 선택은 selector에 제공된 공개 view의 고정 순서만 따랐다. private canary는 실제 selector worker에서 읽지 못했고, 각 공개 결과의 evidence는 기존 resolver로 찾아 hash를 확인했다. snapshot 내 public 파일은 실행 전후 동일했다.

| Revision | 공개 후보 | step 제한/실제 step | 고유 실행 | released | no_record | failed/거절 | 추가 Observation / 근거 확인 | 예산 시작/사용/예약/잔액 | 종료 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| `revision-20260917-r2` | 5 | 5 / 5 | 5 | 1 | 4 | 0 / 0 | 1 / 1개 resolver+hash 확인 | 5 / 1 / 0 / 4 `synthetic_credit` | `max_steps` |
| `revision-20260918-primary-active-all` | 1,682 | 30 / 30 | 30 | 4 | 26 | 0 / 0 | 4 / 4개 resolver+hash 확인 | 5 / 4 / 0 / 1 `synthetic_credit` | `max_steps` |

두 실행 모두 Oracle 조회 수는 step 수와 같았고 중복 조회는 없었다. settlement는 released 행동 수만큼 발생했다(각각 1건, 4건). 관측 증가 수는 released 측정 수와 같았다. 이 snapshot 실행은 replay 검증이며 실제 wet-lab 실행이나 효능 평가가 아니다.

CLI 자체도 r2 임시 DB에서 `start` 후 같은 DB/run ID로 `resume`하는 왕복 실행을 했다. 5 step, 5 고유 실행, released 1, no_record 4, Observation 1, 예산 5 중 1 사용·0 예약·4 잔액으로 끝났고, 재개 결과가 기존 요약과 일치했다. 검증 후 임시 DB를 제거했다.

## 완료 범위와 제약

Stage 3-A 코드, CLI, 문서 및 실제 snapshot replay 검증을 완료했다. 운영자가 제공한 보존 Stage 2C SQLite DB는 없어 migration 증거는 v1 기존 fixture와 v2 생성 fixture로 한정된다. 실행은 고정 snapshot·bounded replay·로컬 격리 launcher 범위다. selector 대안 비교, 성능/정답 기반 평가, ML/LLM, UI, 배포와 실제 실험은 후속 범위다. 보존 snapshot은 덮어쓰지 않았고 commit/push는 하지 않았다.
