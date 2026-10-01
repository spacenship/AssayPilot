# Stage 5-B 구현 및 검증 보고서

## 최신 보완 및 실제 실행 (2026-10-01)

아래 요약이 이 보고서의 최신 상태다. 이후의 “초기 구현” 기록은 과거 연결 실패와 기존 검증을 보존한다.

Stage 5-B에서 활성 예측과 자료 가용성 가설을 `assay_activity`와 `data_availability`로 구분했다. 선택한 candidate-assay에는 같은 pair의 검증 전 활성 가설을 연결한다. 동일 pair의 공개 Active 관측은 해당 범위의 활성 예측을 지지할 수 있고, Inactive 관측은 `weakened`로 갱신할 수 있다. Inconclusive는 지지 근거가 아니며 `no_record`와 실행 실패는 활성 증거가 아니다. 이 실제 run에는 `data_availability` 가설이 없었다. 제안된 `proposed` 가설은 확인 전 상태이고, 확정 예측이나 보정된 확률을 뜻하지 않는다.

핵심 구현은 `scientific_context.py`, `scientific_reasoner.py`, `scientific_run_loop.py`, `scientific_run_loop_cli.py`, `run_loop.py`와 관련 execution 저장 코드에 반영했다. DTO와 validator는 가설 유형·예상 결과·candidate-assay 범위·문장 일관성, 동일 ID의 관측 기반 갱신을 확인한다. Controller는 API cap 안에서 마지막 해석 호출 2회를 예약하고, 해석 전용 context에는 실행 action을 제공하지 않는다. 관련 오프라인 검증은 `test_stage5a_reasoning.py`, `test_stage5b_scientific_run_loop.py`, `test_execution.py`에 추가·갱신했다.

호출 상한은 신규 run 기본값 24회다. 정상 판단·repair·stale 재판단·마지막 해석과 repair는 하나의 상한을 공유한다. 행동 판단을 시작할 때 마지막 해석용 2회 예약을 포함해 최소 4회가 남아야 한다. 실행/예산 한도에 도달해도 pending 공개 관측이 있으면 실행 없는 `interpretation_only` 단계로 해석하며, pending이 없으면 추가 호출하지 않는다. 과거 v1 artifact와 `stage5b-de88ac21575840918126014abc05cb8f` run은 수정하지 않았고, 이전 가설 의미를 추정해 변환하지 않았다.

| 검증 항목 | 새 run 결과 |
| --- | --- |
| Run / revision | `stage5b-a3dd1a84d0d84c8db2d212ed4d9f09ac`, `artifacts/rev-000001/` |
| 실행 환경 / 설정 | `conda run -n drug`, Python 3.12.14; `openai_compatible`, Responses, `gpt-5.6-terra`, `json_schema`; API key는 보고서와 artifact에 포함하지 않음 |
| 실행 제한 | budget 5 `synthetic_credit`, max steps 10, duration 300초, API cap 24, shortlist 24/seed 3, final interpretation reserve 2 |
| API 호출 | 10회 성공한 action 판단; repair 0, stale 재판단 0, 별도 finalization 0, transport retry 0. 공개 관측 3건은 그 뒤의 일반 판단 안에서 해석되어 추가 finalization 호출은 필요하지 않았음 |
| 선택과 replay | 선택·실행 candidate-assay pair 10/10 일치; replay 10건, released 3건, `no_record` 7건, 실패 0건; budget spent 3, available 2 |
| 관측/가설 | 신규 관측 3건 모두 Inactive이며 각각 동일한 활성 가설 ID가 `proposed`에서 `weakened`로 갱신됨. 현재 활성 가설 10개(제안 7, 약화 3), 자료 가용성 가설 0개 |
| 사용량 / 종료 | provider가 보고한 179,350 tokens (input 171,519 / output 7,831); `max_steps`로 종료. 공개 관측 3/3 해석, pending 0, `interpretation_complete=true` |
| 저장 schema | context `assaypilot.decision-context.v2`, decision `assaypilot.scientific-decision.v2`, prompt `assaypilot.public-science-reasoning.v2`, SQLite `PRAGMA user_version=5` |

