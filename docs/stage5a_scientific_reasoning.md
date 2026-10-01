# Stage 5-A: 공개 근거 기반 과학적 판단

## 범위와 상태

이 단계는 공개 `PublicCampaign`와 한 Stage 4 run의 공개 archive를 `DecisionContext`로 만들고, 설정된 Chat Completions 호환 LLM에 가설·관측 해석·다음 행동을 요청한 뒤 구조와 참조를 검증한다. 판단기는 Stage 3 selector나 Stage 4 controller에 연결되지 않는다. 제안은 실행 요청이 아니며, 실제 assay 실행·Oracle 조회·승인·예약·정산·웹 변경은 수행하지 않는다.

공개 입력 구성, fixture 검증, provider 어댑터와 CLI는 구현되어 있다. 현재 checkout에는 provider 설정이 없어 live API smoke와 관측 전/후 LLM 판단은 아직 실행되지 않았다. 실제 API 검증은 이 문서의 설정 후 실행 절차를 완료해야 한다.

## 실제 입력의 정보 가용성

검사한 snapshot은 `revision-20260918-primary-active-all`의 manifest 검증 공개 bundle이다. 후속 관측은 동일한 `stage4-0160c1d8677d414f8a07d4035e88a5d7` run의 `rev-000004/public_export.json`에서 공개된 step 1만 읽는다. `ReplayOracle`, hidden follow-up 파일, 다른 run의 결과는 입력 구성에 사용하지 않는다.

| 정보 | 공개 입력에서 확인한 내용 |
| --- | --- |
| Primary assay | PubChem AID 2016, MEP2 yeast TOR-pathway GFP-fusion multiplex HTS. DTO endpoint는 categorical `PubChem Activity Outcome`이다. 공식 assay 설명에는 `RESPONSE`, `Z_PRIME` result name이 있지만 이번 관측 행에 수치 값은 없다. 세부 조건은 DTO에서 사용할 수 없다. |
| Confirmatory assay | PubChem AID 2272, MEP2 cherry-pick confirmatory assay. DTO endpoint는 categorical `PubChem Activity Outcome`이다. 공식 설명에 `RESPONSE`, `Z_PRIME`, `RESPONSE_MOTHERS`, `RESPONSE_DAUGHTERS`가 있으나 공개 관측 행에 해당 수치가 없다. 세부 조건은 DTO에서 사용할 수 없다. |
| Candidate identity | 공개 campaign의 1,682 candidates. `candidate_id`와 `source_id`(PubChem SID), `source_cid`, 원본 SMILES가 공개된다. 모든 1,682개에 원본 SMILES가 있으나 계산 descriptor나 검증된 유사도 feature는 입력되지 않는다. |
| Initial observations | 1,682개 모두 AID 2016의 `Active` Activity Outcome이다. 값·단위·비교 qualifier는 없다. 입력 campaign 자체가 primary Active 후보로 구성되어 있어 primary Active와 Inactive 간 비교는 할 수 없다. |
| Public follow-up | 동일 run의 step 1에서 후보 `candidate-0013fe448f989571e188`의 AID 2272 관측 1건이 `2026-09-30T23:53:06.943858Z`에 공개됐다. 결과는 categorical `Inactive`; 수치·단위는 없다. 공개 근거 행은 AID `2272`, SID `4264070`, CID `893931`, Activity Outcome `Inactive`이다. |
| Executable actions | 초기 공개 상태에서 confirmatory assay의 공개 eligible action 1,682개. LLM에는 재현 가능한 SHA-256 seeded exploratory shortlist 24개만 전달하고 과거 fixed-order 선택 후보를 shortlist에 고정 포함한다. 이 shortlist는 학습된 분자 ranking이 아니다. |
| Budget and limits | Stage 4 run 설정의 총 `5 synthetic_credit`, assumed budget, 최대 step 10, 최대 duration 300초. step 1 공개 뒤 spent `1`, available `4`, 남은 step 9. 이는 wet-lab 예산이 아니다. |

