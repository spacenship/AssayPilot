# 0단계 공통 데이터 규격과 모듈 인터페이스

## 목적과 의존 방향

`assaypilot.domain`은 표준 라이브러리와 Pydantic만 사용합니다. 향후 `data`, `models`, `agents`, `workflow`가 domain 타입과 Protocol을 import하며 domain이 구현 계층을 import하지 않습니다. 이번 코드는 형식·참조 정합성을 검증할 뿐 생물학적 타당성이나 실제 데이터 품질 감사를 완료하지 않습니다.

```text
향후 data / models / agents / workflow
                  ↓
             domain.protocols
                  ↓
      catalog / records / exchange / containers
                  ↓
                common
```

`validation`은 이 모델들을 입력받는 별도 순수 함수입니다. 어떤 함수도 데이터 조회, 예산 갱신 또는 선행조건 충족 여부에 따른 행동 승인을 실행하지 않습니다.

## 타입별 입출력

| 타입 | 입력·의미와 소비 지점 |
|---|---|
| Candidate | 내부 후보 ID, 원본 출처·ID·SMILES. 어댑터 출력 및 예측기 구조 입력 |
| AssaySpec | 시험 ID·이름·역할·endpoint·단위·판정 의미·선행조건·Cost. 예측기와 Governor의 시험 맥락 |
| Prerequisite | `observed`는 이전 시험 관측 존재, `verdict`는 특정 표준 판정 요구. 복수 조건은 향후 모두 충족하는 의미로 사용 |
| SuccessCondition | 시험별 `verdict` 또는 비교 연산자·수치·단위. 목표 선언이며 실행 엔진 없음 |
| CampaignSpec | 캠페인 목표, 선택적 표적·생물학 맥락, 성공 조건, 가정 여부를 가진 총예산 |
| EvidenceRef | 근거 ID, 출처 종류, 원본 ID, 위치. 관측·가설·계획·보고가 ID로 공유 |
| Observation | 후보·시험의 실측값, 비교·단위, 원본/표준 판정, 근거 ID, 공개 시점, 반복·조건 ID |
| Hypothesis | 내용, 지지/반박 근거 ID, 반증 조건, 근거에 대한 상태 |
| Plan | 가설·근거 ID, 구분 질문, 시험 ID, 이유, 선택적 중단/보류 사유. 후보 배치 없음 |
| ActionRequest | 캠페인·후보·시험과 JSON 매개변수. 승인 권한 없는 제안 |
| Prediction / PredictionBatch | 후보×시험의 명시적 사건 확률, 버전, 불확실성·보정 여부 또는 unavailable 사유 |
| ApprovedAction | ActionRequest 하나와 승인 ID·시점·이유. 승인 정보를 연결하는 값 타입 |
| GovernanceDecision | 행동 ID, approved/rejected, 사유. 승인일 때만 ApprovedAction |
| ExecutionReceipt | 접수 ID, 행동 ID, 접수 시점. 접수의 안정적인 식별 정보 |
| ExecutionResult / ExecutionError | 접수·행동 ID, pending/completed/failed, 완료 관측 또는 실패 코드·메시지·재시도 가능성 |
| BudgetState / Cost | Decimal 금액·단위. Cost는 설정값 가정 여부 포함, BudgetState는 total/spent/reserved |
| PublicCampaign | 캠페인·후보·시험·근거 카탈로그, 초기 공개 관측, 공개 기준 시점 |
| ObservationBatch | 캠페인 ID와 공개 관측 전송 묶음 |
| RunState | 캠페인 ID, 현재 시점, 현재까지의 공개 관측, 가설·계획, 승인된 진행 행동, 예산, 실행 상태 |
| AuditIssue / AuditResult | 대상 ID·필드 경로·코드·사유 목록, 계산 속성 ok |
| ReportStatement / Report | 근거 ID가 연결된 문장들과 캠페인 ID |
| DataSource | 어댑터 입력의 출처 종류·위치. 공개 컨테이너에는 저장하지 않음 |

공개 카탈로그의 정적 정의는 RunState에 중복 삽입하지 않습니다. PublicCampaign 관측은 어댑터의 초기 스냅샷이고 RunState 관측은 이후 현재 공개 집합입니다. 두 컨테이너를 서로 중첩하지 않으며 실행 중 최신 관측의 기준은 RunState입니다. 승인 행동은 RunState에 한 번만 저장하고 접수·결과는 행동 ID를 참조합니다. 조건의 구체 정의와 원본 레코드는 Observation의 EvidenceRef를 따라 확인하며 `condition_id`와 `replicate_id`는 식별자입니다.

## 판정과 가설

