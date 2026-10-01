# AssayPilot Stage 3-A 구현 프롬프트 — Luna용

현재 저장소에서 **Stage 3-A: 공개 정보 기반 최소 자동 실행 루프**를 구현해라. 기존 2-A/2-B/2-C의 조회·승인·예약·결과 공개·과금 기능을 연결하고, 여러 행동을 제한 안에서 실행하며 중단 후 재개할 수 있게 한다. 계획만 제시하지 말고 코드·검증·실행 예제·보고서까지 완료한다.

## 0. 목표와 범위

이번 목표는 다음 순환의 실제 동작이다.

```text
현재 공개 상태 → 실행 가능한 행동 → 선택 제안 → trusted 정책 검증/승인
→ 기존 execute → ready_for_release이면 기존 release_result
→ 최신 공개 상태·시도 이력 반영 → 종료 조건 검사 → 다음 선택
```

- 포함: 선택기 입출력 계약, 공개 정보 기반 행동 열거, 고정 순서 최소 선택기 하나, trusted 반복 실행기, 종료 조건, 진행 기록·복구, CLI, 실제 snapshot 검증.
- 제외: 무작위·다양성 등 여러 기준선 비교(3-B), 정답 기반 성능 평가·통계(3-C), ML 학습, LLM, 문헌 검색, UI, 외부 서비스 배포, 실제 실험.
- 고정 순서 선택기는 연결 검증용이다. 이를 학습 모델·지능적 최적화·신약 효능 검증이라고 표현하지 않는다.
- 기존 AGENTS.md와 사용자 변경을 보존한다. snapshot 덮어쓰기, git reset/clean, 무단 commit/push를 하지 않는다.
- 실제 모델·필드·메서드를 먼저 읽고 재사용한다. 아래 제안 이름은 실제 계약과 충돌하면 조정한다. 새로운 범용 에이전트 프레임워크, ORM, 분산 스케줄러는 도입하지 않는다.
- 단일 호스트·run당 단일 loop worker가 이번 지원 범위다. 기존 DB transaction·중복 실행 방지·공개 원자성을 다시 만들지 않는다.

## 1. 기존 코드 확인과 짧은 선행 검증

다음을 먼저 읽는다.

- docs/stage2_result_release.md, stage2_execution_control.md, stage2_data_handoff.md.
- reports/stage2/03_result_release.md 및 최신 보완 보고서.
- execution.py, public_api.py, replay.py와 domain Action/RunState/조건/예산 validator.
- tests/test_execution.py와 기존 public stdio/실제 sandbox launcher.
- scripts/verify_result_release_snapshots.py.

보고서상 연결점은 다음과 같다. 실제 시그니처를 확인하고 사용한다.

```python
coordinator.public_reader(run_id)
coordinator.approve_action(run_id, action, approver_id=..., reason=...)
coordinator.execute(run_id, request_id, action)
coordinator.release_result(run_id, execution_id)
coordinator.get_public_state(run_id)
coordinator.get_current_budget(run_id)
```

선행 점검은 아래 세 항목으로 제한한다. 이미 구현·검증됐으면 테스트 이름과 결과만 연결한다.

1. get_public_state가 관측·근거·예산·version을 일관된 DB 읽기 snapshot으로 반환하는지 확인한다. 공개 commit과 동시 조회에도 혼합 상태가 없도록 필요한 경우 읽기 transaction을 보완한다.
2. 최초 공개 시각은 승인·실행 시각과 run의 기존 공개 기준 시점보다 과거일 수 없고 timezone-aware여야 한다. clock 역행을 거절하는 테스트가 없으면 최소 보완한다. 재공개는 최초 시각을 유지한다.
3. 보존해야 할 실제 2-B v1 DB가 있으면 복사본으로 migration을 확인한다. 없다면 기존 v1 fixture 검증 범위를 명시하고 이 항목 때문에 3-A를 멈추지 않는다. 실제 DB를 검증한 것처럼 보고하지 않는다.

이후 필요한 API 계약과 책임을 짧게 문서화한 뒤 구현한다. 213개 통과는 이전 보고 값이며 현재 실행 결과로 복사하지 않는다.

## 2. 선택기와 trusted 실행기를 분리

### 2-1. 선택기 입력과 출력