초기 판단은 step 1 공개 시각 이전의 campaign snapshot `as_of`와 state version 0으로 구성했다. 후속 판단은 step 1 공개 시각, state version 1, 추가 Observation, 갱신된 예산으로 구성한다. 공개 context에 표시된 `no released follow-up Observation`은 해당 시점의 입력에 후속 Observation이 없다는 뜻이며, 실험 수행 여부를 단정하지 않는다.

## 모듈과 계약

| 모듈 | 계약과 책임 |
| --- | --- |
| `src/assaypilot/scientific_context.py` | `build_context(PublicCampaign, public_evidence, eligible_actions, ...) -> DecisionContext`; 공개 후보·assay·관측·evidence, 시점, 제한, shortlist, 예산과 prior를 검증하고 canonical digest를 계산한다. `public_as_of`보다 늦게 공개된 관측, 끊어진 candidate/assay/evidence identity, 잘못된 evidence hash, context 초과 크기를 거부한다. |
| `src/assaypilot/scientific_reasoner.py` | `ScientificReasoner.decide(DecisionContext) -> ReasoningResult`; `ScientificDecision` schema를 요청하고 행동 eligibility, 예산, evidence/state 참조 및 scope, prior의 신규 관측 참조, 관측 verdict 연결을 검증한다. 모델 출력은 최대 32 KiB이며 schema 오류는 한 번만 수정 요청한다. |
| `src/assaypilot/llm_provider.py` | `OpenAICompatibleChatProvider.complete(messages, json_schema, ...) -> ProviderResponse`; 설정된 HTTPS endpoint/model/auth로 요청하고 제한된 timeout·출력·재시도, 응답 크기 제한, request ID·latency·provider usage를 처리한다. 파일 경로, DB, Oracle 또는 실행 서비스는 provider에 전달하지 않는다. |
| `src/assaypilot/scientific_reasoning_cli.py` | `doctor`, `run-public-pair --preview-only`, `run-public-pair` 명령. 실제 공개 pair context를 저장한 다음 live 모드에서는 먼저 작은 JSON smoke call을 하고, 초기 판단과 공개 step 1을 반영한 판단을 순서대로 호출한다. 산출물은 gitignore된 `runtime/stage5a/` 아래 private mode로 저장한다. |

DTO 버전은 `assaypilot.decision-context.v1`과 `assaypilot.scientific-decision.v1`이다. context digest는 실제 전달되는 공개 context만 포함하며 hidden snapshot hash나 전체 hidden 결과 분포는 포함하지 않는다. evidence 원문과 assay 설명은 untrusted data로 system instruction과 분리한다.

`ScientificDecision`은 `select|stop`, `evidence_guided|exploratory|insufficient_information`, assay/candidate 범위 가설과 상태, prior 갱신, basis evidence refs, 실행 상태 refs, 간단한 근거, expected information, 관측별 해석, gaps와 limitations를 가진다. 보정되지 않은 확률이나 chain-of-thought를 요구하지 않는다. 이 context의 후보별 primary profile이 같으면 `evidence_guided` 선택을 validator가 거부하고 exploratory 근거를 요구한다. 측정값의 과학적 의미나 자유 서술 전체가 참인지 validator가 완전 증명하지는 않는다.

## Provider 설정과 실행

실제 model ID나 API key는 이 저장소에서 추측하거나 설정하지 않았다. 프로젝트 루트의 `.env.stage5a.local` 또는 프로세스 환경에 아래 값을 설정한다. 프로세스 환경 변수가 파일 값보다 우선한다. 설정 파일은 POSIX에서 mode `0600`이어야 하며 `.gitignore`에 의해 제외된다.

