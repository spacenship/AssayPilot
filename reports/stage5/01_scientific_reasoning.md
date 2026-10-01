# Stage 5-A 구현 및 검증 결과

> 이 보고서는 Stage 5-A 당시의 v1 계약과 실행을 기록한다. 후속 Stage 5-B 구조화 가설 계약은 context/decision v2를 사용하며, 아래 Stage 5-A 산출물은 소급 변환하지 않고 보존한다. 최신 구현·실행 결과는 [Stage 5-B 보고서](02_scientific_run_loop.md)를 참조한다.

**상태:** 공개 근거 context 구성, 구조화 판단 contract, Chat Completions/Responses provider 어댑터, CLI, fixture 검증을 완료했다. 2026-10-01 실제 Responses API smoke와 공개 입력 전·후 판단을 실행했고, 두 판단은 모두 구조·참조 검증을 통과했다. 이번 감사 보완 후 전체 회귀는 314개 통과했다.

## 1. 실제 공개 입력에서 확인한 정보

사용한 입력은 manifest 검증을 거친 `revision-20260918-primary-active-all` 공개 bundle과 Stage 4 run `stage4-0160c1d8677d414f8a07d4035e88a5d7`의 `rev-000004/public_export.json`에서 공개된 step 1이다. 입력 구성 코드에서는 hidden snapshot, `ReplayOracle`, 다른 run의 관측을 사용하지 않았다.

| 자료 | 실제로 사용할 수 있는 필드와 제한 |
| --- | --- |
| AID 2016 primary | MEP2 yeast TOR-pathway GFP-fusion multiplex HTS. categorical `PubChem Activity Outcome`만 관측 DTO에 있다. 공식 result name은 `RESPONSE`, `Z_PRIME`이나 이번 입력 행에는 숫자가 없고 세부 실험 조건도 없다. |
| Primary candidate와 관측 | 1,682 candidates와 1,682개 primary 관측. 모두 Activity Outcome `Active`; 관측 값·단위·qualifier 없음. candidate에는 `candidate_id`, `source_id`(SID), CID, 원본 SMILES가 있다. 계산 descriptor·검증된 분자 유사도는 없다. |
| AID 2272 confirmatory | MEP2 cherry-pick confirmatory assay. categorical Activity Outcome만 context에 제공된다. 공식 result name `RESPONSE`, `Z_PRIME`, `RESPONSE_MOTHERS`, `RESPONSE_DAUGHTERS`에 해당하는 수치는 없다. 실험 조건도 사용할 수 없다. |
| 공개된 step 1 결과 | 공개 시각 `2026-09-30T23:53:06.943858Z`, candidate `candidate-0013fe448f989571e188`, AID 2272, SID `4264070`, CID `893931`, Activity Outcome `Inactive`. 수치와 단위는 없다. 당시 선택기는 `fixed_order`였고 LLM이 고른 행동이 아니다. |
| 행동 및 제한 | 초기 전체 공개 eligible action 1,682개. retrospective 전·후 비교 context는 seed `3`의 재현 가능한 탐색 shortlist 24개를 사용하고, 과거 step 1 후보를 검증 목적으로 고정 포함한다. 총 예산은 assumed `5 synthetic_credit`, max steps 10, max duration 300초. 후속 공개 뒤 spent 1, available 4, 남은 step 9이다. `synthetic_credit`은 실제 실험 예산이 아니다. |

모든 후보의 초기 측정 profile이 primary `Active` 하나로 같으므로 입력 자료만으로 후보의 과학적 우선순위를 구별할 근거가 없다. 따라서 validator는 이런 상태를 `evidence_guided`로 잘못 표시한 선택을 거부하고 `exploratory` basis를 요구한다.

## 2. 구현 모듈과 입력/출력 계약