- 기존 Selector Protocol이 있으면 재사용한다. 없다면 작고 명시적인 select(view) → proposal 또는 stop 계약을 만든다.
- 입력은 JSON 직렬화 가능한 공개 DTO다. 공개 후보/시험 정보, 필요한 공개 관측, 현재 예산, 현재 가능한 행동, 이전에 자신이 시도한 행동의 안전한 요약만 포함한다.
- 선택기에 coordinator/Oracle/store/DB connection/파일 경로/curator 객체를 전달하지 않는다. 이미 공개되지 않은 측정 존재 여부도 입력하지 않는다.
- 출력은 허용된 candidate_id, assay_id와 짧은 선택 사유 또는 명시적 중단 사유다. 승인 여부·비용·run_id·DB 경로·측정값을 선택기가 정하지 못하게 한다.
- run은 trusted 실행기가 고정한다. action_id/request_id는 선택 결과를 기록할 때 trusted 실행기가 생성하고 재시작에도 유지한다.
- 선택 사유는 “안정 정렬에서 첫 번째 실행 가능 행동”처럼 실제 규칙을 설명한다. 생물학적 근거나 모델 예측을 꾸며 쓰지 않는다.

### 2-2. 최소 고정 순서 선택기

- 실행 가능 행동을 `(candidate_id, assay_id)`의 명시적 안정 정렬로 정렬하고 첫 항목을 선택한다.
- 입력 배열의 우연한 순서나 Python hash/set 순서에 의존하지 않는다.
- 후보/시험 이름, MEP2, KLF5, AID 번호, 295개 후속 후보 목록을 구현에 고정하지 않는다.
- 같은 초기 입력과 정책·동일 중간 공개 결과에서는 같은 행동 순서가 재현돼야 한다. UUID·wall-clock timestamp까지 동일할 필요는 없다.
- 3-B에서 선택기만 교체할 수 있게 하고, 이번에는 랜덤·학습 기반 선택기를 추가하지 않는다.

## 3. 실행 가능 행동과 최소 선택용 상태

- 초기 공개 후보 전체와 공개 후속 assay 정의에서 행동을 만든다. 초기 후보를 hidden follow-up이 있는 295개로 줄이지 않는다.
- 조건은 공개된 선행조건, 현재 가용 예산, 지원 행동 종류, 이미 실행/처리한 행동 여부다. 기록이 존재하는지를 Oracle에 미리 물어보지 않는다.
- 기존 validator/조건 계산 함수를 공유한다. 가능한 행동을 만든 시점과 실제 실행 시점 사이 상태가 달라질 수 있으므로 execute 직전 authoritative 재검증은 유지한다.
- 같은 run에서 released/no_record/failed/cancelled인 행동을 다시 선택하지 않는다. ready_for_release는 신규 선택 후보에서 제외하고 복구 대상으로 처리한다.
- no_record는 이 snapshot의 기록 부재이며 inactive 관측이 아니다. 정책 선택기가 알 수 있는 것은 이미 시도한 자기 행동의 부재 결과뿐이다. 시도하지 않은 후보의 availability 목록은 노출하지 않는다.
- 기존 PublicReader는 공개된 execution만 읽는다. loop 복구에 필요한 pending/private 상태는 trusted controller가 읽고, 선택기에 필요한 attempted-action 요약만 명시적 allowlist DTO로 내보낸다. 공개 execution 조회를 private 결과 조회로 확장하지 않는다.
- 전체 state 약 1.60 MB를 매번 근거 payload와 함께 선택기에 보내지 않는다. 초기 고정 catalog를 한 번 제공하고 필요한 현재 필드만 투영하거나, 간단한 최소 선택용 DTO를 만든다. 복잡한 페이지/캐시 서버는 필요 없다.
- 관측 state_version만으로 예산/시도 이력까지 같다고 가정하지 않는다. 선택용 view는 같은 DB 읽기 경계의 최신 예산·시도 상태를 포함하고, 판단 당시 version/필요하면 view digest를 기록한다.
- 공개되지 않은 결과를 바꾸어도 초기 선택용 view와 첫 선택이 바뀌지 않는지 fixture로 검사한다.

## 4. 명시적인 실행 정책과 반복 루프

### 4-1. 시작 설정

필수 또는 명시적인 설정으로 run_id, snapshot, runtime DB, 초기 예산·단위, cost policy, 선택기 종류, max_steps, max_duration_seconds, 유한 오류/공개 재시도 상한을 받는다. 금액은 기존 Money/Decimal 계약을 사용한다.

