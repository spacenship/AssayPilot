# AssayPilot 0단계 확인 보고

## 1. 모듈 계약의 정의 범위

`src/assaypilot/domain/protocols.py`에는 다음 8개 모듈의 Protocol과 10개 메서드가 정의되어 있다. 모든 메서드는 타입이 명시된 계약이며 본문은 `...`이다. 구체적인 어댑터·학습기·실행기의 동작 구현은 포함하지 않는다.

| 모듈 | 메서드 | 반환 타입 |
|---|---|---|
| CampaignAdapter | load(DataSource) | PublicCampaign |
| Auditor | audit(PublicCampaign) | AuditResult |
| Predictor | fit(후보 구조, 시험 맥락, ObservationBatch) | None |
| Predictor | predict(후보 구조, 시험 맥락, ObservationBatch) | PredictionBatch |
| Planner | plan(PublicCampaign, RunState, 근거 목록) | Plan |
| Selector | select(Plan, PredictionBatch, RunState) | list[ActionRequest] |
| Governor | review(ActionRequest, PublicCampaign, RunState) | GovernanceDecision |
| Executor | submit(ApprovedAction) | ExecutionReceipt |
| Executor | collect(ExecutionReceipt) | ExecutionResult |
| Reporter | render(PublicCampaign, RunState 기록 목록) | Report |

승인 제안과 승인된 행동은 ActionRequest와 ApprovedAction으로 구분한다. 계약 정의는 실행 구현이나 런타임 권한 인증을 의미하지 않는다.

## 2. 실제 실행 증빙

현재 `AssayPilot` 소스를 conda `drug` 환경에서 다시 실행했다. Python 3.12.14, Pydantic 2.13.5, pytest 9.1.1을 사용했다. 실행 시각·인터프리터·실제 import 경로는 environment.json에 기록했다.

실제 실행한 재현 명령:

```bash
cd /data1/miplab/wjyang/AssayPilot
conda run -n drug python reports/stage0_verification/reproduce.py
```

재현 스크립트는 동일한 drug Python으로 아래 두 프로세스를 실행하고, 별도로 5종 오류 주입을 검증한다. 현재 폴더의 소스를 확실히 사용하도록 프로세스의 PYTHONPATH를 프로젝트 src 절대경로로 지정한다. 전역 설정이나 환경 설치는 변경하지 않았다.

```bash
PYTHONPATH="$PWD/src" conda run -n drug python examples/stage0_contract_demo.py
PYTHONPATH="$PWD/src" conda run -n drug python -m pytest -v --color=no --junitxml=reports/stage0_verification/pytest.xml
```

- 예제: synthetic-alpha 후보 3개·시험 3개·공개 관측 5개, synthetic-beta 후보 1개·시험 1개·공개 관측 0개. 참조 및 JSON 왕복 검사 성공. 교환 객체 검사도 성공. 종료 코드 0.
- 전체 테스트: **80 passed in 0.20s**, 실패·건너뛰기 없음, 종료 코드 0.
- 개별 테스트 이름과 통과 결과는 pytest.log, 기계 판독 결과는 pytest.xml에 있다.
- 실제 모델 학습·예산 차감·Oracle 실행은 하지 않았다. 합성 데이터의 계약 검증 결과다.

## 3. 오류 입력에 대한 실제 결과

| 주입한 입력 | 검출 결과 | 대상·필드 |
|---|---|---|
| 관측 o1의 assay_id를 `missing`으로 변경 | ok=False, unknown_reference | o1 / assay_id |
| screen이 자기 자신을 선행조건으로 참조 | ok=False, cycle | screen / prerequisites[0].assay_id |
| screen → confirmation → screen | ok=False, cycle | confirmation / prerequisites[0].assay_id |
| screen → confirmation → interference → screen | ok=False, cycle | interference / prerequisites[0].assay_id |
| 관측의 assay_id를 빈 문자열로 입력 | ValidationError, string_too_short | observations[0].assay_id |

위 화살표는 시험이 선행 시험을 참조하는 방향이다. 원본 예제 파일은 변경하지 않고 매번 새 객체에 오류를 주입했다. 결과 원문은 negative_cases.json, 실행 코드는 reproduce.py에 있다.

단일 객체의 빈 ID·범위·필수 정보 오류는 Pydantic ValidationError로 거절한다. 존재하지 않는 ID나 순환 같은 여러 객체 사이의 오류는 `validate_public_campaign` 등의 순수 함수를 호출할 때 AuditIssue 목록과 `ok=False`로 반환한다. **객체 생성만으로 참조 검사가 자동 실행되지는 않는다.** 호출자는 검사 결과를 확인해 다음 단계 진행을 막아야 한다.

기존 테스트도 잘못된 후보·시험·근거 참조, 중복 ID, 자기/2개/3개 순환, 단위·시점 불일치, 잘못된 확률·불확실성·금액, 실행 상태 불일치, 승인·접수 ID 연결, 공개 추가 필드 거절, JSON 의미 보존을 검증한다.

## 4. 첨부 구성

`reports/stage0_review_bundle.zip`에는 다음 파일이 원래 상대경로로 포함된다.

- src/assaypilot/domain/: 공통 타입, 교환 모델, 컨테이너, Protocol, 순수 검사 함수
- src/assaypilot/__init__.py: 패키지 진입점
- tests/: 전체 테스트와 fixture
- examples/: 합성 JSON 2개 및 독립 실행 예제
- pyproject.toml, README.md, docs/stage0_contracts.md: 설치·설계 문서
- reports/stage0_verification/: 이 보고서, 재현 스크립트, 환경 정보, 예제 로그, 테스트 로그/XML, 오류 주입 JSON

기존 domain/tests/examples 코드는 수정하지 않았으며 보고·재현 파일만 추가했다.
