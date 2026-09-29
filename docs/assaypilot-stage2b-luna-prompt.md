# AssayPilot 2-B 구현 프롬프트 — Luna용

현재 저장소에서 **2-B: 행동 승인·예산 예약·실행 이력·중복 실행 방지**를 구현해라. 이미 구현한 2-A ReplayOracle을 내부 호출하고, 후속 2-C가 결과를 공개할 수 있는 상태까지 저장한다. 계획만 설명하지 말고 구현, 필요한 테스트, 실제 snapshot 검증, 인계 문서까지 완료한다.

## 0. 단계 경계와 작업 원칙

- 2-A의 실제 인터페이스는 다음과 같다. 실제 코드를 읽고 일치하는지 확인한다.

```python
from assaypilot.replay import ReplayOracle, load_replay_store
store = load_replay_store(snapshot_root, initial_public_campaign)
oracle = ReplayOracle(store)
result = oracle.lookup(candidate_id, assay_id)
```

- snapshot/store/oracle은 신뢰된 실행기 초기화 시 한 번 적재하고 재사용한다. 행동마다 모든 원본을 다시 해시하거나 store를 다시 만들지 않는다. 개별 lookup은 승인·선행조건·예산 검증 뒤에만 호출한다.
- 이번에 할 일: 실행 단위 초기화, 명시적 승인 기록, 행동 검증, Decimal 예산 예약/해제, 내부 lookup, 비공개 결과 저장, 실행 이력, 재시도/중복 요청 처리, 재시작 후 상태 복원.
- 이번에 하지 않을 일: Observation/EvidenceRef 생성·공개, released_at 부여, PublicCampaign/RunState의 관측 갱신, 최종 공개에 따른 비용 확정, OS/컨테이너 접근 격리, LLM/학습/자동 선택 루프, UI/API 서버, 실제 실험.
- 이번의 records_found는 **공개 대기**다. 실험 완료·공개 완료·생물학적 성공과 구분한다.
- 기존 AGENTS.md, git 상태, domain 계약·테스트를 먼저 읽는다. 기존 사용자 변경·snapshot을 보존하고 임의 reset/clean/commit/push를 하지 않는다.
- 아래 이름은 설계 제안이다. 실제 기존 타입·필드·validator를 우선 재사용하고, 동일 개념을 중복 정의하지 않는다. domain 타입 변경은 필요한 최소 범위만 한다.
- 새 플랫폼, ORM, 메시지 큐, 분산 락을 도입하지 않는다. 기존 영속화 계층이 없으면 표준 라이브러리 sqlite3 한 DB를 사용한다. DB는 비공개 runtime 경로에 둔다.
- 신뢰된 Python 내부 호출을 위한 구현이다. 모듈 분리를 실제 파일 접근 격리나 외부 사용자 인증이 완성된 것으로 표현하지 않는다.

## 1. 시작 전 확인 — 이미 완료한 일을 다시 만들지 말 것

다음을 읽고 실제 코드·테스트와 대조한다.

- docs/stage2_replay_lookup.md, docs/stage2_data_handoff.md.
- reports/stage2/01_replay_lookup.md 및 Stage 1 완료 보고서.
- src/assaypilot/replay.py, domain 모델·protocols·validator, 기존 돈/비용/승인/Action/RunState 계약.
- tests/test_replay.py, 단계별 실행·시점·예산 관련 기존 테스트.

2-A 보고에는 선행 Stage 1 테스트 6개, replay 테스트 24개, 전체 165개 통과와 두 snapshot 실제 검증이 기록돼 있다. 이를 현재 테스트 결과로 사칭하지 말고 필요한 검증을 현재 코드에서 실행한다.

보고서만으로 명시되지 않은 아래 부분을 기존 코드와 테스트에서 확인한다. 이미 검증되면 테스트 이름만 보고서에 연결하고, 실제 공백이 있으면 작은 fixture 테스트와 필요한 최소 수정만 수행한다.

1. ReplayLookupResult는 records_found인데 measurements가 비어 있는 상태와 no_record인데 measurements가 있는 상태를 둘 다 거절한다.
2. Oracle 조회를 통과한 counter active 의미, inconclusive, 결측, 지원되는 부등호 비교값이 변형되지 않는다. 수치 0/등호 보존 기존 테스트와 중복하지 않는다.
3. public 로딩 경로는 여전히 curator와 새 runtime DB를 읽지 않는다.

