# Stage 5-A 구현 및 검증 결과

**상태:** 공개 근거 context 구성, 구조화 판단 contract, provider 어댑터, CLI, fixture 검증은 구현·검증했다. 실제 LLM API smoke와 실제 공개 pair에 대한 LLM 판단 2회는 provider 설정이 없어 완료하지 못했다. 따라서 Stage 5-A의 실제 API 완료 기준은 아직 충족하지 않았다.

## 1. 실제 공개 입력에서 확인한 정보

사용한 입력은 manifest 검증을 거친 `revision-20260918-primary-active-all` 공개 bundle과 Stage 4 run `stage4-0160c1d8677d414f8a07d4035e88a5d7`의 `rev-000004/public_export.json`에서 공개된 step 1이다. 입력 구성 코드에서는 hidden snapshot, `ReplayOracle`, 다른 run의 관측을 사용하지 않았다.

| 자료 | 실제로 사용할 수 있는 필드와 제한 |
| --- | --- |
| AID 2016 primary | MEP2 yeast TOR-pathway GFP-fusion multiplex HTS. categorical `PubChem Activity Outcome`만 관측 DTO에 있다. 공식 result name은 `RESPONSE`, `Z_PRIME`이나 이번 입력 행에는 숫자가 없고 세부 실험 조건도 없다. |
| Primary candidate와 관측 | 1,682 candidates와 1,682개 primary 관측. 모두 Activity Outcome `Active`; 관측 값·단위·qualifier 없음. candidate에는 `candidate_id`, `source_id`(SID), CID, 원본 SMILES가 있다. 계산 descriptor·검증된 분자 유사도는 없다. |
| AID 2272 confirmatory | MEP2 cherry-pick confirmatory assay. categorical Activity Outcome만 context에 제공된다. 공식 result name `RESPONSE`, `Z_PRIME`, `RESPONSE_MOTHERS`, `RESPONSE_DAUGHTERS`에 해당하는 수치는 없다. 실험 조건도 사용할 수 없다. |
| 공개된 step 1 결과 | 공개 시각 `2026-09-30T23:53:06.943858Z`, candidate `candidate-0013fe448f989571e188`, AID 2272, SID `4264070`, CID `893931`, Activity Outcome `Inactive`. 수치와 단위는 없다. 당시 선택기는 `fixed_order`였고 LLM이 고른 행동이 아니다. |
| 행동 및 제한 | 초기 전체 공개 eligible action 1,682개. LLM context는 seed `3`의 재현 가능한 탐색 shortlist 24개를 포함하고 과거 step 1 후보를 고정 포함한다. 총 예산은 assumed `5 synthetic_credit`, max steps 10, max duration 300초. 후속 공개 뒤 spent 1, available 4, 남은 step 9이다. `synthetic_credit`은 실제 실험 예산이 아니다. |

모든 후보의 초기 측정 profile이 primary `Active` 하나로 같으므로 입력 자료만으로 후보의 과학적 우선순위를 구별할 근거가 없다. 따라서 validator는 이런 상태를 `evidence_guided`로 잘못 표시한 선택을 거부하고 `exploratory` basis를 요구한다.

## 2. 구현 모듈과 입력/출력 계약

