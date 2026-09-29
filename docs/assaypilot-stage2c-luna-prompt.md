# AssayPilot 2-C 구현 프롬프트 — Luna용

현재 AssayPilot 저장소에서 **2-C: 검증된 결과 공개·근거 등록·최종 과금·현재 공개 상태 갱신·접근 경계**를 구현해라. 기존 2-A/2-B를 재사용하고, 아래 선행 확인을 먼저 수행한다. 계획만 제시하지 말고 코드·검증·인계 문서까지 완료한다.

## 0. 범위와 원칙

- 목표: 2-B의 ready_for_release 실행 하나를 선택해 원본 후속 기록을 Observation과 EvidenceRef로 변환하고, 공개 상태와 과금을 동일 transaction에서 확정한다. 에이전트에는 commit된 공개 정보만 제공한다.
- 포함: 현재 RunState/공개 관측 관리, 실행 결과와 근거 검증, 공개 재시도·중복 방지, 예약→지출 확정, 명시적 공개 취소, 현재 공개 상태의 선행조건, 최소 공개 조회 인터페이스 및 실제 접근 경계 검증.
- 제외: LLM, 자동 승인·자동 후보 선택, 학습·성능 평가, 신규 실험, 웹 UI, 외부 서비스 배포, 일반 사용자 로그인 시스템, 분산 실행 플랫폼.
- 실제 AGENTS.md, git 상태와 기존 타입·validator·DB schema를 먼저 읽는다. 기존 사용자 변경과 snapshot은 보존하며 임의 reset/clean/commit/push를 하지 않는다.
- 기존 함수·필드·enum을 추측하지 않는다. 아래 이름은 역할 제안이며 실제 domain 계약에 맞춘다. 별도 범용 framework나 ORM을 도입하지 않는다.
- 전체 snapshot 재검증과 네트워크 호출을 각 공개 transaction 안에 넣지 않는다. 검증된 store/Oracle/coordinator는 초기화 시 적재하고 재사용한다.
- 실제 파일 접근 격리와 단순 Python 인터페이스 분리를 구분한다. 수행하지 못한 검증을 완료로 적지 않는다.

## 1. 선행 자료와 정확한 계약 확인

다음을 읽는다.

- src/assaypilot/execution.py, replay.py.
- domain의 Observation, EvidenceRef, ExecutionReceipt, ExecutionResult, PublicCampaign, RunState, ActionRequest, ApprovedAction, Cost/BudgetState 및 관련 validators.
- docs/stage2_execution_control.md, stage2_replay_lookup.md, stage2_data_handoff.md.
- reports/stage2/01_replay_lookup.md, 02_execution_control.md.
- tests/test_execution.py, tests/test_replay.py, 기존 객체 교체·시점·참조 검증 테스트.
- scripts/verify_execution_snapshots.py와 실제 snapshot 검증 결과.

기존 인터페이스는 보고서상 다음과 같다. 코드와 대조해 정확한 호출법을 문서에 기록한다.

```python
ExecutionCoordinator(database_path, public, oracle,
                     cost_policy_version=..., clock=..., busy_timeout_seconds=...)
initialize_run(run_id, initial_budget)
approve_action(run_id, action, *, approver_id, reason)
execute(run_id, request_id, action)
read_private_result(run_id, execution_id) -> ReplayLookupResult

validate_execution(action, receipt, result, public, *, as_of) -> AuditResult
```

2-B의 ExecutionReceiptView는 공개 domain ExecutionReceipt와 동일 타입이 아니다. 이름이 비슷하다는 이유로 그대로 대입하지 않는다. 공개 결과 생성에 필요한 **원래 실행의 승인·ActionRequest·시각·예약 비용**을 DB에서 검증해 읽는 내부 연결점을 마련한다.

### 1-A. 예산 검증 설명의 모호성 해소

2-B 보고서는 초기 예산과 행동 비용을 모두 1 설정 단위로 설명하면서, 같은 검증에서 records_found와 no_record를 실행했다고 기록했다. records_found가 먼저 전액을 reserve하면 뒤의 다른 행동은 잔액 부족으로 거절되어야 한다.