정확한 기존 Action/승인/비용/시점 validator 시그니처를 문서에 적고 난 뒤 구현한다. 검증 함수를 통과시키기 위해 가짜 Observation이나 빈 성공 결과를 만들지 않는다. 기존 validator가 실제 공개 이후에만 적용되는 함수라면 2-B에서 재사용 가능한 부분과 2-C에서 적용할 부분을 구분한다.

## 2. 이번 실행 정책 — 임의로 다른 정책을 만들지 말 것

### 2-1. 행동과 실행 단위

- 이번에 지원할 행동은 공개 candidate_id와 지원 후속 assay_id에 대한 고정 snapshot replay 조회다. primary 조회, 신규 반복 실험, 자유 형식 파일 경로, 임의 SQL/도구 실행은 지원하지 않는다.
- 한 행동은 그 후보×시험에 연결된 모든 측정을 받는 한 번의 replay 조회다. 반환 레코드 수만큼 비용을 곱하지 않는다.
- 동일 run과 snapshot에서 같은 후보×시험을 여러 request_id로 제출해도 동일 행동으로 인식한다. 근거 문장·제출 시각·모델 이름만 바꿔 별도 과금 가능한 행동을 만들지 않는다.
- 다른 run은 독립 평가다. 다른 snapshot과 campaign을 같은 run에 섞지 않는다.
- run 초기화에는 initial PublicCampaign, snapshot 식별/검증 정보, 초기 예산과 비용 단위를 명시한다. 기본 무제한 예산이나 자동 승인으로 대체하지 않는다.
- 비용은 초기 공개 시험 정의 또는 명시적으로 version 고정한 실행 비용 정책에서 가져온다. 요청자가 제출한 임의 비용을 권위값으로 사용하지 않는다. snapshot의 비용이 가정값이면 실제 실험 견적이라고 부르지 않는다.

### 2-2. 승인

- 승인 발급은 신뢰된 호출자의 명시적인 함수 호출이다. 테스트용 approver 이름은 신원 인증을 구현한 것이 아니다. 요청 payload의 approved=true만으로 승인하지 않는다.
- 승인 기록은 run, snapshot, 정확한 행동 내용, 비용/단위 및 비용 정책 버전에 묶는다. 기존 계약의 승인 시점·만료·상태 필드가 있으면 그대로 검사한다.
- 행동 내용과 비용을 canonical하게 직렬화한 digest 등으로 승인 대상 변경을 감지한다. 부동소수점 변환 없이 Decimal 문자열 규칙을 일관되게 사용한다.
- 승인 없는 행동, 거절·취소된 승인, 다른 run/후보/시험/비용의 승인을 거절한다. 거절은 Oracle 호출과 예산 예약 전에 끝나야 한다.
- 승인 자체가 자금을 확보하는 것은 아니다. 실행 직전 가용 예산과 공개된 선행조건을 다시 검증한다.
- 현재 단계에서 초기 공개 관측이 충족하는 선행조건만 판단할 수 있다. 비공개 결과로 선행조건을 만족시켜서는 안 된다. 이후 2-C의 검증된 현재 공개 상태를 연결할 지점을 남긴다.

### 2-3. 예산과 결과 공개 전 상태

다음 보존식을 사용한다. 기존 domain에 동등한 계약이 있으면 용어를 맞춘다.

```text
available = initial_budget - spent - reserved
initial_budget >= 0, spent >= 0, reserved >= 0, available >= 0
```