- 예산을 자동 무제한으로 만들지 않는다. 자동 반복 실행은 `bounded_replay` 같은 명시적 trusted 승인 정책을 선택한 경우에만 수행한다.
- 정책은 공개 선행조건 충족·허용 후속 assay·예산·행동 제한을 검사하고 매 행동마다 실제 approve_action을 호출한다. 선택기가 자신을 승인하거나 approved=True 플래그로 우회하게 하지 않는다.
- 정책의 approver_id는 설정된 시스템 정책 주체이며 실제 사람의 검토·외부 인증을 가장하지 않는다.
- 이 정책은 오프라인 replay에만 적용한다. 실제 wet-lab/API 비용 승인으로 확대하지 않는다.
- 같은 run_id의 재초기화로 예산이나 비용 정책을 바꾸지 않는다. resume은 기존 설정 fingerprint와 DB를 확인하며 충돌하면 거절한다.

### 4-2. 한 스텝

1. 이전에 저장한 미완료 loop step/실행을 먼저 조정·복구한다. 복구가 끝나기 전 새 행동을 선택하지 않는다.
2. 일관된 공개 선택용 view와 실행 가능한 행동을 계산한다.
3. 종료 조건을 검사하고 선택기를 호출한다. 선택기 출력 크기·스키마·허용 행동 여부를 검사한다.
4. 선택 결과와 고정 action_id/request_id, view 식별자, step 번호를 durable하게 저장한다. 이후 승인/실행 중 죽어도 같은 요청으로 복구한다.
5. trusted 정책으로 검증하고 명시적으로 승인한 뒤 기존 execute를 호출한다.
6. ready_for_release이면 같은 execution으로 release_result를 호출한다. no_record/failed이면 실험 관측을 만들지 않고 기존 실행 상태를 loop 이력에 연결한다.
7. 완료된 스텝을 기록하고 최신 공개 상태·예산·시도 이력으로 다음 단계에 진입한다.

- 실행 사유·오류는 간결한 코드와 실제 사실로 남긴다. 내부 예외 원문·SQL·private 측정은 선택기에 보내지 않는다.
- 입력이 잘못되거나 선행조건이 바뀌어 거절됐을 때 같은 행동을 무한히 재제안하지 않는다. 기본은 명확한 이유로 loop를 중단하고, 재시도 가능한 일시 오류만 같은 step/ID로 유한 재시도한다.
- records_found를 임상적 성공으로 해석하지 않는다. confirmatory inactive도 정상적으로 관측 공개 및 비용 확정되는 결과다.

## 5. 종료·오류·재시작 정책

### 5-1. 종료 조건

다음을 구별한 stop_reason을 남긴다.

- max_steps 도달.
- 실행 시간 한도 도달.
- 새로 실행할 수 있는 행동 없음: 모든 행동 처리 완료 또는 남은 공개 선행조건 미충족.
- 선행조건은 맞지만 가용 예산으로 충당할 행동이 없음. 0 비용 행동이 있으면 예산이 0이라는 이유만으로 중단하지 않는다.
- 선택기의 명시적 중단.
- 정책/입력/비정상 선택기 오류 또는 유한 재시도 소진.
- 사용자 interrupt: 재개 가능한 중단.

- max_steps는 **durable하게 기록한 신규 선택 step 수**다. no_record와 정상적으로 기록된 거절/실패도 제한을 소비하며, 같은 step의 복구 재시도는 추가 step으로 세지 않는다.
- 별도로 실제 고유 실행 수·공개 실행 수·관측 수·재시도 수를 기록한다. 이 수들을 하나의 “실험 횟수”로 합치지 않는다.
- 프로세스 내 시간 제한은 monotonic clock을 사용한다. resume으로 시간 제한을 무제한 초기화하지 않도록 최초 시작 기준 UTC deadline도 보존하고, 중단 시간 포함 여부를 문서화한다. 기본은 최초 deadline 유지다.
- 이미 진행 중인 짧은 로컬 transaction을 강제로 중간 종료하지 않는다. 다음 행동 시작 전에 deadline을 검사하고, pending 공개 복구에만 유한한 별도 마무리 시도를 허용한다.
- 공개가 계속 실패하면 pending reservation을 자동 취소하지 말고 resumable 오류로 중단한다. 명시적 취소는 기존 cancel_pending_release를 통해 별도로 수행한다.