- [`scientific_context.py`](../../src/assaypilot/scientific_context.py): 공개 campaign, 공개 evidence, eligible actions 및 현재 공개 상태로 `assaypilot.decision-context.v1` `DecisionContext`를 만든다. shortlist 규칙·seed·전체 수·목록 digest, state/as-of, 예산, evidence catalog, prior와 정보 공백을 포함한다. context digest는 전달되는 공개 입력에 대해서만 계산한다.
- [`scientific_reasoner.py`](../../src/assaypilot/scientific_reasoner.py): `ScientificReasoner.decide(DecisionContext) -> ReasoningResult`. `assaypilot.scientific-decision.v1` 출력에는 가설, 근거 참조, 행동 제안, 신규 관측 해석, prior 갱신, 예상 정보와 한계가 들어간다. 행동 eligibility·예산·ID·candidate/assay/evidence 연결과 새 Observation 참조를 검사한다. invalid 응답에 대한 수정은 한 번만 허용한다.
- [`llm_provider.py`](../../src/assaypilot/llm_provider.py): HTTPS Chat Completions 호환 endpoint, 정확한 model ID, 인증 header/scheme, timeout, 출력 token limit, response format과 제한된 retry를 설정으로 받는다. key와 provider response body를 오류 메시지에 기록하지 않는다. provider usage, request ID, latency를 수집한다.
- [`scientific_reasoning_cli.py`](../../src/assaypilot/scientific_reasoning_cli.py): `doctor`, `run-public-pair --preview-only`, `run-public-pair`를 제공한다. live 모드는 먼저 작은 JSON smoke를 확인하고, 같은 과거 run의 공개 관측 전/후 판단을 호출한다. 실행 context·검증 decision·manifest는 `runtime/stage5a/`에 private 권한으로 저장한다.

실제 LLM 판단은 실행 제안만 만들며 승인·예약·실행 ID를 만들지 않는다. 이 단계에서 웹 UI, Stage 3 selector, Stage 4 run loop와 연결하지 않았다.

## 3. Provider 설정과 API 호출 여부

실제 provider나 model ID는 지정·추측하지 않았다. 현재 환경에서 `doctor`는 다음 설정이 없음을 반환했다.

```text
ASSAYPILOT_LLM_ENDPOINT
ASSAYPILOT_LLM_MODEL
ASSAYPILOT_LLM_API_KEY
```