실제 실행은 아래 명령으로 한 번만 시작했다. 기존 run의 deadline이나 호출 카운터를 재설정하지 않았다.

```bash
conda run -n drug python -m assaypilot.scientific_run_loop_cli start \
  --budget 5 --budget-unit synthetic_credit \
  --max-steps 10 --max-duration-seconds 300 --max-llm-calls 24 \
  --shortlist-size 24 --shortlist-seed 3
```

대표적인 관측-갱신 연결은 step 4와 decision 5다.

1. 실행 전 활성 가설 ID `hypothesis-candidate-1ceb525543d5b9f6cb07-mep2-confirmatory-active`, 문장: “Candidate candidate-1ceb525543d5b9f6cb07 is expected to be active in assay mep2-confirmatory.” 상태는 `proposed`였다.
2. LLM이 선택한 pair `candidate-1ceb525543d5b9f6cb07` / `mep2-confirmatory`는 step 4에서 실행됐고 상태는 `released`였다.
3. 다음 공개 context에 `observation-05f9caeb4ce58cca0bf3c23be5709c2d`와 해당 근거 `evidence-05f9caeb4ce58cca0bf3c23be5709c2d`가 포함됐다. 관측 outcome은 Inactive였다.
4. decision 5는 같은 hypothesis ID를 `proposed`에서 `weakened`로 바꾸고 위 observation/evidence ID를 event에 저장했다. rationale은 “The newly released same-pair confirmatory observation is Inactive, opposing the expected Active outcome.”였다. 이후 context 6에도 같은 ID의 `weakened` 상태가 전달됐다.
5. decision 5는 다음 pair `candidate-1d8d38a03eefb69d9edd` / `mep2-confirmatory`를 선택했다. 이 pair 실행은 `no_record`로 끝났으며 이를 Inactive 활성 근거로 취급하지 않았다.

검토 산출물은 [`summary.json`](../../runtime/stage5b/stage5b-a3dd1a84d0d84c8db2d212ed4d9f09ac/artifacts/rev-000001/summary.json), [`decisions.json`](../../runtime/stage5b/stage5b-a3dd1a84d0d84c8db2d212ed4d9f09ac/artifacts/rev-000001/decisions.json), [`hypotheses.json`](../../runtime/stage5b/stage5b-a3dd1a84d0d84c8db2d212ed4d9f09ac/artifacts/rev-000001/hypotheses.json), [`hypothesis_history.json`](../../runtime/stage5b/stage5b-a3dd1a84d0d84c8db2d212ed4d9f09ac/artifacts/rev-000001/hypothesis_history.json), [`interpretation_status.json`](../../runtime/stage5b/stage5b-a3dd1a84d0d84c8db2d212ed4d9f09ac/artifacts/rev-000001/interpretation_status.json), [`contexts/`](../../runtime/stage5b/stage5b-a3dd1a84d0d84c8db2d212ed4d9f09ac/artifacts/rev-000001/contexts/)와 [`api_calls.json`](../../runtime/stage5b/stage5b-a3dd1a84d0d84c8db2d212ed4d9f09ac/artifacts/rev-000001/api_calls.json)이다. 기존 실패 run의 `summary.json` SHA-256은 실행 전후 동일하게 `7c263b9b2acd399fa67d632e1c90ea3e2f38d4f361e0be6f8655700e95397ea5`였다.