### 5-2. 복구와 중복 방지

- loop 상태는 기존 private runtime DB에 작은 테이블/기존 저장 구조로 보존한다. 새 테이블이 필요하면 기존 user_version을 확인하고 호환 migration을 작성한다. snapshot은 수정하지 않는다.
- coordinator가 승인/실행/공개 상태의 권위 원본이다. loop 체크포인트의 추정 상태보다 기존 execution의 실제 상태를 우선한다.
- 승인 후·execute commit 후·release commit 후 loop 기록 직전에 중단된 경우를 모두 처리한다. 같은 action/request/execution으로 재호출하고 기존 멱등성을 사용한다.
- release된 실행을 과거 receipt의 ready_for_release만 보고 새로 과금하거나 재선택하지 않는다. 원래 execution과 published result를 확인한다.
- 승인 단계 재시도가 새 승인으로 기존 최초 승인 이력을 덮어쓰지 않는지 확인한다. 필요하면 기존 승인 조회/재사용 연결점을 최소 추가한다.
- 같은 run에 두 loop worker를 동시에 띄우지 못하게 한다. 단일 호스트의 OS file lock 등 단순한 방식으로 막고 프로세스 종료 시 자동 해제되도록 한다. 여러 coordinator connection 동시성 검증을 loop worker 동시 실행 허용으로 오해하지 않는다.
- loop lock을 잡은 채로 전체 실행 동안 DB 쓰기 transaction을 유지하지 않는다. 선택기 실행 중 DB transaction을 열어두지 않는다.

## 6. 실제 선택기 경계 재사용

- 기존 2-C의 제한된 broker/namespace 경계를 재사용해 최소 선택기 프로세스와 trusted 실행기를 연결한다. 새 분산 시스템이나 로그인 서버를 만들지 않는다.
- 선택기는 공개 JSON DTO를 받아 작은 proposal JSON만 출력한다. private DB/curator/raw/host root/관리 socket/네트워크 접근은 허용하지 않는다.
- 기존 BusyBox public-reader 테스트의 성공을 새 Python 선택기의 격리 성공으로 대신하지 않는다. 실제 사용할 선택기 launcher에서 public 입력→proposal 성공과 private canary 접근 실패를 확인한다.
- selector가 객체 참조로 coordinator에 접근하는 in-process 구현은 fixture 편의용으로만 사용하고 실제 격리 실행으로 보고하지 않는다.
- 환경에서 실제 launcher를 실행할 수 없으면 인터페이스·loop 검증은 마치고 실제 격리 미실행 사유를 남긴다. 조용히 비격리 실행으로 fallback하지 않는다.
- sandbox 내부에 필요한 Python/runtime만 제공한다. 승인·실행·공개 메서드는 부모 trusted controller에서만 호출한다.

## 7. CLI와 실행 기록

프로젝트 기존 CLI 구조에 맞는 start/resume 명령을 추가한다. 실제 확정된 명령을 docs에 기록한다. 새로운 웹 서버는 필요 없다.

- 시작 예제: snapshot, private DB, 새 run ID, 명시 예산/단위, bounded replay 정책, fixed_order, max_steps, 시간 제한 지정.
- 재개 예제: 같은 DB/run을 사용해 저장된 config와 미완료 step을 확인 후 resume. resume이 새 run을 만들면 안 된다.
- 기본 실행은 제한적이고 명시적이어야 하며 예제만 복사해 1,682개 전체를 모두 공개하지 않게 한다.
- step별 기록: step/action/request/execution ID, 선택 당시 공개 view 식별자, 선택 사유, 승인/실행/공개 상태, 관측 증가 수, 최신 spent/reserved/available, stop/error code. raw private 결과는 제외한다.
- 최종 요약: 선택 step 수, 고유 실행 수, released/no_record/failed/거절 수, 관측 증가 수, pending 수, budget, 종료 이유, resume 가능 여부.
- 모든 기록은 runtime/개발자용 산출물로 분리하고 초기 bundle에 쓰지 않는다. 실험 판정 통계·성능 곡선·정답 기반 recall은 3-C에서 구현한다.

## 8. 검증 순서와 완료 조건

### 8-1. 작은 fixture