- verify_execution_snapshots.py에서 실제 실행 순서·run 분리·초기 예산을 확인한다.
- no_record 먼저 실행했거나 별도 run을 썼다면 정상이다. 순서와 run별 initial/spent/reserved/available을 보고서에 명시한다.
- 예산 1, 비용 1인 run에서 records_found 후 다른 행동을 실행하면 Oracle call_count가 증가하지 않고 InsufficientBudget 계열 오류로 거절되는지 확인한다. 결과가 no_record일 것을 미리 조회해 예산 검사를 우회해서는 안 된다.
- 이미 검증하는 테스트가 있으면 근거만 연결한다. 실제 오류가 있으면 2-B에서 최소 수정 후 회귀를 통과시키고 2-C로 진행한다.

### 1-B. 중복 요청과 실행 정체성

- 2-B는 같은 행동에 다른 request_id/action_id를 주어도 최초 execution의 receipt를 재사용할 수 있다. 2-C의 공개 ActionRequest/ExecutionReceipt/ExecutionResult는 **저장된 최초 execution의 action_id와 승인**을 기준으로 연결한다.
- 마지막 재요청의 action_id를 기존 결과에 덮어쓰지 않는다. 별도 request alias는 필요하면 이력에만 남긴다.
- 재전송 receipt의 예산은 과거 실행 직후 값일 수 있다. 이를 현재 잔액으로 사용하지 않는다. 현재 예산은 DB의 현재 상태로 조회하고 별도 getter로 명확히 제공한다.
- ready_for_release는 2-B lookup의 종료 상태이지만 전체 결과 공개 수명주기의 최종 상태는 아니다. 2-C에서 released/cancelled 등 실제 기존 타입에 맞는 후속 상태를 추가하고 문서를 정정한다.

## 2. 공개 상태와 시점의 모델

- 초기 PublicCampaign과 Stage 1 bundle은 불변으로 유지한다. 초기 as_of를 실행 시각으로 덮어쓰거나 기존 snapshot 파일에 새 관측을 추가하지 않는다.
- run별 현재 상태는 초기 공개 정보 + 공개 완료된 후속 관측/근거 + 현재 예산/실행 요약 + state_version 및 현재 공개 기준 시점으로 구성한다. 기존 RunState를 우선 활용한다.
- 미래 released_at을 가진 관측을 초기 as_of 검증에 억지로 통과시키지 않는다. 초기 snapshot validator는 초기 입력에, 현재 상태 validator는 명시적 현재 cutoff에 적용한다.
- 기존 validator가 현재 상태를 표현할 수 없으면 필요한 runtime validator/adapter만 추가한다. 미래 관측 검사를 삭제하거나 전역적으로 비활성화하지 않는다.
- 기존 validate_execution의 참조 검증에 새 근거가 필요하면, commit 전 만든 **임시 새 공개 상태**에 새 근거·관측을 넣고 검증한다. 실패하면 이 임시 객체를 버리며 기존 상태를 변경하지 않는다.
- 현재 시각은 주입된 timezone-aware clock에서 얻는다. 승인/실행 시각 이후이며 기존 현재 상태보다 과거가 아닌지 검증한다. 이전 값을 재사용하거나 초기 as_of로 위조하지 않는다.
- 후속 Observation.released_at은 최초 성공 공개 transaction에서 선택한 공개 시각이다. 재요청 시 이미 저장된 시각을 유지한다. 원본 실험 날짜를 이 값으로 채우지 않는다.

## 3. 결과 변환과 원본 근거

### 3-A. 공개 대상 제한

- 입력은 trusted 호출자가 주는 run_id와 execution_id다. 외부에서 관측값·판정·비용·파일 경로를 주입하지 않는다.
- 2-B read_private_result와 실행 이력을 사용해 같은 run/snapshot/campaign/후보/시험의 ready_for_release인지 검사한다. no_record/failed는 관측 공개 대상이 아니다.
- 동일 run 안에서도 다른 execution의 결과를 섞지 않는다. DB에서 읽은 결과를 ReplayLookupResult로 검증하고 관련 식별자·비용을 재확인한다.
- 한 실행에 연결된 모든 측정을 공개 단위로 유지한다. 여러 행을 평균·다수결·첫 행 선택으로 줄이지 않는다.

### 3-B. Observation/ExecutionResult