관련 오프라인 검증 명령은 `conda run -n drug python -m pytest tests/test_stage5a_reasoning.py tests/test_stage5b_scientific_run_loop.py tests/test_execution.py -q`이며 **114 passed**였다. 핵심 검증은 `test_data_availability_hypothesis_cannot_substitute_for_activity_prediction`, `test_inactive_observation_cannot_support_active_expected_hypothesis`, `test_no_record_and_failed_attempts_are_not_fabricated_as_inactive`, `test_scientific_loop_replays_actions_interprets_observations_and_updates_same_hypothesis`, `test_reserved_calls_cover_action_repair_and_final_interpretation_repair`, `test_reserve_prevents_new_action_when_only_three_calls_remain_and_no_observation_is_pending`다. `conda run -n drug python -m compileall -q src/assaypilot`, `git diff --check`, CLI `start --help` 확인도 통과했다. 새 실제 run에서는 step 9에서 공개된 마지막 관측까지 다음 일반 판단에서 해석됐으므로 별도 finalization 호출은 0회였다.

## 초기 Stage 5-B 연결 실패 기록 (과거 이력)

Stage 5-A `ScientificReasoner`를 기존 `RunLoopController`의 별도 `scientific_reasoner` 분기에 연결했다. Fake provider와 실제 replay fixture를 사용한 오프라인 통합·복구 검증은 통과했다. 제한된 live run은 실제 API request 1회에서 provider 연결 오류로 응답 전에 중단됐다. 따라서 코드 경로와 offline contract는 검증됐지만, 실제 LLM 선택 → replay 실행 → 결과 공개 → 다음 LLM 판단의 live 폐루프는 이번 run에서 확인되지 않았다.

Stage 5-A historical fixed-order 결과는 이번 5-B 실제 실행 증거로 사용하지 않았다.

## 변경 파일과 책임

| 파일 | 변경 책임 |
| --- | --- |
| `src/assaypilot/scientific_run_loop.py` | Trusted scientific selector, public `DecisionContext` 구성, API call 기록, stale 재검증, decision/hypothesis/interpretation/step transaction 저장, pending observation 처리와 recovery 보조 로직. |
| `src/assaypilot/scientific_run_loop_cli.py` | bounded `start`/`resume`, 공개 snapshot/replay 입력, private SQLite 및 revision artifact 관리, resume settings binding. |
| `src/assaypilot/run_loop.py` | baseline selectors 경로를 그대로 두고 `scientific_reasoner` 전용 controller loop 추가. 결과 공개 뒤 pending observation을 해석하고 한도 초과 시 신규 행동 없이 종료. |
| `src/assaypilot/execution.py` | DB schema version 4, 과학 판단/API/hypothesis/history/interpretation 상태 테이블과 기존 runtime DB migration. |
| `src/assaypilot/scientific_context.py` | full population 통계와 context 내 후보/action 통계 구분, public-only context metadata 및 shortlist digest. |
| `src/assaypilot/scientific_reasoner.py` | 부족한 evidence의 허용 표현, exploratory 근거, reference scope 및 hypothesis/observation 참조 검증. Repair는 최대 1회. |
| `src/assaypilot/llm_provider.py` | provider 설정/호출 제한과 request metadata 사용. API key 및 raw provider error body를 산출물에 포함하지 않는다. |
| `src/assaypilot/scientific_reasoning_cli.py` | 기존 Stage 5-A 판단 CLI와 provider 설정 계약을 보완했다. |
| `tests/test_stage5b_scientific_run_loop.py` | replay/fake-provider 기반 판단→실행, 후속 해석·가설 갱신, stale, 오류 거부, no_record, deadline 및 재시작 검증. |
| `tests/test_stage5a_reasoning.py`, `tests/test_execution.py` | Stage 5-A validator 보완 및 schema migration 기대값 갱신. |
| `pyproject.toml`, `.gitignore` | `assaypilot-stage5b` CLI entry point와 Stage 5-B private runtime ignore. |
| `docs/stage5b_scientific_run_loop.md`, Stage 5-A docs/report | 호출 경계, 제한, 복구와 검증 결과 문서화. |

## 호출 경로 및 경계