- `active`: 해당 시험의 활성 기준 충족. 확인시험의 active는 목표에 유리할 수 있지만 카운터의 active는 간섭을 뜻할 수 있습니다. 성공은 역할로 추론하지 않고 CampaignSpec의 시험별 조건으로 명시합니다.
- `inactive`: 해당 시험의 활성 기준 미충족. 미측정의 대체값이 아닙니다.
- `inconclusive`: 실측 판정이 불확실합니다.
- `unspecified`: 실측은 존재하지만 범주 판정이 제공되지 않았습니다. 수치만 있는 관측을 보존합니다.
- 미측정: Observation이 없습니다. 수치·원본 판정·표준 판정이 모두 없는 빈 관측은 거절합니다.
- 원본 판정에서 표준 판정을 자동 추정하지 않습니다. 출처별 매핑 정책은 향후 어댑터가 근거와 함께 명시해야 합니다.
- SMILES와 원본 ID는 공백 제거·정규화 없이 보존하며 문자열에 공백 외 문자가 있는지만 검사합니다. 화학적 유효성은 검증하지 않습니다.
- 가설 상태 `evidence_supported`는 근거상 지지를 뜻하며 임상 검증을 의미하지 않습니다. 일차 hit가 직접 결합이나 치료 효능을 증명하지 않습니다.

## 직렬화·비용·시점

상위 독립 전송 컨테이너 PublicCampaign, ObservationBatch, RunState, PredictionBatch, ExecutionReceipt, ExecutionResult, AuditResult, Report는 `schema_version="0.1.0"`만 허용합니다. 내부 값 객체는 상위 버전을 따릅니다.

금액 입력은 Decimal, 십진 문자열, 정수만 허용합니다. float와 bool은 거절해 이진 소수의 암묵 반올림을 피합니다. 음수·NaN·무한대는 거절합니다. Pydantic JSON에서 금액은 **문자열**로 직렬화됩니다(예: `"100.00"`). `model_dump_json()` → `model_validate_json()`으로 Decimal·Enum·ID·시간 의미를 보존합니다.

BudgetState의 `available`은 `total - spent - reserved` 계산 속성으로, 입력 필드나 JSON에 중복 저장하지 않습니다. 지출+예약은 총액 이하여야 합니다. 비용/캠페인 예산/현재 예산 단위는 정확히 같은 문자열이어야 하고, 현재 total은 캠페인의 설정 총액과 일치해야 합니다. 수치 관측과 성공 조건의 단위도 시험 단위와 일치해야 합니다. 자동 환산·예산 증액 정책은 없습니다.

모든 시점은 타임존을 요구합니다. 원본 UTC offset을 보존하며 시각 비교는 동일한 순간 기준입니다. 공개 관측의 released_at은 스냅샷 as_of 이하여야 합니다. RunState는 초기 카탈로그 시점 이후여야 합니다. 공개 기준 시각은 실험 발생 시각이 아닌 **시스템에 공개된 시각**입니다.

## 확률과 불확실성

0단계 예측 대상은 `target_meaning`으로 설명한 이진 사건의 확률입니다. `available`은 유한한 [0,1] 확률과 보정 여부를 요구합니다. `unavailable`은 사유를 요구하고 확률·불확실성·보정값을 허용하지 않습니다. 학습 전 임의의 0.5로 채우지 않습니다.

불확실성은 선택적이며 종류와 값을 함께 선언합니다. 모두 유한·비음수이고, Bernoulli 확률 변수 기준 `probability_stddev ≤ 0.5`, `probability_variance ≤ 0.25`, `entropy_nats ≤ ln(2)`입니다. 이는 허용 범위 검사이며 불확실성 추정·보정 품질을 검증하지 않습니다. 다른 회귀 출력이나 지표가 필요하면 향후 규격을 확장합니다. 예측값은 Observation으로 변환하지 않습니다.

## ID와 참조 검사

ID는 비어 있거나 공백만인 문자열을 거절하며 자동 변경하지 않습니다. 유일성은 **캠페인 안의 엔터티 종류별**입니다. 후보/시험/근거/관측/가설/계획/행동/승인 ID는 해당 묶음 내 중복을 거절합니다. 서로 다른 종류나 캠페인에서 같은 문자열을 사용할 수 있습니다. 후보의 원본 식별자는 출처와 함께 해석하며 내부 ID가 정체성의 기준입니다. 예측 키는 묶음 내 `(candidate_id, assay_id)`입니다. 복수 대상 의미·버전은 별도 PredictionBatch로 전달합니다.

관측은 반복·조건이 달라도 observation_id가 달라야 합니다. `(replicate_id, condition_id)`는 캠페인 전역 고유 ID가 아니며 같은 반복의 여러 측정 항목을 금지하지 않습니다. 근거/가설/제안 시험 ID 목록의 중복도 검사합니다. 접수 ID는 캠페인 실행 기록에서 고유하게 발급해야 합니다. 0단계는 단일 접수 계약이므로 여러 접수의 저장 이력 간 중복 및 재처리 멱등성은 이후 실행 계층 책임입니다.