- [`scientific_context.py`](../../src/assaypilot/scientific_context.py): 공개 campaign, 공개 evidence, eligible actions 및 현재 공개 상태로 `assaypilot.decision-context.v1` `DecisionContext`를 만든다. shortlist 규칙·seed·전체 수·목록 digest, state/as-of, 예산, evidence catalog, prior와 정보 공백을 포함한다. context digest는 전달되는 공개 입력에 대해서만 계산한다.
- [`scientific_reasoner.py`](../../src/assaypilot/scientific_reasoner.py): `ScientificReasoner.decide(DecisionContext) -> ReasoningResult`. `assaypilot.scientific-decision.v1` 출력에는 가설, 근거 참조, 행동 제안, 신규 관측 해석, prior 갱신, 예상 정보와 한계가 들어간다. 행동 eligibility·예산·ID·candidate/assay/evidence 연결과 새 Observation 참조를 검사한다. invalid 응답에 대한 수정은 한 번만 허용한다.
- [`llm_provider.py`](../../src/assaypilot/llm_provider.py): HTTPS Chat Completions 또는 Responses endpoint, 정확한 model ID, 인증 header/scheme, timeout, 출력 token limit, response format과 제한된 retry를 설정으로 받는다. key와 provider response body를 오류 메시지에 기록하지 않는다. provider usage, request ID, latency를 수집한다.
- [`scientific_reasoning_cli.py`](../../src/assaypilot/scientific_reasoning_cli.py): `doctor`, `run-public-pair --preview-only`, `run-public-pair`를 제공한다. live 모드는 먼저 작은 JSON smoke를 확인하고, 같은 과거 run의 공개 관측 전/후 판단을 호출한다. 실행 context·검증 decision·manifest는 `runtime/stage5a/`에 private 권한으로 저장한다.

실제 LLM 판단은 실행 제안만 만들며 승인·예약·실행 ID를 만들지 않는다. 이 단계에서 웹 UI, Stage 3 selector, Stage 4 run loop와 연결하지 않았다.

## 3. Provider 설정과 API 호출 여부

소스 코드에는 특정 provider endpoint, model ID, API key를 기본값으로 넣지 않았다. 사용자가 프로젝트 루트 `.env.stage5a.local`에 제공한 현재 설정을 `doctor`로 확인했다. 출력에는 비밀값이 포함되지 않는다.