1. `ExecutionCoordinator.get_scientific_loop_snapshot`에서 현재 public view, execution status, evidence를 읽는다. Eligibility는 공개 결과와 controller 실행 기록에서 계산하고, hidden Oracle 결과를 reasoner context에 포함하지 않는다.
2. Stage 5-B selector는 현재 공개 eligible action에서 SHA-256 seeded shortlist를 구성한다. 과거 후보 고정 anchor는 사용하지 않는다. 이전 observation/attempt와 saved hypothesis는 해석을 위해 필요할 때만 context에 담는다.
3. `ScientificReasoner`에 schema 검증된 `DecisionContext`를 전달한다. 구조, public reference, evidence/observation 연결, prior status, assay/candidate scope, current action 및 budget 검증에 실패하면 행동 전에 거부한다.
4. 네트워크 요청 후 controller가 public state version, execution status digest, budget 및 현재 eligible pair를 재검증한다. stale 판단은 기록하고 실행하지 않는다.
5. 검증된 판단, hypothesis 변경, observation 해석 및 `SelectorProposal` 기반 step은 SQLite transaction으로 저장한다. 이후 기존 approval, `ExecutionCoordinator`, replay release와 public state 갱신을 호출한다.
6. 공개된 새 Observation은 다음 판단 context와 evidence catalog에 들어간다. 가능한 행동이 없더라도 pending Observation을 먼저 해석한다. 실제 실행 실패/no_record는 가짜 Observation으로 바꾸지 않는다.

Trusted controller process가 credentials와 네트워크 권한을 보유하지만, provider에는 public DTO 기반 요청만 보낸다. DB transaction은 network request 동안 열려 있지 않다. 기존 isolated selector worker의 격리 설정은 바꾸지 않았다. Public export는 기존 정책 그대로이며 Stage 5-B private runtime은 `/runtime/stage5b/` ignore 규칙 아래 저장한다. 관리 경로 디렉터리는 `700`, DB/artifact 파일은 `600`으로 설정한다.

## Stage 5-A 보완 세 가지

1. **통계의 기준 시점:** 초기 전체 eligible population, shortlist 생성 시점의 전체 action 수, context에 포함된 후보/action 수를 별도 기록한다. Full count를 shortlist 크기나 현재 available 수로 해석하지 않는다.
2. **shortlist와 LLM 재현성:** SHA-256 seeded shortlist는 동일 입력 후보 샘플을 추적할 수 있다. 실제 LLM proposal은 decision/context로 보존되지만 같은 요청이 같은 proposal을 반환한다고 주장하지 않는다.
3. **후보 차이에 대한 표현:** 서로 다른 SMILES나 ID만으로 우열·기전·구조활성을 추론하지 않는다. 공개 활성 근거와 검증된 특징만으로 confirmatory 우선순위를 정당화하지 못하면 `exploratory`로 판단하도록 system instruction과 validator를 정리했다.

## 예산, 한도, 영속화

Stage 5-B는 `assumed=false` replay budget을 요구하며 context에는 coordinator의 current total/spent/reserved/available/unit을 보낸다. Replay credit과 LLM provider 금액은 분리한다. Run 설정에는 selector, provider/model metadata, prompt/context schema version, shortlist seed/size, token limit, API timeout/cap, stale cap, 실행 한도와 전체 duration이 저장된다. API key는 settings fingerprint나 artifact에 포함하지 않는다. Resume 때 saved config 및 settings fingerprint가 맞지 않으면 거부한다.

요청별 timeout은 설정 timeout과 저장된 run deadline 잔여 시간 중 작은 값이다. Resume은 absolute `deadline_at`을 이어 쓰며 시간을 새로 주지 않는다. Provider transport retry는 0, Stage 5-A schema repair는 최대 한 번이며 repair/stale 판단은 API call cap에 포함된다. API 금액은 price data가 없어 계산하지 않았다.

SQLite `user_version=4`가 authoritative state다. 기존 version 0–3 migration을 지원하고 이 범위 밖은 `unsupported_database_version`으로 거부한다. Decision context, digest, state version, validation history, API metadata, current hypotheses와 event history, public observation interpretations 및 completion status를 저장한다. 검증된 decision의 hypothesis update와 execution proposal은 transaction 단위로 반영한다. 기존 action/request ID와 coordinator idempotency로 재시작 시 중복 실행 및 예산 차감을 막는다. API 응답 이후 저장 전 장애가 나면 재호출 가능성은 남으므로 외부 API exactly-once는 주장하지 않는다.