```dotenv
ASSAYPILOT_LLM_PROVIDER=<provider-label>
ASSAYPILOT_LLM_ENDPOINT=<HTTPS Chat Completions endpoint>
ASSAYPILOT_LLM_MODEL=<exact model ID from that provider>
ASSAYPILOT_LLM_API_KEY=<secret>
ASSAYPILOT_LLM_AUTH_HEADER=Authorization
ASSAYPILOT_LLM_AUTH_SCHEME=Bearer
ASSAYPILOT_LLM_RESPONSE_FORMAT=json_schema
ASSAYPILOT_LLM_TOKEN_PARAMETER=max_completion_tokens
ASSAYPILOT_LLM_TIMEOUT_SECONDS=60
ASSAYPILOT_LLM_MAX_OUTPUT_TOKENS=1200
ASSAYPILOT_LLM_RETRY_ATTEMPTS=1
```

`json_schema`는 provider가 strict JSON Schema response format을 지원할 때 사용한다. 미지원 provider는 해당 provider가 지원하는 `json_object` 또는 `prompt_only` 모드를 명시적으로 선택할 수 있으며, 이 경우에도 애플리케이션이 JSON과 DTO를 검증한다. token parameter도 `max_completion_tokens` 또는 `max_tokens`로 provider 사양에 맞춘다. OpenAI Chat Completions의 structured output 요청 형식은 [공식 API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)에 설명되어 있다.

```bash
cd /data1/miplab/wjyang/AssayPilot
# .env.stage5a.local에 실제 provider 값 입력 후 파일 권한 설정
chmod 600 .env.stage5a.local
conda run -n drug python -m assaypilot.scientific_reasoning_cli doctor
conda run -n drug python -m assaypilot.scientific_reasoning_cli run-public-pair
```

`doctor`는 key를 출력하지 않고 준비 여부와 비밀이 아닌 설정 metadata만 보고한다. `run-public-pair`는 작은 `{"status":"ok"}` 응답을 확인한 뒤 실제 공개 context의 초기/후속 판단을 호출한다. API 오류는 제한 재시도 후 끝나며 response body나 API key를 artifact에 저장하지 않는다. API 비용은 `synthetic_credit`과 별도이며 가격 정보가 없어 금액 추정은 하지 않는다.

현재 환경에서 설정 상태를 확인한 결과 `ASSAYPILOT_LLM_ENDPOINT`, `ASSAYPILOT_LLM_MODEL`, `ASSAYPILOT_LLM_API_KEY`가 없다. 따라서 위 live 명령은 아직 실행하지 않았다. 키를 채팅에 붙여넣지 말고 로컬 private env 파일 또는 secret 환경 변수로 설정한다.

## 검증 범위와 Stage 5-B 인계

`conda run -n drug python -m pytest tests/test_stage5a_reasoning.py -q`는 fixture·provider 요청 contract·validation을 검사한다. 실제 공개 bundle pair는 다음처럼 API 호출 없이 확인할 수 있다.

```bash
conda run -n drug python -m assaypilot.scientific_reasoning_cli run-public-pair --preview-only
```

Stage 5-B 연결 시에는 현재 `Selector.select(view: SelectorView) -> SelectorProposal | dict`와 `SelectorView`(`assaypilot.selector-view.v1`) 및 `SelectorProposal` DTO가 경계다. Stage 5-A 판단기는 `DecisionContext` 전체와 public evidence를 필요로 하며 기존 격리 selector worker는 네트워크·secret·evidence payload를 받지 않는다. 따라서 5-B는 trusted controller 경계에서 공개 view/evidence와 동일한 state version을 묶어 context를 만들고, `ScientificReasoner.decide(context)` 결과를 검증한 뒤 `SelectorProposal(kind="select", candidate_id=..., assay_id=..., reason=...)` 또는 `SelectorProposal(kind="stop", stop_reason="selector_stop")`로 변환해야 한다. controller의 현재 eligible-action, 승인, 예산 검증은 계속 최종 권한을 가진다. LLM 호출 중 공개 state가 바뀐 경우 stale context proposal은 사용하지 않는다. 5-A에는 이 연결이나 production selector 교체를 구현하지 않았다.
