# AssayPilot Stage 3-C 구현 프롬프트 — Luna용

현재 저장소에서 **Stage 3-C: 보존된 기준선 실행을 평가하는 trusted offline evaluator**를 구현해라. Stage 3-B의 실행/복구/격리 경로는 유지한다. 이번 목표는 공개된 후속 assay 결과와 검증 가능한 평가 정답을 구분하여, 기준선의 발견 성과·기록 확보·비용·실행 안정성을 재현 가능하게 계산하는 것이다. 필요한 코드, fixture 검증, 실제 snapshot 평가, 문서와 보고서까지 수행한다.

## 0. 범위와 작업 원칙

- 포함: 3-B 인계 자료 점검, 필요한 공개 결과 보존 보완, 평가 계약/정답 연결/지표 함수/CLI, 두 snapshot 기준선 평가, 결과 JSON/표/곡선, 의미 있는 회귀 검증.
- 제외: 새로운 선택 전략, 활성 예측 모델 학습, LLM, UI/배포, 실제 실험, 성능 개선을 위한 seed/후보/예산 재탐색.
- 기존 3-B 비교는 fixed_order 1회와 seeded_random_priority seed 0–4이다. uninterrupted control은 복구 검증용이며 여섯 번째 무작위 기준선으로 세지 않는다.
- 기존 AGENTS.md와 git 상태를 확인하고 사용자 변경을 보존한다. snapshot/기존 plan/과거 보고서를 덮어쓰거나 무단 reset/clean/commit/push하지 않는다.
- 이름은 실제 코드에서 확인한다. 아래 모듈 이름은 제안이며 존재하지 않는 API를 가정하지 않는다.
- 사용 가능한 데이터에서 결론을 계산한다. 결과가 좋지 않거나 모든 run에서 신규 Active가 0이어도 정상적인 결과로 보존한다.

## 1. 먼저 읽고 확인할 자료

- docs/stage3_baseline_selectors.md, reports/stage3/02_baseline_selectors.md.
- docs/stage3_run_loop.md와 Stage 2의 replay/publication 계약.
- src/assaypilot/run_loop.py, selector_worker.py, execution.py, replay.py, public_api.py 및 실제 domain 모델.
- scripts/verify_stage3_baselines.py와 관련 테스트.
- reports/stage3/baselines/20260930-seeded-priority-v1/plan.json, execution_summary.json.
- 해당 디렉터리의 baseline 12개 및 recovery control 1개의 summary.json/trace.json. 문서 설명만 믿지 말고 실제 JSON 필드를 읽어라.
- 두 snapshot의 공개 assay 정의, Observation/EvidenceRef 계약, hidden manifest와 기존 ReplayStore의 데이터 검증 방식.

보고서에 따르면 기존 전체 테스트는 259개 통과했고, baseline 12개와 control 1개가 실제 격리 worker에서 실행됐다. 해당 과거 수치를 현재 검증 수치로 재사용하지 않는다.

이번 인계에서 특히 확인할 것:

1. plan 원본 bytes SHA-256과 execution_summary의 plan_sha256 일치.
2. planned baseline 12개와 실제 run 목록의 정확한 일치, seed/selector/revision/제한 일치.
3. 각 run의 step/unique execution/released/no_record/failed/Observation 수와 예산이 trace/summary에서 일치.
4. 각 released execution의 **실제 공개 Observation 값, 단위/qualifier, assay, EvidenceRef와 허용된 evidence payload**를 임시 DB 없이 다시 읽을 수 있는지.

문서상 trace에는 관측/evidence ID와 payload hash가 있고 private DB는 이미 삭제됐다. ID/hash만으로 Active/Inactive를 복원할 수 있다고 가정하지 않는다. 아래 보완 정책을 적용한다.

## 2. 3-C 시작 시 공개 결과 복원 가능성 보완

### 2-1. 공개 결과 보존이 이미 충분한 경우