- 금액은 처음부터 끝까지 Decimal이며 DB/JSON에서는 문자열로 보존한다. float 합산, SQLite REAL 컬럼/SQL SUM에 의한 금액 계산, 암묵적 단위 변환을 사용하지 않는다.
- NaN/Infinity/음수 비용/잘못된 단위·통화 및 bool을 금액으로 받지 않는다. 0 비용과 예산이 정확히 일치하는 경계는 허용하고 검증한다.
- 실행 전 비용을 reserve한다. 승인된 요청이라도 다른 행동의 예약 때문에 예산이 부족하면 Oracle을 호출하지 않는다.
- records_found: 비공개 결과를 저장하고 상태를 ready_for_release로 만든다. 예약은 유지하고 spent는 증가시키지 않는다.
- no_record: 결과 부재를 실행 이력에 남기고 예약을 전액 해제한다. assay 비용은 0으로 확정하며 Observation을 만들지 않는다.
- 알려진 lookup 오류: 부재와 다른 실패 상태/오류 코드로 기록하고 예약을 해제한다. 내부 raw 자료나 경로를 일반 응답 오류 문자열에 노출하지 않는다.
- 최종 과금 확정은 2-C에서 관측·근거 공개와 일관되게 처리할 책임이다. 이번 단계에서 성공 조회만으로 spent를 늘리거나 별도 commit_charge API를 공개해 조기 과금 경로를 만들지 않는다.
- 예산 집계와 ledger의 예약/해제 기록은 하나의 DB transaction에서 함께 반영한다. 실패해서 상태와 예산 중 하나만 남는 상황을 허용하지 않는다.

## 3. 상태·저장 구조와 연결점

최소한 다음 논리 구성을 둔다. 기존 타입·테이블로 충분하면 새로 만들지 않는다.

| 구성 | 역할 |
|---|---|
| Run context | snapshot/campaign/초기 예산·정책을 고정 |
| Action/approval record | 행동 내용, digest, 승인 상태·주체·시각 |
| Execution record | 실행 ID, request ID 대응, 상태, 요청 시각/실행 시각, 오류 코드 |
| Budget ledger | 예약·해제 이벤트와 고유 키, 예산 불변식 |
| Private result | 검증된 ReplayLookupResult와 measurement 원본, 2-C 인계용 |

- request_id/idempotency_key는 같은 run 안에서 같은 payload에만 재사용 가능하다. 다른 payload로 재사용하면 conflict 오류다.
- 별도로 run/snapshot/후보/시험/지원 행동 종류에 대한 고유 제약을 두어 새 request ID로 동일 행동을 중복 실행하지 못하게 한다. request key와 행동 key의 역할을 구분한다.
- 저장 완료된 ready_for_release/no_record/failed에 동일 요청을 재전송하면 기존 영수증을 반환한다. Oracle 재호출·추가 예약·새 결과 저장을 하지 않는다.
- 이번에는 terminal failed의 자동 재실행이나 신규 반복 측정을 지원하지 않는다. 이를 명시하고, 재시도라는 이름으로 새 request ID를 주어 우회하지 않는다. DB transaction 자체가 rollback되어 실행 기록이 없는 요청은 다시 실행할 수 있다.
- 2-B 조회 영수증은 execution_id, 공개 가능한 실행 상태, 예약/가용 예산 등 최소 정보만 반환한다. raw measurements, verdict, 전체 coverage, curator 파일 위치를 포함하지 않는다.
- 원본 결과 접근은 별도의 신뢰된 2-C용 내부 메서드로 제한한다. receipt 생성에 내부 객체 model_dump()를 통째로 사용하지 않는다.
- 보존 result를 다시 읽을 때 2-A 타입으로 검증하고 요청 run/snapshot/후보/시험과 일치하는지 검사한다. 가변 객체를 반환하면 방어적 복사를 적용한다.
- 실행 이력의 상태명은 approved/ready_for_release/no_record/failed 등을 기존 타입에 맞춰 정한다. 공개되지 않은 결과를 completed 또는 released로 표시하지 않는다.

## 4. 트랜잭션·동시 요청·재시작

현재 Oracle은 메모리 내 read-only lookup이며 실제 네트워크/실험 부작용이 없다. 이 범위에서는 과도한 분산 실행 설계 없이 다음 방식으로 원자성을 확보할 수 있다.

1. snapshot과 Oracle은 DB transaction 밖의 서비스 초기화에서 적재한다.
2. 실행 요청 시 sqlite BEGIN IMMEDIATE 또는 기존 동등한 transaction/locking으로 상태 변경을 직렬화한다.
3. 기존 request/action 실행을 확인하고 승인·선행조건·비용·잔액을 검증한다.
4. 예약을 기록하고 이미 적재된 Oracle.lookup을 호출한다.
5. 결과·상태·예약 유지/해제·이력을 함께 저장한 뒤 commit한다.