종료 때 DB에서 새로운 private artifact revision을 만든다. DB가 권위 자료이며 revision은 해당 시점의 읽기용 감사 사본이다. 이번 run은 provider 응답 전 오류로 끝났고 pending public Observation이 없어서 해석 완료 상태가 true로 기록됐다. 이는 과학적 해석을 완료했다는 뜻이 아니라 처리할 공개 Observation이 아직 없다는 뜻이다.

## 오프라인 검증

실행 명령:

```bash
conda run -n drug python -m pytest tests/test_stage5a_reasoning.py tests/test_stage5b_scientific_run_loop.py -q
conda run -n drug python -m pytest -q
conda run -n drug python -m compileall -q src/assaypilot
git diff --check
```

| 검증 | 결과 |
| --- | --- |
| Stage 5-A reasoning + Stage 5-B 통합 fixture | **54 passed in 4.22s**. |
| 전체 회귀 | **332 passed in 11.84s**. Stage 0–4, fixed/seeded selector, isolated worker, execution, web 및 Stage 5-A/5-B 테스트 포함. |
| Python compileall | 통과. |
| `git diff --check` | 통과. |
| Stage 5-B CLI help | `start`, `resume` subcommands 확인. |

주요 offline 통합 테스트는 `test_scientific_loop_replays_actions_interprets_observations_and_updates_same_hypothesis`, `test_stale_provider_result_is_saved_but_not_applied_then_redecided`, `test_interrupted_execution_resumes_without_repeating_provider_action_or_lookup` (네 crash point), `test_invalid_model_action_is_recorded_without_changing_public_or_execution_state`, `test_provider_factory_failure_closes_recorded_call_without_applying_decision`, `test_no_record_and_failed_attempts_are_not_fabricated_as_inactive`, `test_deadline_during_provider_call_discards_decision_and_stops`, `test_provider_timeout_shrinks_to_subsecond_remaining_run_deadline`, `test_managed_runtime_directory_permissions_are_private_on_start_and_resume`다. 기존 Stage 5-A validator 테스트는 다른 Observation의 evidence, 틀린 prior status, 미래 참조, assay/candidate 오류 및 초과 budget 행동을 action/hypothesis 적용 전에 거부한다.

이 검증에서 fake provider가 반환한 선택은 coordinator가 실제 replay fixture에서 실행한 pair와 일치한다. 두 번째 context에는 공개 Observation과 같은 hypothesis ID의 갱신 상태가 전달된다. 네 restart point에서 provider action이나 replay lookup이 중복 수행되지 않는다. Invalid proposal은 public state·budget·hypothesis를 바꾸지 않는다. Baseline 및 worker isolation 회귀도 전체 suite를 통과했다.

## 제한된 live API/replay 시도

실행은 prompt의 시작 한도를 사용해 **새 run 한 번만** 수행했다. 선택을 맞추기 위한 seed 변경이나 반복 실행은 하지 않았다.

```bash
conda run -n drug python -m assaypilot.scientific_run_loop_cli start \
  --budget 5 --budget-unit synthetic_credit --max-steps 10 \
  --max-duration-seconds 300 --max-llm-calls 10 \
  --shortlist-size 24 --shortlist-seed 3
```