실제 trace 또는 별도 공개 archive에 published Observation과 허용된 evidence payload가 있다면 기존 형식을 재사용한다. run/execution/Observation/EvidenceRef 연결과 payload hash를 검증하고 중복 export 포맷을 만들지 않는다.

### 2-2. ID/hash만 보존돼 있는 경우

3-B 실행 export에 **publication reader가 이미 공개한 결과만** 직렬화하는 작은 보완을 추가한다.

- 예: run별 published_results.json. schema version, run/revision, execution 및 action ID, 공개 Observation 전체 필드, EvidenceRef, 공개 payload, 기존 canonical 규칙의 payload hash를 담는다.
- private_result 전체 dump, 전체 hidden 배열, 아직 실행하지 않은 후보의 label/기록 존재 여부는 넣지 않는다.
- 이미 공개된 categorical 결과를 보존하는 것은 private 원본 전체를 노출하는 것과 다르다. 삭제된 DB에서 EvidenceRef ID만 남겨 접근 불가능하게 만들지 않는다.
- 비교적 작은 현재 baseline 규모에서는, 기존 seed/예산/max_steps/정책 그대로 **새 baseline revision에 12개를 다시 실행해 archive를 남기는 방법을 기본 선택**으로 한다. 새 구현 hash와 plan을 실행 전에 등록하고 기존 결과와 별도로 저장한다.
- 실행/선택 알고리즘은 그대로 유지한다. 동일 공개 피드백에서 이전 trace의 후보/assay 순서 및 운영 집계가 같아야 한다. 다르면 시간 종료 등 이유를 조사하고 새 실행을 과거 실행과 동일하다고 쓰지 않는다.
- 재실행은 새로운 실행으로 표시한다. 과거 ID/시각을 복사해 원래 실행인 것처럼 만들지 않는다. 기존 recovery 테스트는 회귀로 유지하며 control을 성과 표본에 넣지 않는다.

기존 hidden snapshot에서 과거 결과를 후처리로 읽는 것은 필요 시 가능하지만, 그것만으로 당시 결과가 공개됐음을 입증할 수는 없다. 과거 trace의 released 상태 및 동일 후보/assay/측정 식별자와 공개 payload hash까지 검증 가능한 경우에만 별도 recovered artifact로 보존하고 reconstruction 출처를 명시한다. 맞추기 위해 hash 검증을 생략하거나 공개 결과를 조용히 바꾸지 않는다. 복원이 불확실하면 위의 새 실행 방식을 택한다.

기존 자료가 불충분하더라도 evaluator와 fixture 검증까지 구현한다. 환경 문제로 필요한 실제 재실행을 못 하면 미완료 항목을 명시하고 평가 성공을 주장하지 않는다.

## 3. 평가 구조와 정보 경계

작은 모듈로 나누되 별도 서비스나 프레임워크를 만들지 않는다. 권장 책임 분리는 다음과 같다.

1. ArtifactLoader/Validator: plan·summary·trace·공개 결과 archive를 읽고 연결/해시/건수를 검사.
2. TrustedTruthAdapter: 고정 snapshot의 평가용 후속 기록을 읽고 출처와 label 해석 규칙을 제공.
3. MetricCalculator: 검증된 불변 입력에서 순수 함수로 지표/곡선 계산.
4. CLI/ReportWriter: 결과 JSON과 Markdown 표, 필요시 matplotlib 정적 곡선 저장.

실제 repository 관례에 맞춰 evaluation.py 또는 evaluation/ 패키지, evaluation_cli.py, tests/test_evaluation.py 등을 선택한다.

