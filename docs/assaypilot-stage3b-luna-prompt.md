# AssayPilot Stage 3-B 구현 프롬프트 — Luna용

현재 저장소에서 **Stage 3-B: 선택기 기준선 추가와 동일 조건 실행**을 구현해라. 검증된 3-A loop에 재현 가능한 무작위 우선순위 선택기를 추가하고 기존 fixed_order와 비교 가능한 실행 자료를 남긴다. 코드·테스트·실제 고정 snapshot 실행·보고서까지 완료한다.

## 0. 범위와 원칙

- 포함: 기존 fixed_order 유지, seeded_random_priority 선택기 하나 추가, 격리 worker/CLI 연결, seed·알고리즘 버전 보존, 중단/재개 결정성, 사전에 고정한 조건의 기준선 실행과 자료 저장.
- 제외: 학습·활성 예측·LLM·화학적 다양성 최적화, 정답 기반 성능 지표/통계 검정(3-C), UI·배포·실제 실험.
- 여러 기준선은 우선 fixed_order와 seeded_random_priority 두 종류로 한정한다. 검증되지 않은 복잡한 선택기를 추가하지 않는다.
- 기존 실행·승인·과금·공개·복구·격리 경로를 유지한다. 알고리즘을 추가하기 위해 loop 전체를 재작성하지 않는다.
- AGENTS.md, git 상태, 기존 사용자 변경을 먼저 확인한다. snapshot 덮어쓰기, 무단 reset/clean/commit/push를 하지 않는다.
- 함수·필드명을 추측하지 말고 실제 계약을 읽는다. 아래 제안 이름이 기존 코드와 충돌하면 조정하고 이유를 기록한다.

## 1. 현재 구현 확인

다음 자료와 실제 코드·테스트를 읽어라.

- docs/stage3_run_loop.md, reports/stage3/01_run_loop.md.
- run_loop.py, selector_worker.py, run_loop_cli.py, public_api.py와 관련 execution.py 연결점.
- tests/test_run_loop.py, tests/test_run_loop_cli.py.
- scripts/verify_stage3_run_loop.py, 실제 격리 selector launcher.
- RunLoopConfig 저장 형식/sha256, resume 검증, selector-view.v1, proposal 검증, runtime DB schema v3.

현재 보고서상 다음 사항이 구현돼 있다. 중복 구현하지 말고 필요한 회귀를 실행한다.

- Selector.select(view: SelectorView) → SelectorProposal 또는 검증되는 dict.
- 공개 후보·assay·관측·예산·eligible actions·이미 시도한 행동 요약만 입력.
- trusted 정책이 승인·execute·release를 담당하고 선택기는 권한/비용/ID를 정하지 않음.
- durable step 저장 후 승인/실행, 동일 action/request ID로 crash/retry/resume.
- 실제 Python worker 격리와 입력 2 MiB/출력 16 KiB 제한, 비격리 fallback 없음.

새 선택기가 이 계약을 사용하도록 필요한 최소 확장만 한다. 기존 fixed_order 실행 결과의 의미를 바꾸지 않는다. r2의 max_steps와 행동 소진이 동시에 발생하면 기존 종료 우선순위를 유지해도 된다. 단, 그 우선순위를 문서에 명시한다.

## 2. 새 선택기 알고리즘을 정확히 고정

### 2-1. fixed_order

- 기존 `(candidate_id, assay_id)` 사전순의 첫 eligible action 선택을 유지한다.
- 이는 결정적 연결 기준선이며 생물학적 우선순위를 주장하지 않는다.

### 2-2. seeded_random_priority

상태를 가진 PRNG 대신 **seed와 행동 식별자로 결정되는 해시 우선순위**를 사용한다. 복구 시 난수 상태 저장·복원을 추가하지 않고도 같은 행동 순서를 재현하기 위한 선택이다.

구현 규칙:

1. 정수 seed와 명시적 알고리즘 버전 `random-priority-v1`을 trusted 실행 설정에 고정한다. seed에서 bool/float/모호한 문자열을 허용하지 않는다.
2. 각 eligible action에 대해 다음 값의 canonical JSON UTF-8 bytes를 만든다. 구분자·인코딩 규칙을 명시해 문자열 단순 연결 충돌을 피한다.

```python
["random-priority-v1", seed, candidate_id, assay_id]
```

3. SHA-256 digest를 계산하고 `(digest_bytes, candidate_id, assay_id)` 오름차순의 첫 행동을 고른다. 동률 처리도 명시적이다.
4. 결과가 공개되어 eligible set이 바뀌면 현재 eligible set에서 같은 규칙을 적용한다. 처리된 행동은 3-A의 제외 규칙으로 재선택하지 않는다.
5. eligible set이 비면 기존 stop 계약을 사용한다.