- measurement_id 하나마다 별도 Observation을 생성하고 원본 measurement_id와 대응을 보존한다. 여러 측정의 verdict가 충돌해도 각각 남긴다.
- raw_verdict와 내부 verdict, value/unit/comparison, 0·결측, 조건·반복의 not_reported 의미를 보존한다. 예측값을 실측값으로 표시하거나 counter active를 일률적 성공으로 바꾸지 않는다.
- ID는 run/execution/measurement에 기반한 안정적인 결정 규칙을 사용한다. 재시도 시 새 ID를 계속 만들지 않는다. 기존 domain ID 제약에 맞춘다.
- ExecutionReceipt/ExecutionResult는 실제 기존 필드와 validator에 맞춰 구성한다. 한 실행의 전체 비용을 매 Observation마다 중복 합산하지 않는다.
- 기계적 실행 성공과 assay active, 임상적 효과를 구분한다. 원본 데이터에 없는 수치·실험 날짜·binding 해석을 추가하지 않는다.

### 3-C. EvidenceRef와 공개 근거 payload

- 공개된 각 측정을 검증할 수 있도록 source row 식별자·원본 파일 SHA-256·SID/AID·원본 판정·필요한 최소 raw row·공식 protocol 위치를 공개 근거에 포함한다.
- 선택하지 않은 다른 행, 전체 normalized/hidden 파일, 전체 coverage/양성률, 내부 절대 경로, SQLite 위치, 비공개 오류 내용은 공개하지 않는다.
- 원본 raw_row를 전체 묶음과 혼동하지 않는다. 원본 행의 필요한 필드만 allowlist로 추출하고, 알 수 없는 비공개 텍스트 필드를 무조건 통째로 노출하지 않는다. 완전한 원본은 private result에 유지한다.
- evidence_id, payload bytes/hash, Observation.evidence_ids, EvidenceRef.location이 실제로 일치해야 한다. 문자열 참조만 만들고 접근 가능한 payload를 누락하지 않는다.
- 초기 근거와 새 근거를 모두 조회 가능한 작은 resolver를 둔다. location은 기존 허용 형식에 맞추고 run에 묶인 evidence ID를 해석한다. 임의 filesystem path/URL을 열어주는 generic resolver는 만들지 않는다.
- 공개 evidence hash는 실제 공개 payload bytes의 SHA-256이며, 원본 source_file_sha256과 구별한다.

## 4. 공개·과금·상태의 원자성

### 4-A. 저장의 기준

- 기존 private runtime SQLite를 유일한 권위 상태로 사용한다. 공개 관측·근거 payload·공개 receipt/result·state_version·ledger settlement·execution 공개 상태를 같은 DB transaction에서 저장한다.
- 에이전트용 getter는 commit된 공개 레코드만 명시적으로 선택해 DTO를 만든다. private DB 전체나 raw result 객체를 반환하지 않는다.
- DB commit과 public 디렉터리 파일 교체를 하나의 원자 연산이라고 가정하지 않는다. 필수 공개 경로는 DB에서 검증된 공개 DTO/근거를 제공하는 trusted getter로 구현한다. 파일 export는 이번 필수 범위가 아니다.
- export가 기존 계약상 꼭 필요하면 immutable revision으로 commit된 공개 상태에서 재생성 가능하게 만들고, 권위 상태와 캐시를 구분한다. 실패·복구 정책 없이 DB와 파일을 이중 권위로 만들지 않는다.
- DB schema를 확장할 때 기존 user_version=1과 2-B 데이터의 이관/호환을 명시한다. 기존 DB 삭제로 마이그레이션을 대신하지 않는다. 이관 자체도 transaction으로 보호한다.

### 4-B. 공개 transaction