- evaluator는 종료된 실행의 offline 소비자다. 승인/execute/release/예산/Observation을 변경하지 않는다.
- hidden truth 접근은 evaluator의 trusted adapter 안에서만 허용한다. selector worker, SelectorView, loop의 eligibility 로직, 공개 archive 생성 경로로 전체 정답을 전달하지 않는다.
- 실행 성과는 실제로 **released된 결과**에서 집계한다. evaluator가 어떤 미공개/실패/취소 행동의 hidden 정답을 알더라도 그 run의 발견으로 세지 않는다.
- truth adapter는 새로운 도메인 의미를 발명하지 말고 기존 manifest/ReplayStore 검증과 source SID↔candidate 매핑을 재사용한다. CID만으로 다른 source SID를 합치지 않는다.
- evaluator 전용 hidden-source fingerprint를 기록한다. 이것은 평가 재현성용이며 selector seed/config/priority에 넣지 않는다.
- 과거 구현 fingerprint와 현재 evaluator fingerprint를 구분한다. 현재 코드가 과거와 달라졌다는 이유만으로 읽기 전용 평가를 거절하지 않는다. 과거 artifact의 저장된 hash와 입력 snapshot의 호환성은 검증한다.
- 모든 hash는 변경 탐지 수단이며 외부 서명이나 출처 인증으로 표현하지 않는다.

## 4. 평가 대상과 label 규칙을 명시

### 4-1. 무엇을 hit로 세는가

현재 campaign의 공개 assay 정의와 실제 normalized label을 읽어서 **평가 대상 후속 assay와 positive 의미**를 evaluation specification에 고정한다.

- 초기 primary-screen Active는 시작 시 알려진 조건이며 새로 발견한 후속 hit에 포함하지 않는다.
- 후속 assay의 명시적 Active를 positive로 세고 Inactive를 negative로 센다. Inconclusive/Unspecified/기타 불명확 label은 unknown으로 보존한다.
- source가 단일 농도 functional assay이면 “후속 assay Active”로 표현한다. direct binder, IC50 검증, 임상 효능으로 바꾸지 않는다.
- no_record는 missing replay record이며 Inactive가 아니다. failed/cancelled/unreleased 역시 negative가 아니다.
- 여러 assay를 생물학적 의미 확인 없이 합산하지 않는다. 기본 출력은 assay별이다.
- 하나의 candidate-assay에 여러 관측이 있을 때 positive 반복만으로 여러 hit를 세지 않는다. 기존 확정된 정규화 규칙을 우선 사용한다. 규칙이 없으면 충돌 label은 ambiguous로 분리하고 유리한 label을 임의 선택하지 않는다.
- source SID 기반 후보를 기준으로 중복을 제거한다. 별도 chemical compound 수준 집계는 매핑이 검증되고 필요할 때만 부가하며 기본 수치를 대체하지 않는다.

### 4-2. 정답을 아는 모집단

snapshot별, 평가 assay별로 다음 집합을 구분한다.

- U: 해당 평가 대상의 공개 후보/행동 모집단. 공개 assay 적용 규칙을 반영한다.
- K: U 중 snapshot에 해석 가능한 binary 후속 label이 있는 평가 단위.
- P: K 중 positive 단위. 신규 발견 recall용 분모에서는 시작 시 이미 동일 후속 결과가 공개된 단위를 제외한다.
- missing/ambiguous: U에서 binary label이 확정되지 않은 단위. negative에 합치지 않는다.

U/K/P, label conflict 수, missing 수는 trusted evaluator가 계산한다. selector에는 전달하지 않는다. 예전에 보고된 295/30 등의 수치를 상수로 쓰지 말고 실제 평가 snapshot에서 확인한다.

recall은 **이 snapshot에서 알려진 후속 positive에 대한 회수율**이다. 아직 측정되지 않은 후보까지 포함한 실제 모든 hit의 recall이라고 표현하지 않는다. 기록 유무가 무작위 누락이라고 가정하지 않는다. r2와 확장 snapshot은 별개의 표로 평가하고 독립 target 일반화 실험처럼 합산하지 않는다.

동적 prerequisite 때문에 접근할 수 없는 후속 단계가 포함되면 그 영향을 분모 설명에 명시한다. “전략이 찾은 것만”으로 분모를 줄이지 않는다. 이번에 지원할 수 없는 복잡한 assay 경로는 명시적으로 unsupported 처리한다.

