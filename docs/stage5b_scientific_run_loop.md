# Stage 5-B 공개 근거 기반 실행 루프

## 목표와 실행 흐름

Stage 5-B는 Stage 5-A `ScientificReasoner`를 기존 replay controller의 `scientific_reasoner` 선택 모드로 연결한다. Controller가 현재 공개 상태를 읽고 판단 context를 만든다. 검증된 판단만 기존 승인·예산·실행·공개 경로로 전달한다.

```text
현재 공개 snapshot + 실행 상태 + budget
  → public DecisionContext / deterministic shortlist
  → ScientificReasoner 및 구조·참조·실행 가능성 검증
  → controller의 현재 state·eligible action·budget 재검증
  → 기존 approval, ExecutionCoordinator, replay release
  → 공개 observation/evidence
  → 다음 판단의 신규 해석 및 저장된 hypothesis 갱신
```

`fixed_order`와 `seeded_random_priority` 경로는 그대로 유지한다. scientific 판단의 오류나 검증 실패를 baseline selector로 조용히 대체하지 않는다. 기존 isolated selector worker에 API key, 네트워크 권한, evidence payload를 추가하지 않았다.

## 신뢰 경계와 데이터

`RunLoopController`의 trusted process만 provider credentials, runtime SQLite, replay execution 계층에 접근한다. `ExecutionCoordinator.get_scientific_loop_snapshot`에서 공개 view, 현재 execution status, public evidence 문서를 함께 얻고 같은 `public_state_version`을 사용해 `DecisionContext`를 구성한다. context에는 공개 campaign 후보·assay·Observation·Evidence, 공개된 execution attempt, 현재 eligible action, 실제 replay budget/한도, 검증되어 저장된 prior hypotheses만 들어간다. hidden Oracle 결과와 미공개 파일 내용은 prompt context 생성에 사용하지 않는다.

외부 provider에는 context DTO와 고정 system instruction으로 만든 구조화 요청만 전송한다. context에 포함된 원본 공개 SMILES는 후보 입력의 일부다. Provider 응답을 명령이나 도구 호출로 실행하지 않는다. 실행 제안은 `ScientificReasoner`가 공개 ID, reference scope, assay/candidate 관계, current eligibility, budget, prior 상태와 Observation/Evidence 참조를 검증한 뒤에만 controller로 돌아온다. 네트워크 요청 중 SQLite write transaction을 유지하지 않는다.

provider 설정은 `.env.stage5a.local`에서 읽지만 key 값과 인증 header는 configuration fingerprint 및 run artifacts에 저장하지 않는다. Private runtime에는 provider/model, endpoint metadata, request usage, 응답의 검증된 구조화 결과와 context가 저장된다. 실패 시 API call metadata에는 단계, 예외 클래스, 정제된 원인 코드와 하위 예외 클래스, HTTP 응답 수신 여부/status, latency를 기록한다. 원문 오류 문자열과 provider 오류 body는 보존하지 않는다. usage가 `{}`이면 provider가 사용량을 보고하지 않은 것이며 0 token을 뜻하지 않는다. `public_export` 형식 및 공개 범위는 바꾸지 않았다. `/runtime/stage5b/`는 `.gitignore` 대상이며 관리되는 경로의 디렉터리는 `700`, private DB와 artifact 파일은 `600` 권한을 사용한다. 지정 외부 경로를 `--runtime-db`로 전달하면 그 경로의 권한은 호출자가 관리한다.

## Shortlist와 Stage 5-A 보완

새 run의 shortlist는 해당 run의 현재 공개 eligible action 전체에서 후보를 추리고 seed와 SHA-256 규칙을 기록한다. 기본 설정은 size 24, seed 3이다. 이미 실행한 pair는 controller가 eligible action에서 제외한다. 새 Stage 5-B shortlist에는 Stage 5-A의 과거 후보 고정 anchor를 넘기지 않는다. pending public observation과 이전 public attempts의 후보는 해석 context에 포함될 수 있지만, 그것만으로 실행 가능 action을 복원하지 않는다.

Stage 5-A의 과학적 입력/출력 계약은 다음을 반영한다.