| 항목 | 실제 값 |
| --- | --- |
| Run ID | `stage5b-e4f0ddc0a28e438999a0821c89979820` |
| Artifact revision | `runtime/stage5b/stage5b-e4f0ddc0a28e438999a0821c89979820/artifacts/rev-000001/` |
| SQLite | `runtime/stage5b/stage5b-e4f0ddc0a28e438999a0821c89979820/private/execution.sqlite` |
| Stop status | `stopped`, `stop_reason=provider_connection_error`, `resumable=true` |
| 초기 공개 입력 | state version 0, 전체 eligible action 1,682개 / eligible candidate 1,682개, context shortlist 24개 (seed 3), 신규 공개 observation 0 |
| API 설정 | `openai_compatible`, Responses API, `json_schema`, model `gpt-5.6-terra`, output limit 1,200 tokens, configured request timeout 60초, call cap 10 |
| API 시도 | **1 request**, transport retry 0, validation repair 0 |
| Token usage | Provider가 응답 전에 연결 실패. Input/output/total token 사용량 미보고 (`usage={}`) |
| Latency | 기록된 실패 응답 시간 **10 ms** |
| Replay | unique execution 0, released execution 0, public observation 0 |
| Replay budget | spent `0`, reserved `0`, available `5 synthetic_credit` |

첫 판단 `science-decision-a03304f0c3bf473790f76648cb7c6849`은 public state version 0, context digest `3344fdef660b1fdd1746952f0e0cb38c9cf355d1f4a5ef5a833ed981ba73da5f`로 저장됐다. Context shortlist metadata는 전체 eligible action 1,682, 전체 eligible candidate 1,682, context candidate 24를 기록한다. Provider 연결 오류라 `result=null`, 선택 후보/assay와 decision basis는 없다. 실행 step 또는 hypothesis 변경도 생성되지 않았다. 실제 live API 응답의 latency 이외에는 token usage가 없다. 키, 인증 header, provider의 원문 오류 body는 보고서 및 public export에 쓰지 않았다.

### 단계별 live 기록

| 판단 ID | 입력 state version | 선택 후보 / assay | basis | 실행 상태 | 공개 관측 | 가설 변화 | budget 사용 / 잔여 |
| --- | ---: | --- | --- | --- | --- | --- | --- |
| `science-decision-a03304f0c3bf473790f76648cb7c6849` | 0 | N/A — provider 응답 없음 | N/A | 판단 전에 연결 오류, 실행 안 됨 | 0 | 없음 | `0` / `5 synthetic_credit` |

Prompt의 live 성공 목표인 실제 모델 선택 실행, 두 번 이상 실행 시도, 공개 관측을 다음 실제 판단에 전달하고 이전 가설 갱신하기는 이번 run에서 **검증되지 않았다**. 원인은 첫 API request의 provider connection error다. 코드의 fixture/restart 검증은 통과했으나 이것을 live loop 성공으로 분류하지 않는다. Prompt의 단일 bounded run 원칙에 따라 API 요청을 추가하지 않았다.

## 완료 구분 및 남은 항목

- **구현 완료:** scientific selector/controller 통합, public context, current budget 및 stale revalidation, 기존 execution/publication 사용, decision/hypothesis/interpretation 저장 및 restart recovery, CLI/artifacts/private boundary.
- **오프라인 검증 완료:** 331개 전체 테스트 통과. Fake provider와 replay fixture로 선택부터 공개 후 해석 및 가설 업데이트까지 통합 검증.
- **실제 검증 완료 범위:** 새 Stage 5-B run 초기화, 실제 provider request 경계 진입, 실패 metadata의 안전한 저장, 예산/실행이 발생하지 않은 상태 보존.
- **미완료:** 성공적인 provider response, 실제 모델 선택, live replay action/release, 다음 모델 호출에서 공개 결과 해석·hypothesis 갱신.
- **장애 요인:** 실제 provider connection error. 잘못된 과학적 선택이나 fixture 실행 오류는 관찰되지 않았다. 제한 지침에 따라 동일 조건을 재시도하지 않았다.

## 후속 보완 및 신규 bounded run (2026-10-01)

앞 절은 기존 실패 run `stage5b-e4f0ddc0a28e438999a0821c89979820`의 당시 결과다. 해당 run을 수정하거나 재개하지 않았다. Provider failure metadata가 하위 원인과 HTTP/latency 정보를 잃지 않도록 보완했고, 새 `stage5b-de88ac21575840918126014abc05cb8f` run을 실행했다.