## 5. 지표: 운영 결과와 과학적 결과를 구분

평가 단위는 기본적으로 `(candidate_id, evaluation_assay_id)`다. step별 중복 제거 후 다음 값을 계산한다.

### 5-1. 필수 운영 지표

- durable steps, unique executions, released/no_record/failed, rejected/pending(필드가 있으면), Observation 증가 수, stop_reason.
- initial/spent/reserved/available와 단위/assumed. 금액은 Decimal로 계산하고 JSON에는 문자열로 보존한다.
- replay record yield = released unique executions / completed unique executions. 0 분모는 null+사유.
- no-record fraction과 실제 실행 수를 함께 표시한다. synthetic_credit는 실제 실험비가 아니다. no_record 무료 정책하에서는 지출만으로 탐색량을 나타낼 수 없다.

### 5-2. 필수 발견 지표

- new_followup_positive_count: 종료까지 새로 공개된 고유 후속 Active 수 H.
- newly_labeled_count: 새로 공개된 고유 binary 결과 수 L; unknown 공개 수는 별도.
- observed_positive_fraction = H/L. 분모 0은 null. 이것은 관측 가능한 결과 중 양성 비율이며 전체 후보에 대한 precision이라고 부르지 않는다.
- known_positive_recall = H / |P_new|. H는 같은 평가 모집단/초기 공개 제외 규칙을 적용한다. |P_new|=0이면 null+no_known_new_positive.
- positives_per_credit = H/spent. 단위와 assumed를 표시하고 spent=0이면 null.
- first_positive_step, first_positive_spent: 최초 신규 positive 공개 시점의 실행 step과 누적 지출. 못 찾았으면 null이며 0/infinity/마지막 step으로 대신하지 않는다.

후속 Active의 후보/assay가 K/P와 모순되거나 공개 label이 동일 snapshot truth와 충돌하면 오류 또는 명시적인 검증 실패로 처리한다. 신뢰할 수 없는 run을 조용히 제외하고 평균을 내지 않는다.

### 5-3. 곡선과 비교 축

- step → 누적 신규 positive / 누적 공개 binary 수 / 누적 지출.
- spent → 누적 신규 positive. no_record로 같은 비용 좌표가 반복될 수 있으므로 계단형으로 처리하고 비용 Decimal 순서를 보존한다.
- 각 run의 실제 endpoint 운영 수치를 그대로 보인다. fixed는 30 step/4 credit에서 끝나고 random은 14–28 step/5 credit에서 끝날 수 있으므로 endpoint 차이를 전략 우위로 단정하지 않는다.
- 같은 step의 비교는 모든 비교 run이 실제로 도달한 공통 범위에서 수행한다. 이번 확장 자료의 공통 step은 최대 14이지만 실제 trace에서 재계산한다.
- 같은 실제 지출의 비교는 모든 run이 관측한 공통 비용 범위에서 수행한다. 이번 기존 자료에서는 최대 4 credit이며 실제 archive에서 재확인한다. 중간 비용에 정확한 점이 없으면 그 비용 이하의 마지막 상태를 사용하고 보간하지 않는다.
- 별도로 “최대 30 step, 최대 5 credit 정책 하 최종 결과”를 보여줄 수 있다. 이를 30번 실행 결과 또는 동일 5 credit 지출 결과라고 부르지 않는다.
- 종료 이후 미래 관측을 채워 넣지 않는다. early stop 이후 상태를 cap 기반 도표에 유지하면 실제로 추가 실행한 것이 아님을 표시하고 실제 공통-prefix 비교와 구분한다.
- ROC-AUC/PR-AUC는 ranking score와 적절한 binary 정답 집합이 없는 현재 기준선에서 계산하지 않는다. 일반적인 enrichment factor도 부분 label을 전체 음성으로 채워 계산하지 않는다. 이번 필수 범위에서는 위 지표로 충분하다.