1. BEGIN IMMEDIATE 또는 동등한 기존 locking으로 시작한다.
2. run/execution/current state를 다시 읽는다. 이미 released이면 저장된 공개 결과를 반환한다. cancelled/no_record/failed 또는 다른 run이면 거절한다.
3. ready_for_release의 원본 결과·원래 행동·승인·예약액을 확인한다. transaction 전에 읽은 데이터를 쓰는 경우에도 이 시점에 상태·version을 재확인한다.
4. 공개 시각과 안정 ID로 임시 Observation/EvidenceRef/ExecutionReceipt/ExecutionResult/새 RunState를 만든다.
5. 기존 객체 제약, 참조, validate_execution(..., as_of=현재 공개 시점), 현재 상태 validator, 예산 불변식을 모두 검증한다.
6. 검증된 관측·근거·현재 상태, execution의 released 상태, 해당 비용의 reserved 감소/spent 증가, 단일 settlement ledger를 함께 저장하고 commit한다.
7. commit 이후에만 공개 DTO를 반환한다. 메모리 캐시를 사용한다면 commit 후 갱신하고 DB에서 재구축 가능하게 한다.

- 총 예산은 변하지 않으며 reserved→spent 이동 때 available도 변하지 않는다. 0 비용에도 한 번의 공개 이벤트와 상태 전이를 기록한다.
- settlement는 execution별 고유 제약으로 한 번만 발생한다. 최신 전체 reserved를 해당 실행의 비용으로 오인하지 않는다.
- 여러 실행을 동시에 공개해도 각각의 관측이 남고 state_version이 증가해야 한다. 마지막 쓰기가 앞 관측을 지우지 않도록 transaction 안에서 최신 상태를 기준으로 합친다.
- 공개 validator 실패·증거 저장 실패·settlement 저장 실패·commit 전 장애는 전체 rollback한다. ready_for_release와 원래 예약을 유지하고 부분 관측·근거·지출을 남기지 않는다.
- 공개 오류를 2-B lookup failed로 변경하거나 자동 예약 해제하지 않는다. 일시 공개 실패는 같은 execution으로 다시 시도한다.
- commit 뒤 응답 전달 실패 시 동일 execution으로 재요청하면 같은 관측/근거 ID와 최초 공개 시각, 같은 과금 결과를 반환한다.

## 5. 명시적 취소와 예산 해제

- trusted 호출자의 별도 cancel_pending_release(run_id, execution_id, reason) 역할을 구현한다. 실제 함수 이름은 기존 구조에 맞춘다.
- ready_for_release에서만 cancelled로 전이하고 해당 예약만 한 번 해제한다. 관측·근거·spent를 추가하지 않는다. 취소 이유/시각을 내부 이력에 남긴다.
- 이미 cancelled이면 중복 해제 없이 같은 결과를 반환한다. released의 취소는 거절한다. 이미 공개된 결과를 지우거나 과금을 되돌리는 환불 시스템은 이번 범위가 아니다.
- release와 cancel이 동시에 요청돼도 한쪽만 성공해야 한다. 취소된 실행을 새 request_id로 다시 실행해 dedup을 우회하지 못하도록 2-B와 연결한다.
- 승인 취소(cancel_approval)와 공개 대기 실행 취소를 별개로 문서화한다.

## 6. 현재 공개 상태를 이용한 후속 실행

- 초기 snapshot/store binding은 그대로 두고, 2-B 승인·실행 시 선행조건 확인에는 해당 run의 최신 commit된 공개 관측을 사용하도록 연결한다.
- pending private result, 아직 commit되지 않은 Observation, 다른 run의 공개 결과는 선행조건 근거가 될 수 없다.
- 요청자가 임의 PublicCampaign/RunState를 넘겨 선행조건을 바꾸지 못하게 한다. trusted DB current-state provider를 사용한다.
- 2-B의 승인 digest·비용 정책·초기 snapshot fingerprint·중복 규칙은 유지한다. 실행 직전에 최신 공개 상태로 재검사하며 필요한 상태 version을 이력에 남긴다.
- fixture로 primary→confirmatory→추가 후속 assay 사슬을 구성해, 첫 결과가 private 상태일 때는 다음 행동을 거절하고 공개 후에만 허용하는지 확인한다. 실제 MEP2 snapshot에 존재하지 않는 assay를 추가해 실제 데이터인 것처럼 사용하지 않는다.

## 7. 최소 공개 인터페이스와 접근 격리

### 7-A. 공개 조회 인터페이스