- 이 방식은 고정 seed의 의사난수 우선순위 기준선이다. 매 step에서 독립적인 균등 난수를 추출하는 알고리즘과 동일하다고 설명하지 않는다.
- Python hash(), set 순회 순서, os.urandom, 현재 시각, run/request/action/execution UUID를 우선순위에 사용하지 않는다.
- 전체 snapshot hash나 hidden file hash를 우선순위에 넣지 않는다. hidden 결과 변화가 초기 선택을 바꾸면 안 된다.
- 같은 초기 공개 후보 ID·assay ID, seed, 알고리즘 버전과 같은 중간 공개 결과면 다른 run ID에서도 행동 순서가 같아야 한다.
- 선택 사유는 “고정 seed의 해시 우선순위가 가장 높은 실행 가능 행동”처럼 실제 규칙만 설명한다. 분자 활성·근거 수준·효능을 예측했다고 쓰지 않는다.

## 3. worker·설정·resume 연결

- production 경로는 기존 실제 격리 Python worker를 사용한다. 새 무작위 기준선만 host in-process에서 실행하는 우회 경로를 만들지 않는다.
- worker startup에 trusted selector kind, seed, algorithm version을 전달하고 세션 동안 고정한다. 선택기 출력이 seed·종류를 바꾸게 하지 않는다.
- catalog 초기 전송·관측 delta·eligible actions 검증 등 기존 통신 계약을 유지한다. 표준 라이브러리 hashlib/json이면 충분하며 새 ML 패키지가 필요 없다.
- 잘못된 selector 종류/누락 seed/미지원 version은 시작 전에 거절한다. worker timeout·출력 상한·proposal schema·불법 행동 거절을 유지한다.
- CLI는 fixed_order와 seeded_random_priority를 명시적으로 선택할 수 있게 한다. 새 선택기에는 --seed 정수와 알고리즘 버전 기록이 필요하다. 기존 fixed_order 명령은 계속 동작해야 한다.
- seed와 selector version은 RunLoopConfig 및 보존 fingerprint에 포함한다. resume에서 다른 seed/종류/버전으로 바꾸지 못하게 한다.
- 기존 fixed_order 설정에는 seed 필드가 없을 수 있다. 새 기본 필드를 추가한 뒤 재직렬화해서 기존 sha256이 깨지지 않도록 **저장된 원본 설정의 hash를 먼저 검증하고 구형 fixed_order 의미를 명시적으로 해석**한다.
- 기존 저장 설정을 조용히 다시 쓰거나 hash 검증을 끄지 않는다. 실제 schema 변경이 필요할 때만 호환 migration을 하고 기존 DB를 삭제하지 않는다.
- 안정된 algorithm version을 바꾸는 동작 변경은 새 버전으로 구분한다. 이미 실행된 run은 원래 의미를 유지한다.

## 4. 같은 조건으로 비교 실행

### 4-1. 사전 고정 설정

실행 전에 개발자용 비교 설정 JSON을 기록하고 다음을 고정한다.

- snapshot/public input 식별 정보, 구현 버전 또는 실제 변경분 식별.
- fixed_order 1회, seeded_random_priority seed `[0, 1, 2, 3, 4]` 각 1회.
- 같은 초기 예산/단위/assumed, cost policy, bounded_replay 승인 정책, max_steps, duration/timeout/retry 제한.
- snapshot당 각 run은 서로 독립된 초기 상태다. 결과 공개나 학습 정보를 다른 run으로 전달하지 않는다.
- 실제 예제 기본값: r2 max_steps=5, 확장 max_steps=30, 둘 다 budget=5 synthetic_credit 및 유한 deadline. 비용 단위는 실제 snapshot 설정과 일치하는지 확인한다.
- 비교 도중 결과가 좋도록 seed·후보·step 제한을 재탐색하지 않는다. 시간이 부족하면 실제 수행한 seed와 미수행 seed를 그대로 보고한다.

동일 예산·max_steps라도 종료 이유에 따라 실제 지출/시도 수는 다를 수 있다. 특히 no_record는 비용이 차감되지 않으므로 released가 많다고 선택기의 과학적 성능이 높다고 결론 내리지 않는다.

### 4-2. 기존 loop 그대로 사용

- 기존 start/controller 경로를 호출하며 별도 비교용 execute/approval/release 로직을 만들지 않는다.
- selector는 초기 공개 후보 전체에서 고른다. 후속 기록이 있는 295개 후보만 넣거나 시작 전에 Oracle로 후보를 선별하지 않는다.
- hidden 측정/전체 Active 수/coverage 분포를 선택기에 넣지 않는다. 미시도 행동의 관측 존재 여부를 추가하지 않는다.
- LLM이나 활성 점수는 이번에 사용하지 않는다. 향후 selector 교체에 필요한 계약은 기존 3-A 그대로 유지한다.

### 4-3. 남길 자료

각 실행의 summary와 공개 판단 trace를 보존한다. 검증용 임시 DB를 삭제하더라도 비교 결과 전체가 사라지면 안 된다.

- 사전 고정한 비교 설정과 selector version/seed.
- run별 step/action 후보·시험 순서, 당시 공개 view 식별자, 원래 선택 사유, 실행/공개 상태, 최신 예산, 종료 이유.
- 새로운 관측·공개 결과의 ID와 근거 참조. 필요하면 commit된 공개 결과를 별도 JSON으로 보존해 3-C가 정규 실행 결과를 읽을 수 있게 한다.
- snapshot/설정/구현 식별자, 실행 명령, 시작·종료 시각, 실제 격리 모드.
- raw hidden 결과, 전체 curator 배열, 시도하지 않은 후보 정답은 trace에 포함하지 않는다. private runtime DB를 사용자 공개 파일로 배포하지 않는다.