| 순수 함수 | 검사 범위 |
|---|---|
| validate_public_campaign | 후보·시험·근거·관측 중복 및 참조, 성공 조건·선행 시험 참조, 자기/다중 순환, 단위, 공개 시점 |
| validate_run_state | 카탈로그 기준 관측·가설·계획·행동·승인 참조와 중복, 예산 일치, 상태/승인 시점 |
| validate_observation_batch | 캠페인·관측 참조 및 공개 기준 시점 |
| validate_predictions | 캠페인·후보·시험 참조와 예측 키 중복 |
| validate_execution | 행동→접수→결과 ID, 관측의 후보·시험 일치, 관측 ID 중복·근거·단위·시점 |
| validate_report | 캠페인과 문장별 근거 참조 |

모델 validator는 단일 객체의 상태별 필수 필드 등만 검사합니다. 참조 검사 실패는 예외 대신 AuditIssue 목록으로 모두 반환합니다. 모델 자체의 실패는 Pydantic ValidationError의 `loc`와 메시지로 확인합니다. 객체는 `extra="forbid"` 및 할당 검증을 사용하지만 list/dict 내부 수정은 자동 재검증되지 않습니다. 외부 입력은 모델로 다시 파싱하고 경계마다 순수 검사를 호출해야 합니다. `model_construct`나 검증 없는 `model_copy(update=...)`를 입력 검증 우회에 사용하면 안 됩니다.

완료 결과에는 비어 있지 않은 관측 목록만, 실패에는 오류 정보만, 대기에는 둘 다 없어야 합니다. 접수 상태를 Receipt에 중복 저장하지 않고 ExecutionResult의 스냅샷으로 유지합니다. 검사는 승인 권한의 진위, 저장 이력, 실제 실험 수행 여부를 인증하지 않습니다.

## 공개 데이터 경계와 모듈 연결

PublicCampaign과 RunState에는 봉인된 후속 라벨 전체, 정답 파일 경로, Oracle 내부 객체용 필드가 없습니다. 미선언 필드를 거절해 명시적인 공개 구조를 유지합니다. 다만 허용된 자유 텍스트·근거 위치에 비공개 정보가 들어가는 것까지 스키마가 판별하지는 않습니다. **스키마 분리가 실제 파일·도구 접근 격리를 보장하지 않으며 해당 격리는 2단계에서 다룹니다.**

| Protocol | 다음 구현의 입출력 |
|---|---|
| CampaignAdapter.load | DataSource → PublicCampaign. 1단계 데이터 어댑터 연결점 |
| Auditor.audit | PublicCampaign → AuditResult. 순수 정합성 함수 재사용 가능 |
| Predictor.fit | 후보 구조·시험 맥락·공개 ObservationBatch → None. 내부 학습만 수행 |
| Predictor.predict | 후보 구조·시험 맥락·공개 ObservationBatch → PredictionBatch. 지정 후보×시험 |
| Planner.plan | 공개 카탈로그·RunState·근거 → Plan |
| Selector.select | Plan·PredictionBatch·RunState → ActionRequest 목록 |
| Governor.review | 제안 행동·공개 카탈로그·RunState → GovernanceDecision |
| Executor.submit / collect | ApprovedAction → ExecutionReceipt → ExecutionResult |
| Reporter.render | 공개 카탈로그·RunState 기록 목록 → 근거 연결 Report |

Protocol은 타입 계약만 선언하며 본문은 `...`입니다. 런타임 인증이나 예산 권한을 Python 타입만으로 보장하지 않습니다. Planner와 Predictor 등은 공개 데이터로 읽기·제안을 수행하고 관측·예산의 영구 반영은 향후 실행 계층이 담당합니다. Report의 근거 ID 존재는 검사하지만 문장의 내용이 근거에 의해 과학적으로 지지되는지는 판별하지 않습니다.

## 다음 단계에 남긴 내용

1. 실제 데이터 어댑터, 출처별 표준 판정 매핑, 공개 데이터 구성.
2. ReplayOracle과 봉인 데이터의 파일·도구 접근 격리, 접수 이력 및 재처리 정책.
3. 예산 예약·차감·환불, 선행조건 런타임 판정 및 Governor 정책.
4. RDKit 특징, LightGBM 학습·불확실성·보정, 선택 정책, LLM 역할.
5. LangGraph 실행 루프, DB, 서비스 UI.

이번 예제는 두 합성 캠페인의 로딩·객체 생성·참조 검사·JSON 왕복만 수행합니다. 승인·접수 객체도 손으로 만든 합성 값이며 학습·예산 차감·Oracle 실행 결과가 아닙니다.