- 최소 기능은 run의 현재 공개 상태, 현재 예산, 공개된 execution 결과, 공개 evidence payload 조회다. 실제 기존 DTO를 재사용하거나 명시적 필드 allowlist로 구성한다.
- 다른 run 접근, 공개 전 execution/evidence, 임의 경로 요청을 거절한다. run은 trusted 세션 생성 시 고정하고 agent가 run ID만 바꿔 다른 run을 읽게 하지 않는다.
- agent 인터페이스에서 approve_action/read_private_result/Oracle/DB connection/전체 snapshot inventory/후속 coverage를 노출하지 않는다.
- 이번에 자유 형식 shell, Python 실행, filesystem browsing, PubChem 직접 검색 도구를 에이전트에 제공하지 않는다. 공개 replay 정답을 외부에서 직접 얻는 경로도 열지 않는다.
- 인증 서비스나 웹 서버는 필요 없다. trusted 호출 예제와 최소한의 제한된 IPC/stdio 요청·응답 harness로 동작 경계를 검증할 수 있다. 요청 verb/필드/크기를 제한하고 오류 원문·내부 경로를 응답에 넣지 않는다.

### 7-B. 실제 파일/프로세스 경계

- 실행 환경에서 사용 가능한 컨테이너 또는 OS sandbox를 먼저 확인하고 한 가지를 선택한다. 이미 프로젝트 표준이 있으면 따르며 새로운 대규모 배포 체계는 만들지 않는다.
- 최소 위협 모델: agent 측 프로세스는 비신뢰로 취급하고, trusted coordinator/DB/snapshot은 다른 보호 경계에 둔다. 단순 subprocess 분리나 Python private 메서드는 접근 격리가 아니다.
- 예: 제한된 컨테이너/namespace 안의 reader에는 검증된 public 자료와 필요한 runtime만 제공하고, raw/curator/DB/프로젝트 전체/호스트 루트/컨테이너 관리 socket을 mount하지 않는다. 네트워크는 차단하고 trusted public 조회 채널만 허용한다.
- runtime DB 0600만으로 동일 UID agent를 차단했다고 주장하지 않는다. DB의 journal/WAL/SHM, 로그·임시 파일도 private 경계에 둔다.
- 실제 제한된 reader 프로세스에서 정상 public 조회는 성공하고, 알려진 private canary 파일 읽기·상위 경로·symlink 탈출·DB/curator 접근·허용하지 않은 IPC verb는 실패하는지 실행 검증한다. 부정 테스트를 단순 mock permission error로 대체하지 않는다.
- 호스트 canary는 fixture의 임시 비밀값이며 실제 자격증명을 읽거나 출력하지 않는다.
- 현재 환경이 sandbox 실행 권한/도구를 제공하지 않으면 권한을 우회하거나 일반 subprocess를 격리로 둔갑시키지 않는다. 가능한 공개·과금 구현과 인터페이스 검증은 끝내고, 재현 가능한 격리 실행 설정/명령과 미실행 이유를 남긴다. 이 경우 2-C 전체 완료가 아니라 ‘공개/과금 완료, 실제 격리 검증 대기’로 보고한다.

## 8. 검증 계획

기존 테스트를 먼저 확인하고 실제 요구사항을 검증하는 최소 테스트만 보완한다. 임시 DB와 작은 fixture로 먼저 확인한 뒤 실제 snapshot을 사용한다.

### 8-A. 공개·근거·시점

1. records_found 하나가 동일 원본 필드와 근거를 가진 공개 관측으로 변환되고 기존 validate_execution을 통과한다.
2. 여러 행/충돌 verdict/동일 CID의 다른 SID를 혼합하지 않고 보존한다. counter active/inconclusive/0/결측/비교 연산자/미지정 조건도 유지한다.
3. no_record/failed/cancelled/다른 run/execution은 공개 관측을 만들지 않는다.
4. EvidenceRef 실제 payload와 SHA-256이 일치하고 누락·변조·다른 후보 근거를 거절한다. 원본 파일 hash와 공개 payload hash를 혼동하지 않는다.
5. 초기 as_of는 유지되고 새 released_at은 현재 공개 시각이다. 과거·미래/naive 시각 등 계약 위반과 깨진 참조를 거절한다.
6. 새 request/action alias로 기존 실행을 재요청한 뒤 공개해도 원래 action/approval/receipt/result 참조가 일치한다.

### 8-B. 원자성·예산·재시작