권장 개발자 산출물 위치는 reports/stage3/baselines/ 아래 비교 설정과 run별 summary/trace다. 예전 성공 자료를 덮어쓰지 말고 식별 가능한 새 디렉터리에 저장한다.

이번 보고 표에는 selector/seed, durable steps, executions, released, no_record, failed, observation 증가 수, spent/reserved/available, stop_reason만 넣는다. released는 기록 공개 수이며 Active 발견 수가 아니다. 정답 기반 recall·hit enrichment·유의성 검정은 3-C에 남긴다.

## 5. 검증 — 아래 순서대로

### 5-1. 선택기 단위·프로토콜

1. 작은 hand-crafted fixture의 canonical hash 계산과 실제 선택이 일치한다. 입력 순서를 뒤집어도 결과는 같다.
2. 같은 seed/version/action identity면 다른 run ID에서도 같은 우선순위다. 다른 seed가 반드시 매번 다른 첫 행동을 만들어야 한다는 잘못된 assertion은 쓰지 않는다.
3. 같은 digest의 동률 처리 경로는 제어된 fixture로 검증한다. 실제 SHA-256 충돌을 찾지 않는다.
4. 동적 eligible set에서 방금 처리한 행동을 제외하고 신규 선행조건 충족 행동을 올바르게 포함한다. eligibility 자체는 기존 로직을 사용한다.
5. 고정 공개 입력에서 hidden outcome/행 존재/정답 분포를 바꿔도 초기 view와 첫 선택이 동일하다.
6. 기존 fixed_order 동작, unknown selector/누락·잘못된 seed/미지원 version 거절, protocol 크기 제한을 확인한다.

### 5-2. 복구와 호환성

7. 고정 seed run을 중단 없이 실행한 행동 순서와, 선택 저장 후 또는 공개 commit 후 중단하고 resume한 순서를 비교한다. 같은 공개 피드백·step 제한과 충분한 deadline 조건에서 동일해야 한다.
8. resume은 새 random order를 뽑지 않고 durable action/request를 재사용한다. 중복 settlement·Observation·step 집계가 없다.
9. 다른 seed/version으로 기존 run 재개를 거절한다. 구형 fixed_order config/hash를 가진 기존 run은 원래 의미로 재개된다.
10. PRNG 상태를 저장하지 않는 대신 hash priority가 복구 결정성을 제공한다는 점을 실제 테스트로 보여준다.

### 5-3. 실제 worker와 자료

11. 새 선택기를 실제 격리 worker에서 실행해 proposal 왕복과 private canary 접근 거절을 확인한다. unit fixture 결과로 대체하지 않는다.
12. r2와 확장 snapshot 각각에서 fixed_order+고정 5 seed를 같은 제한으로 실행한다. 정답을 보고 seed를 고르지 않는다.
13. 하나 이상의 seeded run을 2 step 뒤 중단하고 재개하여 실제 snapshot 복구를 확인한다. 중단 없는 동일 seed 실행과 행동 순서를 비교한다. timeout 등 시간 조건 차이가 생기면 숨기지 말고 설명한다.
14. 보존한 summary/trace를 다시 읽어 step/실행/공개 수와 금액이 원래 loop 결과와 일치하는지 확인한다. stale receipt 대신 현재 budget을 사용한다.
15. 원본 snapshot public hash는 불변이며 runtime 공개 결과만 증가한다. no_record를 음성으로 변환하지 않는다.
16. 마지막에 기존 0·1·2·3-A와 새 3-B 회귀를 실행하고 실제 명령·결과를 기록한다. 실제 환경/자료가 없으면 가능한 fixture 작업을 완료하고 미실행 검증을 명시한다.

## 6. 문서와 종료

- docs/stage3_baseline_selectors.md: 정확한 알고리즘, seed/version 규칙, fixed_order와 차이, config 호환·resume, CLI 예시, 격리 경계, 비교 설정과 산출물 형식.
- reports/stage3/02_baseline_selectors.md: 변경 파일, 회귀/fixture/실제 격리/실제 snapshot 결과를 구분하고 run별 비교 표와 미해결 항목 기록.
- 필요한 기존 stage3_run_loop.md만 수정해 새 선택기 연결과 설정 호환성을 반영한다. 이전 보고서의 과거 검증 수치를 현재 결과로 덮어쓰지 않는다.
- 3-C가 사용할 summary/trace/공개 결과 경로와 schema를 명시한다. hidden 정답을 selector input에 추가하지 않는다.
- 최종 답변은 구현 요약, 검증 결과, 실제 비교 실행 표, 미완료 항목, 3-C 연결점 순으로 작성한다.
- 이 단계의 실행 횟수 차이를 약효·결합·임상 성능 또는 모델 일반화 우위로 해석하지 않는다. Stage 3-B에서 종료한다.