1. Full eligible action/candidate 수는 shortlist를 만든 시점의 분모로 기록한다. 해당 판단에 포함된 후보 수와 현재 가능한 action 수는 별도 값이다.
2. Seeded shortlist는 재현 가능한 입력 선택 규칙이다. LLM의 action 제안은 기록하고 추적할 수 있지만 같은 입력에서 같은 제안이 반복된다고 보장하지 않는다.
3. 설명은 “현재 제공된 활성 근거와 검증된 특징만으로는 confirmatory 우선순위를 정당화하기 어렵다”는 범위에 한정한다. 서로 다른 SMILES만으로 활성 우열, 구조 특징 또는 기전을 만들지 않는다.

이 내용은 `scientific_context.py`의 shortlist metadata, `scientific_reasoner.py`의 system instruction/decision validator와 Stage 5-A 문서에 반영했다.

## 구조화된 가설과 관측 갱신

가설은 `hypothesis_kind`로 의미를 구분한다. `assay_activity`는 특정 candidate-assay의 범주형 결과 예측이며 `expected_outcome`은 `active` 또는 `inactive`다. 선택한 pair에는 예상 결과가 `active`인 같은 pair의 활성 가설이 반드시 연결된다. 새 활성 가설은 검증 전 상태인 `proposed`로만 시작한다. `data_availability`는 결과 기록이 자료에 있는지에 대한 선택적 가설이며 `expected_outcome=null`이다. 가용성 가설은 활성 가설 요건을 대신하지 않는다.

가설의 구조화 필드와 자연어 문장은 현재 고정 문장 template을 사용해 서로 모순되지 않게 검증한다. `proposed`는 보정된 확률이나 확정된 예측이 아니다. 같은 candidate-assay의 공개 Active 결과는 Active 예상 가설을 지지할 수 있고, Inactive는 약화시킨다. Inconclusive/불명확 결과는 지지 근거가 되지 않는다. `no_record`, 실패, 거부는 활성 결과의 근거가 아니다. 상태 업데이트는 기존 hypothesis ID, 저장된 이전 상태, 새 공개 observation ID와 그 observation의 evidence를 함께 검증하고 transaction으로 저장한다. 가용성 가설 업데이트는 활성 가설 통계에서 분리한다.

## 해석 전용 단계와 전체 호출 상한

신규 run의 기본 `max_llm_calls`는 24이며 이는 run별 전체 API 호출 상한이다. 제공자 TPM 제한이나 항상 사용 가능한 호출량을 뜻하지 않는다. 정상 판단, 최대 1회의 구조 repair, stale 재판단, final interpretation 및 그 repair가 모두 같은 상한을 사용한다. 일반 행동 판단을 새로 시작하려면 일반 판단의 최대 2회와 final interpretation 예약 2회를 합쳐 최소 4회가 남아 있어야 한다. stale 재판단도 다음 요청 시작 시 같은 검사를 다시 받는다.

pending observation이 있고 새 행동 판단을 안전하게 시작할 여유가 없으면 selector는 `interpretation_only` context를 만든다. 이 context에는 미해석 공개 observation과 관련 public evidence·가설만 포함하고 `eligible_actions=[]`로 고정한다. Reasoner는 새 가설이나 실행 행동을 내지 않고 observation 해석 및 같은 범위의 prior update만 할 수 있다. 이 단계는 approval, Oracle 조회, replay 실행 또는 budget 차감을 하지 않는다. pending이 없으면 final API call을 만들지 않는다. 공개 결과 없이 종료한 run의 `interpretation_complete=true`는 처리할 observation이 없다는 뜻일 뿐 과학적 검증 성공이 아니다. 시간/API 제한 또는 해석 실패 시 pending ID와 미완료 사유를 보존한다.

## 예산·시간·API 호출 제한

`scientific_reasoner` run은 `assumed=false`인 명시적 replay budget을 요구한다. 각 context의 total, spent, reserved, available, unit 값은 controller의 현 상태에서 읽는다. `synthetic_credit`은 replay action 비용 단위이며 LLM API 요금과 합산하지 않는다. 별도 가격 정보가 없으므로 API 금액은 계산하지 않는다.

Run 설정에는 selector, shortlist 규칙/크기/seed, model/provider/endpoint metadata, API mode/response format, prompt 및 context schema version, output token limit, API timeout, call cap, stale 재판단 cap, 실행 수·budget·전체 deadline을 고정한다. 인증 정보는 settings fingerprint에서 제외한다. Resume 시 fingerprint가 달라지면 이어 실행을 거부한다.