`ASSAYPILOT_LLM_PROVIDER`는 비밀이 아닌 식별 label이며 기본값은 `openai_compatible`이다. API key는 저장소 루트 `.env.stage5a.local`(POSIX mode `0600`) 또는 프로세스 secret 환경 변수에 입력한다. 그 파일은 `.gitignore`에 의해 무시된다. 정확한 실행 절차와 지원 설정은 [`stage5a_scientific_reasoning.md`](../../docs/stage5a_scientific_reasoning.md#provider-설정과-실행)에 적었다. API key는 보고서나 채팅에 기록하지 않는다.

현재 설정은 provider label `openai_compatible`, API mode `responses`, model `gpt-5.6-terra`, 인증 header `api-key`, strict `json_schema`이다. API key는 `.env.stage5a.local`에만 두었고 파일 권한은 POSIX `0600`, Git ignore 적용 상태다. 실제 설정 검사는 `doctor`에서 `ready: true`, `credentials_present: true`, `token_parameter: max_output_tokens`로 확인했다.

최종 성공 실행은 [runtime 산출물](../../runtime/stage5a/20261001T044042Z-34a8a5c868/)에 보존했다. 이 실행에서 smoke 1회와 판단 2회(초기 공개 context, 공개 step 1 반영 context)를 호출했다. smoke 응답은 JSON object였고 사용량은 input 60 / output 16 / total 76 tokens로 기록됐다. 두 판단은 각 1회 호출, validation issue 0건으로 통과했고 `input_manifest.json` 상태는 `completed`, `actual_api_call`은 `true`다. 앞선 live 시도는 validation 및 산출물 교체 문제를 드러냈고 실패 상태로 기록됐다. 수정 후 최종 실행이 완료됐다. API key나 원시 provider response는 보고서에 기록하지 않았다.

## 4. 관측 전/후 공개 context 검사

다음 명령으로 실제 공개 bundle/archive에서 pair context를 만들고 검증했다. `--preview-only`는 API를 호출하지 않는다.

```bash
conda run -n drug python -m assaypilot.scientific_reasoning_cli run-public-pair --preview-only
```

검증 결과:

| Context | 공개 상태 | 후보/관측/evidence/action | digest |
| --- | --- | --- | --- |
| T 이전 | state version 0 (`trace.steps[step_no=1].public_view.state_version`), campaign snapshot `2026-09-15T00:00:00+00:00` | shortlist 후보 24, primary Observation 24, evidence catalog 1, eligible action 24 | `2a8409094cb1c4f396151f7e969508825ed34ff243ec9a4d8564bed66c259473` |
| step 1 공개 후, preview (`prior_hypotheses=[]`) | state version 2 (matching `published_results.executions[].state_version`), 공개 시각 `2026-09-30T23:53:06.943858+00:00` | 후보 24, Observation 25(초기 24 + 신규 1), evidence catalog 2, eligible action 23 | `55d5a426f16fff9595564fc9261b13c2489160c3a707215bdab863b82329226c` |
| step 1 공개 후, live chained judgment | 같은 state version 2; 첫 판단의 prior hypothesis 1개 포함 | 후보 24, Observation 25, evidence catalog 2, eligible action 23 | `aea305f14c0746954aa06acebd483a1e3bb440753916632a8f3e94bfa68d3de3` |

신규 관측은 `observation-61d3d3177bfec280f7441bbca47a3a79`, 근거는 `evidence-61d3d3177bfec280f7441bbca47a3a79`이다. context evidence의 공개 원본 행에서 AID/SID/CID/Activity Outcome, observation/candidate/assay 연결과 payload hash를 검사했다. categorical outcome만 사용하므로 value, unit, comparison은 null이다. 관측 전 판단에 이 후속 결과가 포함되지 않는지, 후속 판단에는 공개된 step 1만 들어가는지 context builder와 fixture가 검증한다.

step 번호 `1`은 trace의 과거 실행을 찾는 데만 쓰인다. 초기 상태 버전은 해당 trace 행의 `public_view.state_version`에서 읽고, 공개 후 버전은 동일 step/candidate/assay에 일치하는 persisted published execution의 `state_version`에서 읽는다. 현재 archive의 값은 각각 `0`, `2`이며 `step_no` 값으로 버전을 추정하지 않는다. 코드에서 원본 필드 누락, bool, 음수, 정수가 아닌 값과 공개 후 버전이 증가하지 않는 경우를 거부한다.

최종 live 실행에서 두 context의 LLM 판단이 모두 생성됐다. 초기 판단은 candidate `candidate-0013fe448f989571e188`의 AID `mep2-confirmatory` 선택을 제안했고, 공개된 step 1 이후 판단은 candidate `candidate-0118b069f69eef28073a`의 같은 assay 선택을 제안했다. 두 decision 모두 `validation_history`의 첫 시도에서 `valid: true`, issues 0건이다. 이는 실행 제안이며 실제 assay 실행이나 selector/controller 연결을 뜻하지 않는다.

## 5. 네 항목 검증

### 공개 상태 버전 출처

문서의 이전 state version `1` 표기는 현재 공개 archive와 맞지 않아 `2`로 정정했다. 실제 전·후 `DecisionContext`는 각각 archive의 `public_view.state_version=0`과 대응 published execution의 `state_version=2`를 사용한다. trace step 번호는 step 1 레코드를 찾는 용도일 뿐 상태 버전 계산에 쓰지 않는다. `test_public_archive_builds_fixed_pre_post_context_from_step1_only`는 context 값과 원본 필드의 일치를 확인하고, `test_state_version_must_be_an_explicit_nonnegative_integer`는 버전 누락, bool, 음수, 문자열을 거부하는지 확인한다.

### 이전 가설 전달과 갱신

보존된 실제 실행 산출물 `runtime/stage5a/20261001T044042Z-34a8a5c868/`에서 초기 판단은 `hypothesis-candidate-0013fe448f989571e188-mep2-confirmatory-active`를 `proposed`로 냈다. 후속 context의 `prior_hypotheses`에 같은 ID, candidate, assay, 상태가 전달됐다. 후속 판단은 같은 ID를 `proposed → weakened`로 갱신하고 새 Observation `observation-61d3d3177bfec280f7441bbca47a3a79`와 evidence `evidence-61d3d3177bfec280f7441bbca47a3a79`를 참조했다. 그 근거가 해당 후보의 AID 2272 `Inactive` 기록임을 `prior_updates.rationale`가 설명한다. 다음 행동 후보가 바뀐 것만으로 가설 갱신을 판단하지 않고 ID·이전/새 상태·관측·evidence 참조를 대조했다.

### 관측 해석과 선택 근거

후속 판단의 historical candidate 해석은 `outcome=inactive`이며 위 신규 Observation ID와 evidence ID를 직접 참조한다. 다음 선택의 `decision_basis=exploratory`는 현재 후보 간 선택 근거가 부족하다는 뜻이다. 관측의 해석을 무효화하거나 evidence 요건을 면제하지 않는다. `test_post_observation_can_update_a_prior_only_from_new_evidence`는 첫 판단에서 만든 가설이 context에 전달되고 동일 ID가 갱신되는 흐름을 검증한다. 이 테스트는 exploratory 선택과 함께 Inactive 해석의 evidence가 해당 관측에서 와야 함을 확인하며 다른 evidence로 바꾸면 `interpretation:evidence_not_from_observation` 오류가 발생하는지도 검사한다.

### 과거 후보 고정의 범위와 신규 실행

고정 후보는 `build_actual_public_pair`의 Stage 5-A 역사적 공개 전·후 비교용 shortlist에서만 사용한다. 현재 archive에서는 과거 step 1 candidate가 pre context의 24개 shortlist에 포함되고, 공개 후에는 그 candidate context가 과거 관측 해석을 위해 남아 있지만 이미 처리된 candidate-assay pair는 23개 eligible action에서 제외된다. 이 고정 shortlist는 신규 run의 selector 입력으로 사용하지 않는다. Stage 5-B는 새 run마다 controller의 현재 공개 view와 execution 상태로 eligible action을 다시 계산하고, 새 deterministic shortlist를 만든다. `test_seeded_selector_uses_released_prerequisite_in_the_next_eligible_set`는 이미 시도한 pair가 baseline selector 실행 가능 목록에 재등장하지 않는 기존 동작을 검사한다. Stage 5-B의 현재-state shortlist와 LLM 연결은 [`Stage 5-B 구현·검증 보고서`](02_scientific_run_loop.md)를 참조한다.

## 6. Fixture·실자료·실제 API 검증 구분

| 검증 종류 | 결과 |
| --- | --- |
| Stage 5-A fixture 및 provider request contract | `conda run -n drug python -m pytest tests/test_stage5a_reasoning.py -q` — **36 passed in 2.48s**. 명시 상태 버전 출처/형식, pre/post 공개 context, 과거 후보의 실행 목록 제외, 첫 판단에서 나온 prior의 전달과 동일 ID 갱신, Inactive 관측의 정확한 evidence 연결을 추가 검증했다. 기존 Active/Inactive scope, `no_record`, strict schema, provider request contract 및 key 비노출 테스트도 통과했다. |
| Stage 4 실행 가능 후보 | `conda run -n drug python -m pytest tests/test_run_loop.py::test_seeded_selector_uses_released_prerequisite_in_the_next_eligible_set -q` — **1 passed**. 공개 Observation으로 열린 후속 action과 이미 처리한 action의 제외를 확인했다. |
| 전체 회귀 | `conda run -n drug python -m pytest -q` — **314 passed in 10.22s**. |
| Python compile / diff | 변경 모듈 `compileall` 성공. 현재 working-tree `git diff --check` 성공. `git diff --cached --check`는 이번 작업과 무관하게 이미 staged된 `reports/stage3/01_run_loop.md` 3–4행의 trailing whitespace 두 곳을 표시했다. 해당 파일은 수정하지 않았다. |
| 실제 공개 자료 | `run-public-pair --preview-only`와 앞서 기록한 live `run-public-pair` 모두 성공. 이번 preview는 `actual_api_call: false`; manifest 검증 공개 bundle과 실제 Stage 4 `public_export`의 공개 step 1만 사용했다. hidden follow-up은 입력하지 않았다. |
| 실제 provider smoke 및 판단 | Responses API smoke 1회와 판단 2회 성공. 최신 실행 manifest는 `completed`; 각 판단의 validation issue는 0건. |

최신 성공 산출물은 `runtime/stage5a/20261001T044042Z-34a8a5c868/`에 저장되어 있으며 `.gitignore` 대상이다. 디렉터리는 mode `0700`, JSON 파일은 mode `0600`이다. 초기/후속 context, 두 판단 결과, smoke metadata, input manifest와 digest가 들어 있다. 이 경로에는 공개 context와 판단 결과가 있으나 API key나 원시 provider response는 없다.

## 6. 과학적 한계와 남은 작업

현재 campaign은 primary Active 후보만 포함하고 측정 결과도 categorical이다. 농도·수치 potency·상세 조건·계산 구조 feature가 없어서 dose-response, 구조 기반 ranking, 기전, 직접 결합 또는 치료 효능을 결론내릴 수 없다. categorical PubChem Activity Outcome을 내부 verdict로 매핑하며 수치 결과를 재계산하지 않는다. 한 번의 confirmatory Inactive는 해당 named assay의 관측이며 모든 조건에서 비활성임을 뜻하지 않는다. 코드 참조 검증은 자유 서술의 과학적 진실 전체를 보증하지 않는다.

이번 검증은 실제 Responses API 호출까지 완료했다. 이후 재실행은 프로젝트 루트 `.env.stage5a.local`을 유지하고 `doctor` 및 `run-public-pair` 명령을 사용한다. API 호출은 `synthetic_credit`과 별도이며 사용 요금은 provider 계정 정책을 따른다.

## 7. Stage 5-B 연결 결과

기존 Stage 3 경계는 `Selector.select(view: SelectorView) -> SelectorProposal | dict`이다. `SelectorView`는 `assaypilot.selector-view.v1`의 공개 상태 요약이며 evidence payload는 포함하지 않는다. Stage 5-B는 기존 격리 worker를 확장하지 않고 trusted `RunLoopController` 안에 `scientific_reasoner` 분기를 추가했다. 이 분기는 원자적으로 읽은 동일 state version의 public view와 evidence로 `DecisionContext`를 구성하고 `ScientificReasoner.decide(context)`를 호출한다.

검증된 선택은 `SelectorProposal(kind="select", candidate_id=..., assay_id=..., reason=...)`로 변환되어 기존 승인·예산·실행 계층으로 전달된다. `stop`도 판단 산출물에 보존된다. 새 공개 관측이 남아 있으면 실행 가능한 행동이 없더라도 관측을 해석할 수 있도록 판단을 수행하며, 해석할 관측과 행동이 모두 없을 때는 불필요한 호출 없이 종료한다. 실행 직전 controller가 현재 state version, 실행 상태, 예산과 eligible action을 다시 확인하고 stale decision은 실행하지 않는다. 구현 및 실행 결과는 [`Stage 5-B 구현·검증 보고서`](02_scientific_run_loop.md)에 정리했다.