`ASSAYPILOT_LLM_PROVIDER`는 비밀이 아닌 식별 label이며 기본값은 `openai_compatible`이다. API key는 저장소 루트 `.env.stage5a.local`(POSIX mode `0600`) 또는 프로세스 secret 환경 변수에 입력한다. 그 파일은 `.gitignore`에 의해 무시된다. 정확한 실행 절차와 지원 설정은 [`stage5a_scientific_reasoning.md`](../../docs/stage5a_scientific_reasoning.md#provider-설정과-실행)에 적었다. API key는 보고서나 채팅에 기록하지 않는다.

**실제 API 호출 수: 0.** 실제 smoke 응답도 없고 사용한 실제 model ID도 없다. mock provider는 단위 테스트용일 뿐 실제 API 성공으로 취급하지 않았다. OpenAI Chat Completions를 선택한다면 지원 여부를 확인할 structured output 형식은 [공식 API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)를 따른다.

## 4. 관측 전/후 공개 context 검사

다음 명령으로 실제 공개 bundle/archive에서 pair context를 만들고 검증했다. `--preview-only`는 API를 호출하지 않는다.

```bash
conda run -n drug python -m assaypilot.scientific_reasoning_cli run-public-pair --preview-only
```

검증 결과:

| Context | 공개 상태 | 후보/관측/evidence/action | digest |
| --- | --- | --- | --- |
| T 이전 | state version 0, campaign snapshot `as_of` | shortlist 후보 24, primary Observation 24, evidence catalog 1, eligible action 24 | `2a8409094cb1c4f396151f7e969508825ed34ff243ec9a4d8564bed66c259473` |
| step 1 공개 후 | state version 1, 공개 시각 `2026-09-30T23:53:06.943858Z` | 후보 24, Observation 25(초기 24 + 신규 1), evidence catalog 2, eligible action 23 | `55d5a426f16fff9595564fc9261b13c2489160c3a707215bdab863b82329226c` |

신규 관측은 `observation-61d3d3177bfec280f7441bbca47a3a79`, 근거는 `evidence-61d3d3177bfec280f7441bbca47a3a79`이다. context evidence의 공개 원본 행에서 AID/SID/CID/Activity Outcome, observation/candidate/assay 연결과 payload hash를 검사했다. categorical outcome만 사용하므로 value, unit, comparison은 null이다. 관측 전 판단에 이 후속 결과가 포함되지 않는지, 후속 판단에는 공개된 step 1만 들어가는지 context builder와 fixture가 검증한다.

두 context의 LLM 출력과 판단 비교는 provider 자격 증명이 없어서 생성되지 않았다. 따라서 이 표는 실제 자료를 이용한 입력/시점 검증이지 실제 LLM 판단 결과가 아니다.

## 5. Fixture·실자료·실제 API 검증 구분

| 검증 종류 | 결과 |
| --- | --- |
| Stage 5-A fixture 및 provider request contract | `conda run -n drug python -m pytest tests/test_stage5a_reasoning.py -q` — **25 passed**. Active/Inactive evidence scope, `no_record` 의미, indistinguishable 후보의 exploratory basis, 잘못된 action/evidence/ID, prior 신규 관측 연결, 지시문이 포함된 evidence, strict schema와 한 번의 repair, endpoint/auth/header 설정 및 key 비노출을 검사했다. HTTP 요청은 monkeypatch fixture를 사용했다. |
| 전체 회귀 | `conda run -n drug python -m pytest -q` — **303 passed in 9.00s**. |
| Python compile / diff | 변경 모듈 `compileall` 성공. `git diff --check` 성공. |
| 실제 공개 자료 | `run-public-pair --preview-only` 성공. manifest 검증 공개 bundle과 실제 Stage 4 `public_export`에서 위 두 context를 만들었다. 이는 LLM 호출이 아니다. |
| 실제 provider smoke 및 판단 | 실행 안 됨: endpoint, model ID, API key 설정 부재. |

마지막 preview 산출물은 `runtime/stage5a/20261001T020221Z-0ade358585/`에 저장되어 있으며 `.gitignore` 대상이다. context JSON, input manifest와 digest가 있다. API 판단 artifact는 없다.

## 6. 과학적 한계와 남은 작업

현재 campaign은 primary Active 후보만 포함하고 측정 결과도 categorical이다. 농도·수치 potency·상세 조건·계산 구조 feature가 없어서 dose-response, 구조 기반 ranking, 기전, 직접 결합 또는 치료 효능을 결론내릴 수 없다. categorical PubChem Activity Outcome을 내부 verdict로 매핑하며 수치 결과를 재계산하지 않는다. 한 번의 confirmatory Inactive는 해당 named assay의 관측이며 모든 조건에서 비활성임을 뜻하지 않는다. 코드 참조 검증은 자유 서술의 과학적 진실 전체를 보증하지 않는다.

실제 검증을 마치려면 로컬에서 endpoint, provider가 발급한 정확한 model ID, API key를 `.env.stage5a.local` 또는 secret 환경 변수에 설정해야 한다. 그 다음 `doctor`와 `run-public-pair`를 실행하면 smoke 후 공개 초기 판단과 공개 후속 판단을 저장한다. 이 설정을 받기 전에는 live 호출 결과가 없다.

## 7. Stage 5-B 연결 계약

기존 Stage 3 경계는 `Selector.select(view: SelectorView) -> SelectorProposal | dict`이다. `SelectorView`는 `assaypilot.selector-view.v1`의 공개 상태 요약이지만 evidence payload는 포함하지 않는다. Stage 5-A는 public evidence를 포함한 `DecisionContext`와 `ScientificReasoner.decide(context)`를 입력으로 쓴다. Stage 5-B는 격리 worker에 key/network/evidence를 추가하지 말고 trusted 경계에서 동일 state version의 public view와 evidence로 context를 만들고, 검증된 decision을 기존 DTO로 변환해야 한다.

선택은 `SelectorProposal(kind="select", candidate_id=decision.action.candidate_id, assay_id=decision.action.assay_id, reason=decision.concise_rationale[:240])`로 표현할 수 있다. 모델이 `stop`을 제안하고 실행 가능 행동이 남았다면 `SelectorProposal(kind="stop", stop_reason="selector_stop")`로 전달하며, decision 자체는 audit artifact에 별도로 보존한다. eligible action이 없으면 모델 호출 없이 기존 `no_executable_actions` 사유를 쓴다. controller가 현재 action, 승인, 예산을 재검증하는 구조를 유지하고 stale state version의 제안은 폐기해야 한다.