### 실패 진단 metadata 보완

- `LLMProviderError`가 failure stage, 예외 클래스, 제한된 cause code/클래스, HTTP 응답 수신 여부/status, latency를 고정 필드로 제공한다.
- DNS `EAI_AGAIN`은 `dns_temporary_failure`로 저장한다. 예외 문자열과 provider 오류 body, 인증 정보는 metadata에 포함하지 않는다.
- Stage 5-B SQLite `loop_scientific_api_calls.diagnostic_json`와 `api_calls.json`에 동일한 진단 객체를 저장한다. `usage={}`는 미보고 상태로 유지한다.
- Runtime SQLite schema를 version 5로 올리고 version 0–4에서 additive migration한다. 기존 실패 run은 이 migration 대상으로 열지 않았다.
- 기존 isolated selector worker 설정은 변경하지 않았다.

### 신규 live run

| 항목 | 결과 |
| --- | --- |
| Run ID | `stage5b-de88ac21575840918126014abc05cb8f` |
| Artifact | `runtime/stage5b/stage5b-de88ac21575840918126014abc05cb8f/artifacts/rev-000001/` |
| 실행 환경 | conda `drug`, AssayPilot 작업 디렉터리, `.env.stage5a.local` 설정 로더, 승인된 controller 네트워크 환경 |
| 제한 | budget `5 synthetic_credit`, 최대 실행 `10`, 전체 deadline `300초`, API call cap `10`, shortlist `24`, seed `3` |
| Provider 요청 | Responses API, `json_schema`, model `gpt-5.6-terra`, transport retry `0`; 10/10 요청 완료 |
| API usage | input `164,640`, output `9,923`, total `174,563` tokens (10개 응답 모두 usage 보고) |
| 실행 결과 | 9개 replay step: 3 released, 6 `no_record`; public Observation 3건; spent `3`, available `2` |
| 종료 | `stopped`, `stop_reason=llm_call_limit`, `resumable=false`; 300초 deadline 만료가 아닌 10회 API cap 도달 |

각 저장된 판단의 선택 candidate/assay는 연결된 실행 step의 pair와 **9/9 일치**했다. 공개된 Inactive 결과 두 건은 다음 판단 context의 `newly_released_observation_ids`에 들어갔고, 각각 동일 hypothesis ID의 `proposed → supported` 갱신과 해당 관측/evidence reference가 저장됐다. 이 가설의 문장은 “해당 assay에서 범주형 결과를 받는다”는 내용이므로 `supported`는 결과가 실제 기록됐다는 뜻이며 Active 예측의 지지를 뜻하지 않는다.

결정 4에서 구조화 출력 검증 보정 요청이 한 번 발생해 API call cap 중 한 회를 추가 사용했다. 그 결과 최대 10 step 설정에도 replay는 9 step에서 멈췄다.

세 번째 released Observation은 마지막 실행에서 공개됐지만 다음 판단용 API call cap이 소진되어 해석 대기 상태로 남았다(`interpretation_complete=false`, pending 1건). 따라서 9개 선택과 실행 pair 연결은 검증됐고, 다음 판단·동일 가설 갱신은 공개 결과 두 건에서 검증됐으나 모든 공개 결과에 대한 해석 완료는 검증되지 않았다. 남은 pending 관측을 처리하려는 추가 call은 이번 run의 10회 cap 밖이므로 실행하지 않았다.

### 변경 및 검증

- 변경: `llm_provider.py`, `scientific_run_loop.py`, `scientific_run_loop_cli.py`, `execution.py`, provider/Stage 5-B/schema migration 테스트, Stage 5-B 문서.
- 회귀 검증: `conda run -n drug python -m pytest -q tests/test_stage5a_reasoning.py tests/test_stage5b_scientific_run_loop.py tests/test_execution.py` — **108 passed**. DNS 원인 분류, HTTP status/body 비저장, Stage 5-B DB/artifact 진단 저장, version 4 migration을 포함한다.