각 API 요청 직전에 남은 전체 deadline과 요청 timeout을 비교해 더 짧은 값을 provider timeout으로 설정한다. 전체 deadline은 DB에 저장된 절대 `deadline_at`에서 이어지므로 resume할 때 초기화되지 않는다. Stage 5-A의 검증 repair는 최대 1회 재사용하며 repair와 stale 재판단도 전체 API call cap에 포함한다. Stage 5-B에서는 provider transport retry를 0으로 고정한다. Run 종료 뒤 pending public observation이 있으면 해석 미완료와 pending ID를 저장하고, 신규 실행 행동은 더 만들지 않는다. pending Observation이 없으면 불필요한 final call을 하지 않는다.

## 영속화 및 복구

Runtime SQLite가 권위 있는 run 상태다. `PRAGMA user_version` 5 schema에 config hash, loop steps, context/decision, API call metadata, current hypotheses, hypothesis event history, observation interpretations와 interpretation completion 상태를 저장한다. 기존 DB schema 0–4는 additive migration 경로를 사용하고, 이 범위 밖의 version은 `unsupported_database_version`으로 거부한다. Context schema는 `assaypilot.decision-context.v2`, decision schema는 `assaypilot.scientific-decision.v2`다. 이전 v1 JSON artifact/run은 보존하지만 v2 DTO에 없는 hypothesis 의미를 추측해 변환하지 않으며, 이전 scientific run 설정은 새 controller에서 자동 재개하지 않는다. v2 새 run에는 새로운 run ID와 config fingerprint를 사용한다.

판단마다 안정적인 run-local `decision_id`, state version, context digest와 context JSON을 저장한다. Validated prior updates, 신규 hypotheses, 신규 observation interpretations, decision 결과와 제안된 loop step은 같은 DB transaction에서 commit한다. Prior update의 `previous_status`가 현재 저장값과 다르거나 public references 검증에 실패하면 transaction 전체를 rollback한다. 이미 적용한 decision/hypothesis event는 unique constraint로 중복 반영을 막는다. 공개 observation의 해석 완료 상태와 미완료 이유도 DB에서 복구한다.

실행은 기존 action/request IDs와 coordinator idempotency를 재사용한다. API 응답을 받은 직후 영속 저장 전에 process가 종료되는 경우 provider를 다시 호출할 수 있으므로 외부 API exactly-once는 보장하지 않는다. Persisted decision의 재적용, duplicate action, 예산 중복 차감은 복구 계약에서 막는다. Private artifact는 종료 또는 일시 정지 때 SQLite에서 `rev-NNNNNN` 새 revision으로 export하며 이전 revision을 덮어쓰지 않는다. DB는 권위 상태, artifacts는 revision 시점의 감사용 사본이다.

## CLI

새 bounded run:

```bash
conda run -n drug python -m assaypilot.scientific_run_loop_cli start \
  --budget 5 --budget-unit synthetic_credit \
  --max-steps 10 --max-duration-seconds 300 --max-llm-calls 24 \
  --shortlist-size 24 --shortlist-seed 3
```

실행은 기본 공개 snapshot과 프로젝트 루트 `.env.stage5a.local`을 사용한다. 설정 경로 또는 snapshot을 바꾸려면 `--env-file` 또는 `--snapshot`을 명시한다. CLI의 실제 subcommands는 `start`, `resume`이다. 저장된 run을 재개할 때는 같은 snapshot, private DB, credentials 및 model/request settings를 사용한다.

```bash
conda run -n drug python -m assaypilot.scientific_run_loop_cli resume \
  --runtime-db /absolute/path/to/runtime/stage5b/<run-id>/private/execution.sqlite \
  --run-id <run-id>
```

출력은 `runtime/stage5b/<run-id>/artifacts/rev-NNNNNN/`에 비공개 저장된다. `contexts/`, `decisions.json`, `api_calls.json`, `execution_steps.json`, `hypotheses.json`, `hypothesis_history.json`, `interpretations.json`, `interpretation_status.json`, `configuration.json`, `summary.json`이 포함된다. `summary.json`의 `scientific_metrics`는 schema-valid/applied 또는 stale 판단, action/finalization/repair 호출, 실행/released/no_record 수, 해석·pending 상태, 가설 종류별 상태와 갱신, API 사용량 보고 여부 및 replay budget을 분리해 기록한다. 빈 API `usage`는 0 token으로 환산하지 않는다. DB에 기록하기 전 API 원문 body는 artifact로 저장하지 않는다.