- 위 transaction 안에 전체 snapshot 해시 검증, PubChem fetch, LLM, 장시간 파일 분석을 넣지 않는다.
- 예상 가능한 ReplayRequestError 등은 failed와 예약 해제로 기록한다. DB 쓰기 오류나 예상 못한 중간 예외는 transaction 전체를 rollback하고 오류를 전파한다. 실패를 no_record로 바꾸지 않는다.
- 이 구현에서 실행 도중 프로세스가 죽으면 미완료 transaction이 rollback된다. 같은 요청을 다시 보내면 read-only lookup을 다시 수행해도 되지만 이중 예약·과금·결과 저장은 없어야 한다.
- 이를 “프로세스 장애에도 Oracle 함수가 정확히 한 번만 호출됨”이라고 주장하지 않는다. 보장할 것은 저장된 실행 결과와 예산 효과의 중복 방지다.
- DB 재오픈 후 기존 승인·실행·예약·비공개 결과를 복원한다. 프로세스 메모리 set만으로 중복을 막지 않는다.
- SQLite 잠금 경합에는 유한 busy timeout과 명확한 오류를 사용한다. 같은 key를 두 connection에서 동시에 제출해도 실행 기록과 예산 효과가 하나인지 검증한다.
- 이후 실제 외부 실험을 수행할 때는 별도의 durable outbox/작업 상태 설계가 필요함을 문서에만 적는다. 이번에 구현하지 않는다.

## 5. 시점과 데이터 경계

- 초기 PublicCampaign.as_of는 변경하지 않는다. 요청 시각·승인 시각·실행 시각은 별도 필드로 두고 timezone-aware 값으로 검증한다. 테스트에서는 주입 가능한 고정 시계를 사용한다.
- 요청 이후 승인, 승인 이후 실행의 순서를 기존 계약에 맞춰 검사한다. 미래 승인·만료 등 존재하는 제약을 우회하지 않는다.
- lookup 결과에 released_at을 미리 부여하지 않는다. stage2A 데이터와 snapshot bytes는 그대로 둔다.
- runtime DB, ledger와 raw result는 public/ 밖에 둔다. PublicBundleAdapter의 의존성과 입력을 바꾸지 않는다.
- 접근 제어는 아직 신뢰된 실행기 경계 안의 인터페이스 분리다. OS/컨테이너 격리는 2-C에 남긴다.

## 6. 검증 — 순서대로 구현하며 확인

### 6-1. 승인·요청 검증

- 정상 승인 요청, 미승인, 다른 후보/시험/run/snapshot 승인, 행동/비용 변조, 거절·취소 상태를 검사한다.
- 공개 선행조건 미충족, unknown candidate/assay, primary 조회, 잘못된 시각을 거절한다.
- 거절 시 Spy Oracle의 call_count가 0이고 예산이 그대로인지 확인한다. 비공개 결과를 조회한 다음 거절하는 구현은 허용하지 않는다.

### 6-2. 예산·상태

- records_found: 한 행동 비용만 예약, spent=0, ready_for_release, 모든 연결 측정 비공개 보존.
- no_record: 예약 해제, spent=0, 기록 부재 이력, 가짜 inactive 없음.
- 알려진 lookup 오류: failed와 예약 해제, no_record와 다른 오류 코드.
- 서로 다른 두 행동이 예산을 경쟁할 때 기존 예약을 차감한 available로 판단.
- 비용 0, 정확히 남은 예산과 같은 비용, 부족 예산, Decimal 소수 정밀도·문자열 roundtrip, 음수·NaN·Infinity·bool·단위 불일치를 검사.
- 승인 후 다른 행동이 잔액을 바꾼 경우 실행 시 다시 검사하는지 확인.

### 6-3. 중복·transaction·복원