### 5-4. seed 집계

- snapshot/assay별 fixed_order 1회 결과와 random seed 5개 개별 결과를 모두 제시한다.
- random은 유효 지표별 n, 평균, sample SD(ddof=1, n<2이면 null), min/max를 계산한다. null을 0으로 치환하지 않는다. 0-positive run도 정상 표본으로 유지한다.
- 5개 seed는 같은 campaign에서의 순서 변동이다. 독립 약물 표본/독립 target 5개로 취급하지 않는다.
- 유의성 검정/승자 선언은 이번 필수 범위에서 제외한다. fixed 결과를 5번 복제하여 표본 수를 늘리지 않는다.
- r2는 후보 5개 전부를 시도하므로 최종 집계가 같아도 정상이다. 순서 지표는 달라질 수 있다.

## 6. Artifact 검증 및 산출물

입력 검증:

- plan hash, snapshot/revision, run 집합과 역할, selector kind/seed/version, 대상 assay, 초기 예산 및 정책 일치.
- step 순서/ID 참조, execution 중복, released→공개 Observation/evidence 연결, payload hash와 source mapping.
- 요청 별칭/retry/resume을 새 execution/hit로 중복 집계하지 않는다.
- 예산 합계, 음수 금지, trace checkpoint와 최종 summary 대조. 추가 action/settlement를 evaluator가 생성하지 않는다.
- 잘못된 archive 연결, 변조, 누락을 경고 한 줄로 무시하고 hit 계산하지 않는다. affected run의 evaluation_status/error를 남기고 전체 비교는 incomplete로 표시한다.
- 경로는 지정한 artifact root/snapshot root 안에서 실제로 resolve한다. trace에 든 임의 절대 경로를 그대로 열지 않는다. 기존 안전한 resolver를 재사용한다.

평가를 시작하기 전에 버전이 있는 evaluation_spec.json을 저장한다. baseline plan hash, artifact hashes, public 및 hidden-source fingerprint, 평가 assay/label/conflict 규칙/집계 단위/분모/공통 비교 축, evaluator 구현 hash, 입력 run 목록을 담는다. 이는 사후 평가 규칙의 고정이며 과거 실행 전 과학적 가설 사전 등록이라고 주장하지 않는다.

권장 출력:

- reports/stage3/evaluations/<evaluation-id>/evaluation_spec.json
- validation.json: run별 무결성/호환성 검증 상태와 이유.
- run_metrics.json: 개별 성과, 명시적인 분모/단위/null reason.
- curves.json: step 및 비용 축의 원시 계단형 자료.
- aggregate_metrics.json: seed 집계와 공통 범위 비교.
- comparison.md: 결과 표, label/partial-data/예산 해석 한계.
- figures/: 필요하면 두 축의 cumulative positive PNG/SVG. 0 값 결과도 그대로 표시.

전체 hidden label 매핑은 공개 trace에 쓰지 않는다. evaluator에 필요한 데이터는 메모리 또는 명확히 구분한 개발자 전용 입력에서만 사용하고 보고서에는 aggregate와 이미 공개된 근거만 남긴다.

기존 결과 디렉터리는 덮어쓰지 않는다. 평가 반복 실행은 새로운 output 디렉터리에서 수행하고 시각/출력 경로를 제외한 지표의 동일성을 검증한다.

## 7. 검증 순서와 완료 조건

### 7-1. 손으로 계산 가능한 fixture

작은 공개 캠페인과 후속 assay를 만든다. 초기 primary Active, 후속 Active/Inactive, missing, ambiguous를 각각 포함하고 결과 공개 순서를 명시한다.