7. 공개 성공: reserved 감소=spent 증가=해당 단일 행동 비용, total/available 불변. 여러 측정이어도 중복 과금 없음.
8. validator·근거 저장·ledger 저장·commit 전 실패 주입: 공개 레코드·과금·state_version 부분 변경이 없고 예약 유지.
9. commit 후 응답 유실과 DB 재오픈 후 재요청: 동일 관측/근거/시각, settlement 한 번.
10. 같은 실행 동시 공개, 서로 다른 실행 동시 공개, 공개와 취소의 경쟁에서 중복 과금/관측 유실/이중 예약 해제가 없음.
11. 취소 중복, released 취소 거절, 취소 뒤 새 request ID 우회 거절.
12. 기존 2-B DB를 실제로 생성한 후 새 schema로 여는 migration 테스트. 승인·예약·결과가 보존되고 미지원 미래 DB 버전은 거절.
13. 현재 예산 getter는 과거 receipt 예산과 구분되며, 최신 공개 선행조건 사슬 테스트를 통과.

### 8-C. 공개 범위·실제 격리

14. 공개 DTO·근거·오류·로그에 다른 후보 private 행, coverage, 원본 전체 파일, DB 경로가 포함되지 않음.
15. pending/released 각각에서 같은 공개 getter를 사용해 공개 시점 이전 정보가 보이지 않는지 확인.
16. 실제 sandbox reader의 public 정상 접근과 private canary/경로 탈출/DB/무허가 verb 접근 거절을 실행하고 결과를 분리 기록.

### 8-D. 실제 snapshot

```text
data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/
data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/
```

- 기존 원본을 네트워크로 갱신하지 않는다. public 입력 로드→store→coordinator→run→승인→실행→공개→공개 getter까지 최소 통합 검증을 수행한다.
- 개발자 검증용 기록 존재/부재 선택은 허용하지만 성능 평가나 모델 선택으로 표현하지 않는다. 예산이 한 행동 비용이면 no_record 먼저 실행하거나 별도 run을 사용한다. 기록 존재가 예약 중일 때 잔액 부족을 우회하지 않는다.
- records_found를 공개하면 runtime 공개 관측이 원본 측정 수만큼 증가하고 증거가 조회되며 spent가 한 행동 비용만 증가한다. no_record는 관측 수를 늘리지 않는다.
- 재공개/DB 재시작 후 동일 관측·과금 결과, 원본 snapshot public tree hash 불변을 확인한다. **runtime 공개 상태의 의도된 증가와 초기 snapshot 불변을 구분**한다.
- 원본 비용 assumed 속성을 유지하고 실제 실험비라고 설명하지 않는다.
- 1,682개 전부를 공개해 정답을 agent에 보여줄 필요는 없다. 필요한 실제 사례만 trusted verifier로 검증한다.
- 마지막에 0·1·2-A·2-B·2-C 전체 회귀를 실행한다. 실제 실행하지 않은 데이터/격리 검증은 명확히 미실행으로 기록한다.

## 9. 문서와 종료

- docs/stage2_result_release.md: 실제 함수·DTO·resolver 계약, 시점·ID, 상태 전이, 원자성, 예산 settlement/취소, migration, 현재 선행조건, 최소 공개 채널과 격리 사용법.
- reports/stage2/03_result_release.md: 2-B 예산 사례 확인 결과, 코드 변경, fixture/실제 snapshot/실제 격리 검증 구분, 명령·결과, 미해결 항목.
- stage2_execution_control.md와 stage2_data_handoff.md: ready_for_release 이후 released/cancelled 전이, 공개 시점·현재 상태·예산 getter, 초기 public과 runtime public의 구분을 반영.
- 코드·문서는 단계별 책임을 구분하고 실제 공개 인터페이스에 개발자 통계를 넣지 않는다. snapshot을 덮어쓰지 않는다.
- 최종 답변은 구현 요약, 검증 결과, 미완료 항목, 다음 단계가 호출할 정확한 공개/실행 연결점 순으로 작성한다. 실제 격리까지 검증됐는지 별도로 명시한다.
- 2-C에서 종료한다. 자동 선택 루프·LLM·학습·서비스 배포를 자동으로 시작하지 않는다.