- 같은 request ID + 같은 내용: 동일 execution, Oracle 추가 호출 없음.
- 같은 request ID + 다른 내용: conflict, 상태/예산 변경 없음.
- 다른 request ID + 같은 행동: 기존 실행 재사용 또는 명시적 중복 응답, 중복 lookup·예약 없음. 선택한 동작을 문서화.
- 다른 run의 같은 행동: 독립적인 예산과 실행.
- 두 SQLite connection의 동시 동일 행동, 동시 잔액 경쟁에서 고유 제약과 예산 불변식 유지.
- 예약 뒤·lookup 뒤·비공개 결과 저장 중·commit 전의 쓰기 실패를 주입해 부분 상태가 남지 않는지 검사. 꼭 필요한 실패 지점을 선정하고 중복 테스트를 피한다.
- commit 뒤 응답 전달 실패를 모사하고 같은 request 재전송 시 저장 결과를 재사용하는지 검사.
- DB를 닫고 다시 열어 승인·예약·terminal 실행·private result가 복원되고 이중 실행이 없는지 확인.
- receipt/공개 상태에 verdict/raw_row/다른 후보의 결과/coverage가 없고 public 파일과 관측은 불변인지 확인.

### 6-4. 실제 snapshot 통합 검증

먼저 실제 경로 존재 여부를 확인한다. 네트워크로 원본을 갱신하지 않는다.

```text
data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/
data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/
```

- 개발자용 임시 runtime DB를 만들어 run을 생성한다. 초기 예산과 비용의 출처를 기록하고 임의 실험 가격을 사실처럼 쓰지 않는다.
- 각 snapshot에서 기록 존재 행동과 기록 부재 행동을 확인하여 명시적으로 승인 후 실행한다. 이 선택은 검증용이며 모델 선택·성능 평가가 아니다.
- records_found에서 ready_for_release와 예약 유지, no_record에서 예약 해제, 재요청의 동일 execution, DB 재오픈 후 동일 상태를 확인한다.
- 실제 결과의 측정 ID·필드가 원래 Oracle.lookup 반환과 일치하는지 비공개로 대조한다.
- 실행 전후 public tree hash와 초기 PublicCampaign/Observation이 동일한지 확인한다. 공개 결과·released_at은 새로 만들지 않는다.
- 이번 범위에서 1,682개를 모두 승인/예약할 필요는 없다. 필요한 실제 사례와 fixture로 확인한다. 원본 전체 검증은 기존 스크립트를 재사용한다.
- 실제 자료/환경이 없으면 fixture 검증은 끝내되 실제 검증을 미실행으로 보고한다. 통과 기록을 작성하지 않는다.
- 마지막에 기존 0·1·2-A와 새 2-B 전체 회귀를 실행한다. 테스트 개수는 완료 기준이 아니며 실제 실행 결과만 기록한다.

## 7. 산출물과 종료

기존 구조에 맞는 작은 모듈로 구현하고, 최소한 다음 문서를 남긴다.

- docs/stage2_execution_control.md: 실제 함수 시그니처, run 초기화→승인→실행→receipt→내부 결과 조회 예시, 상태 전이, 예산 보존식, 중복·transaction 정책, 오류, 2-C 인계 계약.
- reports/stage2/02_execution_control.md: 선행 검증 결과, 변경 파일, fixture/실제 검증 구분, 테스트 명령·결과, 기존 snapshot 불변성, 미구현 경계.

2-C 인계에는 아래를 명시한다.

1. ready_for_release 실행과 비공개 결과를 읽는 정확한 메서드.
2. 관측·근거 공개가 검증된 뒤 reserved→spent를 한 번만 확정해야 한다는 계약.
3. 공개 실패 시 관측 일부/예산 일부만 남지 않도록 해야 한다는 계약. 일시 실패에서는 예약을 유지해 동일 execution으로 재시도하며, 명시적 영구 취소 시에만 해제할 정책을 이후 구현한다.
4. 2-B의 no_record/failed를 공개 실험 판정으로 변환하면 안 된다는 점.
5. 실제 접근 격리와 현재 공개 상태의 갱신·검증이 아직 남아 있다는 점.

최종 답변은 핵심 구현, 검증, 미해결 사항, 2-C 시작 연결점 순으로 정리한다. 문제가 없으면 2-B 종료를 명시하되, 승인된 결과가 이미 에이전트에 공개됐다고 표현하지 않는다. 2-C를 자동 구현하지 않는다.