1. known 후속 positive가 2개인데 하나만 새로 공개되면 H=1, recall=1/2. 다른 미공개 positive를 evaluator가 알아도 H에는 포함되지 않는다.
2. 새 binary 공개가 Active 1/Inactive 1이면 H/L=1/2. no_record/ambiguous를 분모에 넣지 않는다.
3. 초기 primary Active 및 시작부터 공개된 동일 후속 positive를 신규 hit에서 제외한다.
4. retry/alias/resume/동일 관측 반복에도 unique hit와 execution이 중복되지 않는다.
5. label 없는 기록, 충돌 label, positive 분모 0, 미발견, 0 지출을 각각 null/unknown 규칙대로 처리한다. 분모 0을 임의 1로 대체하지 않는다.
6. 서로 다른 assay positive를 섞지 않고 SID가 다른 후보를 CID로 합치지 않는다.
7. step/비용 축의 early stop, 같은 비용 좌표, 공통 비교 범위, 최초 positive 시점을 손계산과 대조한다.
8. 계획/trace/archive의 hash 불일치, 누락 EvidenceRef, 잘못된 run 연결을 거절한다.

### 7-2. 정보 경계와 비파괴성

9. evaluator 실행 전후 snapshot/기존 artifacts hash 불변, 실행 이력/예산 변화 없음.
10. 미공개 후보의 evaluator-only 정답을 바꾼 fixture에서는 recall 분모만 달라질 수 있고 실제 공개 positive 수/기존 trace/selector 입력은 달라지지 않는다. released label 불일치는 별도 검증 오류다.
11. 기존 selector hidden 독립성과 실제 격리 검증이 계속 통과한다. evaluator truth adapter가 worker에서 import/호출되는 경로를 만들지 않는다.
12. 보존 공개 archive만으로 run의 공개 결과와 evidence를 읽을 수 있고, trusted truth 입력 없이도 운영 지표와 공개 결과 기반 H/L을 계산할 수 있다. recall 등 truth 의존 지표만 명시적으로 unavailable이다.

### 7-3. 실제 자료

13. 원래 12개 자료가 충분하면 그것을 평가한다. 불충분하면 2절에 따라 새 revision에서 동일 조건 실행 및 archive 생성 후 평가한다. report에 어떤 경로를 택했는지 기록한다.
14. snapshot별 6개 baseline 전부 평가하고 recovery control은 성과 집계에서 제외한다.
15. source-normalized categorical label과 공개 Observation의 매핑을 실제 공개 positive/negative/unknown 사례가 존재하는 범위에서 확인한다. 해당 label 사례가 없으면 fixture 검증과 구분한다.
16. 생성한 JSON을 다시 읽어 표/곡선/분모/집계 일치와 반복 평가 결정성을 확인한다.
17. 마지막으로 기존 회귀와 새 evaluator 테스트를 실행한다. 실제로 실행한 명령·pass/fail·미실행 항목을 기록한다. 수치/그림을 임의 생성하지 않는다.

## 8. 문서 및 최종 보고

- docs/stage3_evaluation.md: 구조, 입력/출력 계약, label/분모 정의, metric 수식, artifact 무결성, 공개/hidden 경계, CLI 예시.
- reports/stage3/03_evaluation.md: 변경 파일, 인계 보완 여부, fixture/실제 평가/회귀 결과, baseline 개별 표 및 seed 집계, 한계와 미완료 항목.
- 필요하면 3-B export 문서를 갱신하되 과거 결과를 새 실행 결과로 덮어쓰지 않는다.
- 최종 답변 순서: 구현 요약 → 3-B 인계 점검/보완 → 실제 성과 표 → 검증 결과 → 남은 한계 → 다음 모델 선택기를 동일 조건으로 평가할 연결점.
- Stage 3-C에서 종료한다. 학습 모델/LLM/배포까지 자동 확장하지 않는다.

완료 기준은 “좋은 성능 수치”가 아니라, **실제로 공개된 결과만 성과로 세고, 부분적으로만 알려진 정답과 비용 제약을 명시하며, 누구나 같은 artifact에서 같은 평가 수치를 재생성할 수 있는 상태**다.