1. fixed_order가 같은 공개 입력에서 같은 순서를 내고 hidden 결과 변형이 초기 view/첫 선택에 영향을 주지 않는다.
2. 여러 step의 승인→execute→release가 기존 경로로 동작한다. pending 결과만으로 후속 조건이 열리지 않고 commit된 공개 결과로만 열린다.
3. no_record/failed가 관측으로 변하지 않고 다시 선택되지 않는다. 전부 no_record인 fixture도 max_steps/행동 소진으로 유한 종료한다.
4. 0 비용, 정확히 맞는 예산, 잔액 부족, 선행조건 미충족, 후보 소진, 선택기 중단, 시간 제한을 구별한다.
5. 미등록 후보/시험, 다른 run 시도, malformed/과대 proposal, selector timeout에 대해 Oracle이 호출되지 않으며 유한 종료한다.
6. 선택 step 기록 후·승인 후·execute commit 후·release commit 후 checkpoint 전 중단을 주입한다. resume 시 같은 ID를 재사용하고 중복 조회 효과·중복 공개·중복 지출·step 이중 집계가 없다.
7. 공개 일시 실패 재시도 성공과 retry 소진 중단에서 원래 execution/예약을 유지한다. resume 후 안전하게 공개를 마친다.
8. DB 재오픈 시 설정·step·예산·종료/재개 상태를 복원하고 설정 충돌을 거절한다. 이미 정상 완료된 run 재개가 새 행동을 실행하지 않는다.
9. 같은 run의 두 loop worker를 막고 다른 run은 독립적으로 처리한다.
10. 실제 선택기 프로세스에서 public DTO/제안 왕복과 private 파일 접근 거절을 확인한다. fixture-only와 실제 sandbox 결과를 구분한다.

### 8-2. 실제 고정 snapshot

```text
data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/
data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/
```

- 실제 raw/cache를 갱신하지 않는다. smoke는 5개 후보 범위, 확장은 max_steps=30과 유한 시간 제한의 고정 순서 실행을 예제로 사용한다. 초기 예산은 snapshot 비용 단위로 명시하고, 예제에서는 최대 5개 유료 행동을 감당하는 금액으로 설정할 수 있다.
- 실행 전에 예산·step 제한·선택 규칙을 고정한다. Active가 나오도록 후보나 순서/제한을 사후 조정하지 않는다. 30 step에서 양성이 0개여도 정상 결과로 보고한다.
- 초기 후보 전체는 유지한다. trusted 자료에서 기록 존재를 확인해 selector의 첫 후보를 골라주는 방식을 실제 loop 검증에 사용하지 않는다.
- 중단/재개 예제는 같은 run을 이어가고 기존 결과·과금이 중복되지 않는지 검사한다.
- 초기 public 파일 hash는 불변이어야 하며 runtime 공개 관측만 실제 released 측정 수만큼 증가한다. 각 공개 결과의 근거는 기존 resolver로 확인한다.
- 보고서에 실제 step/실행/공개/기록 부재/예산/종료 이유를 기록한다. 데이터가 없는 경우 fixture만으로 실데이터 완료를 선언하지 않는다.
- 마지막에 기존 0·1·2와 새 3-A 전체 회귀를 실행한다. 테스트 목표 개수를 만들지 않는다.

## 9. 산출물과 다음 단계 연결

- docs/stage3_run_loop.md: 실제 모듈/Selector 계약, 상태 투영, 정책, 종료/복구 규칙, CLI, 격리 launcher, 사용 예제.
- reports/stage3/01_run_loop.md: 선행 확인, 변경 코드, fixture/실제 snapshot/실제 sandbox 결과 구분, 실행 명령·수치·미해결 사항.
- 공개 상태/진행 이벤트 계약은 다음 선택기·서비스가 재사용할 수 있게 작은 typed DTO로 고정한다.
- 3-B는 이 loop를 바꾸지 않고 다른 기준선 selector를 연결할 수 있어야 한다. 3-C는 trusted 평가기가 실행 기록을 읽을 수 있어야 하지만 평가 정답을 selector 입력에 넣지 않는다.
- 최종 답변은 구현 요약, 검증, 실제 실행 결과, 미완료 항목, 3-B/3-C 연결점 순으로 정리한다. 3-A에서 종료하며 LLM/학습/UI/배포를 자동 시작하지 않는다.
