# AssayPilot 2-A 구현 프롬프트 — Luna용

현재 AssayPilot 저장소에서 1단계의 남은 보완사항을 먼저 재검증하고, 2-A인 **비공개 후속 자료 로딩·조회와 ReplayOracle의 내부 조회 기능**을 구현해라. 계획만 제시하지 말고 코드, 필요한 테스트, 실제 자료 검증, 인계 문서까지 완료한다.

## 0. 범위와 작업 원칙

- 목표: 고정 snapshot과 초기 PublicCampaign을 받아 `(candidate_id, assay_id)`에 대응하는 실제 보존 측정을 조회한다. 기록이 없으면 명시적 `no_record`를 반환한다.
- 이 단계의 Oracle은 신뢰된 실행기 내부 구성요소다. 실제 실험, 활성 예측, 신규 실험 결과 생성은 하지 않는다.
- 이번에 구현하지 않을 것: 승인 저장소, 예산 차감/예약, 실행 이력 저장소, 중복 실행 방지 저장소, Observation 공개, EvidenceRef 등록, RunState 갱신, OS/컨테이너 수준 접근 격리, LLM, 학습, 자동 선택 루프, UI, API 서버. 이는 2-B/2-C 또는 이후 범위다.
- 단, 내부 조회 모듈을 에이전트 도구로 노출하지 않고 public 로더와 의존성을 분리한다. 구조적 분리가 실제 접근 격리를 완료했다는 뜻은 아니다.
- 기존 AGENTS.md와 사용자 변경을 보존한다. git reset/clean, 무단 commit/push, 기존 snapshot 덮어쓰기를 하지 않는다.
- 함수·필드·Protocol 이름을 추측하지 않는다. 실제 정의와 호출부를 먼저 읽는다. 아래 제안 이름은 기존 계약과 충돌하면 조정하고 그 이유를 기록한다.
- 광범위한 리팩터링, 새 프레임워크, 범용 플러그인 체계를 추가하지 않는다. 작은 모듈과 명시적인 입출력으로 구현한다.
- 아래 순서를 지키고 각 작업 직후 해당 테스트를 실행한다. 마지막에만 전체 회귀를 실행한다. 동일 동작의 중복 테스트를 만들거나 테스트 개수 목표를 설정하지 않는다.

## 1. 저장소 확인과 선행 검증

먼저 git 상태, Python/패키지 관리 설정, 실행 환경을 확인하고 다음 실제 파일을 읽어라.

- domain 모델·protocols·validator, 특히 PublicCampaign, Candidate, DataSource, Oracle 관련 계약과 시점 검증.
- data/schemas.py의 NormalizedMeasurement, 공개 Adapter, build/selection, snapshot 보존 코드.
- docs/stage1_data_pipeline.md, docs/stage2_data_handoff.md.
- reports/stage1_completion/01_structure_audit.md, 02_snapshot_semantics.md, 03_boundaries_handoff.md.
- 기존 0·1단계 테스트 중 아래 선행 항목과 관련된 테스트.

기록된 환경은 conda `drug`지만 실제 사용 가능 여부를 확인한다. `conda run -n drug python ...`이 불가능하면 프로젝트가 지정한 환경을 사용한다. 임의로 전역 환경을 업그레이드하지 않는다.

### 1-1. 후속 0건 정책

- 유효한 PubChem 입력에서 연결된 후속 기록이 0건이어도 public bundle을 만들고 curator 진단을 남기는지 코드와 테스트로 확인한다.
- 공개 primary·시험 정의를 고정하고 후속 행을 모두 제거했을 때 public 파일이 동일한지 확인한다. synthetic data_kind만이 아니라 `data_kind="pubchem"` 경로도 검증한다. 실제 네트워크 대신 올바른 metadata/hash를 가진 작은 fixture를 사용해도 된다.
- 파일 누락, 손상, 파싱 실패를 정상적인 0건으로 처리하지 않는다. 입력 CSV의 정상적인 빈 결과 표현과 잘못된 빈 응답을 구분한다.
- 오래된 “후속 0건이면 무조건 build 오류” 문구가 남으면 수정한다. 기존 동작이 이미 맞으면 문서와 검증 근거만 보완한다.

### 1-2. AID 설명과 counter 의미

- 보존된 공식 description과 이전 source review를 대조해 AID 2272의 단일 농도 cherry-pick confirmatory와 AID 504468의 dose-response SAR를 구분한다. 근거 없이 2272를 dose-response라고 부르는 문구를 정정한다.
- counter fixture에서 active의 의미와 설정된 성공 조건이 build·직렬화·public 로딩 후 유지되는 테스트를 확인한다. 없다면 최소 테스트를 추가한다. 성공 판정 엔진은 구현하지 않는다.

### 1-3. 인계 계약

- 실제 파일명이 `curator/data_audit_report.json`인지 확인하고 문서를 통일한다.
- 전체 정규화 파일은 primary와 후속 기록 전체, hidden 파일은 선택 후보에 연결된 primary 외 기록의 부분집합이라는 설명이 실제 산출물과 맞는지 확인한다.
- 후보 연결은 `Candidate.source="pubchem_sid"`, `source_id="SID:<sid>"`와 `NormalizedMeasurement.sid`의 명시적 대응을 사용한다. 실제 코드와 다르면 실제 계약을 먼저 확인한다.
- 버전 위치와 지원 값은 실제 validator에서 확인한다. 현재 문서에는 public manifest 1.0.0, PublicCampaign 0.1.0, config 1.0.0이 기록되어 있고 측정 JSON 배열 자체에는 별도 버전이 없다.

선행 항목별로 `기존 검증 확인 / 문서 수정 / 코드 수정 / 미확인`을 기록한다. 수정 가능한 문제는 이 범위에서 고치고 통과 후 2-A를 진행한다. 필요한 파일이나 근거가 없으면 그 항목을 완료로 적지 않는다. 실제 자료가 없더라도 독립적인 fixture 구현·검증은 수행하고, 실제 검증만 미실행으로 보고한다. 핵심 계약이 서로 모순되어 정확한 구현이 불가능하면 해당 의존 작업만 중단하고 구체적인 충돌을 보고한다.

## 2. 구현 계약을 먼저 정리

권장 구성은 비공개 자료 loader/store, 결과 타입, ReplayOracle의 세 부분이다. 기존 패키지 구조와 Protocol을 우선 사용하고 필요할 때만 내부 타입을 추가한다.

개념적인 연결은 다음과 같다. 이 이름과 시그니처를 기존 코드를 보지 않고 그대로 강제하지 않는다.

```python
store = load_replay_store(snapshot_root, public_campaign)
oracle = ReplayOracle(store)
result = oracle.lookup(candidate_id, assay_id)
```

- store는 하나의 snapshot과 campaign에 묶는다. 조회마다 외부에서 curator 경로나 파일명을 받지 않는다.
- 기존 Oracle Protocol이 승인·실행·Observation 반환까지 요구한다면 완료된 척 맞추지 않는다. 그 Protocol 뒤에 붙일 내부 조회 구성요소를 구현하고 이후 adapter 연결 지점을 문서화한다.
- 구현 시작 전 결과 상태, 오류 종류, 입력 식별자, 출력 측정의 원본 보존 범위와 2-B/2-C 연결점을 짧게 문서화한다. 같은 개념의 domain 타입을 중복 생성하지 않는다.

## 3. 비공개 후속 자료 로더와 인덱스

### 3-1. 신뢰된 입력 로딩

- 기존 PublicBundleAdapter로 검증된 PublicCampaign을 사용한다. public 검증 규칙을 새로 복제하지 않는다.
- 신뢰된 snapshot_root에서 config, snapshot_manifest.json, 인계용 hidden_followup_measurements.json을 읽는다. 기존 manifest 형식·hash 계산 코드와 helper를 재사용한다.
- snapshot manifest와 보존 config의 실제 버전/구조를 확인하고, 로딩할 curator 파일이 manifest에 등록돼 있으며 bytes의 SHA-256이 일치하는지 검증한다. public manifest가 curator까지 보호한다고 가정하지 않는다.
- 전달된 PublicCampaign이 이 snapshot의 공개 campaign과 일치하는지 확인한다. campaign_id 하나만 믿지 말고, 검증된 campaign 내용이나 기존 canonical 표현을 비교하여 다른 revision 혼입을 거절한다.
- manifest 상대 경로의 절대 경로·`..` 탈출과 symlink를 통한 root 외부 접근을 거절한다. 기존 경로 검증 도구가 있으면 재사용한다.
- hash 확인한 것과 같은 bytes를 Pydantic으로 파싱해 파일을 다시 읽는 사이 변경되는 문제를 피한다.
- 현재 측정 JSON은 배열이다. 이를 `list[NormalizedMeasurement]`로 검증하고 config/manifest와 묶인 현재 고정 형식임을 문서화한다. 독립 버전이 있다고 가정하거나 예전 snapshot을 새 envelope로 덮어쓰지 않는다.
- 알 수 없는 지원 버전은 명확히 거절한다. `except Exception: return []`처럼 오류를 0건으로 숨기지 않는다.
- 로더의 주 입력은 hidden 파일이다. 전체 정규화 파일 328,519행을 매 조회마다 읽지 않는다. 전체와 hidden의 부분집합 관계는 개발자용 실제 자료 검증에서 확인한다.
- hash는 보존본의 변경 탐지 수단이며 악의적인 manifest 교체를 인증하는 장치라고 주장하지 않는다.

### 3-2. 정체성 및 참조 검사

- candidate_id와 assay_id 중복, SID source 대응 중복·충돌을 검사한다. 공개 후보에 없는 SID, 공개 시험 목록에 없는 assay, hidden 파일의 primary 행을 거절한다.
- SID→candidate_id 매핑을 로딩 시 한 번 만든다. 동일 CID를 가진 서로 다른 SID를 합치지 않는다. candidate_id의 임의 문자열 분해에 의존하지 않는다.
- 필요한 source 형식이 지원되지 않는 campaign은 `unsupported` 입력 오류로 처리한다. KLF5/MEP2/AID 2016/2272를 일반 로더 분기문에 고정하지 않는다.
- 기존 자료에 SID/CID/AID 일관성 검사 계약이 있으면 유지한다. 없는 필드를 만들어 유추하거나 CID만으로 누락 SID를 보충하지 않는다.
- 인덱스 키는 snapshot/campaign에 묶인 `(candidate_id, assay_id)`, 값은 연결된 측정들의 튜플 또는 외부 수정에 안전한 컬렉션으로 한다.
- 서로 다른 measurement_id의 같은 내용 행도 원본 기록으로 보존한다. 동일 measurement_id가 여러 번 나타나는 식별자 오류는 명확히 거절하며 조용히 덮어쓰지 않는다.
- 측정 value/unit/comparison, verdict, raw_row, source_row_* 및 반복·조건 식별자를 보존한다. `not_reported`를 실제 조건 확인으로 해석하지 않는다.

## 4. ReplayOracle의 조회 동작

### 4-1. 결과 상태

내부 결과는 다음을 구별해야 한다. 실제 타입 이름은 기존 계약에 맞춘다.

| 경우 | 기대 동작 |
|---|---|
| 유효한 공개 후보와 지원 후속 시험, 연결 기록 1개 이상 | `records_found`와 해당 측정들 반환 |
| 유효한 공개 후보와 지원 후속 시험, 연결 기록 0개 | `no_record`와 빈 측정 컬렉션 반환 |
| 알 수 없는 후보·시험 또는 지원하지 않는 primary 조회 | 명시적 요청 오류 |
| 파일 부재·손상·지원하지 않는 버전·참조 충돌 | 로딩/무결성 오류; `no_record`로 변환 금지 |

- result에는 요청 후보·시험과 내부 추적에 필요한 snapshot/campaign 식별 정보를 포함한다. 전체 후보별 결과 존재 여부, 전체 coverage나 다른 후보 결과를 함께 반환하지 않는다.
- 결과 타입은 `records_found`인데 0개, `no_record`인데 측정이 있는 모순된 상태를 거절한다.
- raw Active/Inactive/Inconclusive는 그대로 보존한다. counter active를 성공/실패로 재해석하지 않는다. 수치 0과 결측을 구분하며 입력 계약이 허용하는 수치·비교 연산자를 그대로 유지한다.
- `no_record`는 이 snapshot의 기록 부재다. 실제 실험 미수행, inactive, inconclusive, 실험 실패를 뜻하지 않는다.

### 4-2. 여러 측정과 결정성

- 후보×시험에 여러 기록이 있으면 모두 반환한다. 임의 첫 행 선택·평균·다수결·중복 내용 제거를 하지 않는다.
- 동일한 measurement 객체 집합의 배열 순서만 달라도 조회 결과 순서는 안정적으로 같아야 한다. measurement_id 등 기존 안정 식별자를 기준으로 명시적 순서를 정한다.
- 원본 CSV 행 순서를 바꿔 정규화 ID 자체가 바뀐 경우까지 같은 ID를 강제하지 않는다. 이번 검증은 고정 정규화 레코드의 배열 순서 독립성이다.
- 동일 요청 반복은 같은 의미의 결과를 반환하고 store를 소비·변경하지 않는다. 결과를 호출자가 수정해도 후속 조회나 내부 저장 상태가 오염되지 않도록 immutable 값 또는 방어적 복사를 사용한다. 외부 리스트만 tuple로 바꾸는 것으로 내부 raw_row까지 보호된다고 가정하지 않는다.

### 4-3. 이후 단계와의 경계

- 이 메서드는 승인 경계 내부의 조회 함수다. 승인 완료를 가짜 bool이나 더미 토큰으로 흉내 내지 않는다. 이후 실행기가 승인·예산 검사를 거친 뒤 호출한다.
- 이번에는 released_at을 부여하지 않고 Observation이나 EvidenceRef를 생성하지 않는다. 공개 시 필요한 기존 원본 근거 정보를 결과에서 보존한다.
- 초기 PublicCampaign과 파일은 변경하지 않는다. 조회 결과를 campaign.observations에 추가하지 않는다.
- `no_record`의 assay 비용 미차감, 여러 기록을 한 번에 공개하는 단위, 같은 행동 재요청의 중복 공개 방지는 후속 실행 정책으로 문서화한다. 이 단계에서는 금액·승인·상태를 전혀 변경하지 않는다.
- 기존 원본에 반복 조건 구별 근거가 없으므로 이번 batch 조회를 실제 신규 반복 실험 수행으로 표현하지 않는다.

## 5. 검증 항목

아래 항목을 기존 테스트와 연결해 확인한다. 구현을 그대로 따라 쓰는 테스트 대신 잘못된 입력과 실제 불변 조건을 검사한다. fixture는 작은 가상 데이터임을 표시하고 실제 PubChem 결과로 보고하지 않는다.

### 5-1. 작은 fixture 단위·통합 테스트

1. 기록 1개 조회 시 후보·시험과 원본 판정·근거가 정확히 대응한다.
2. 유효한 후보×후속 시험에 기록이 없으면 `no_record`다. 유효한 빈 hidden 배열을 로딩하는 경우도 확인한다.
3. 알 수 없는 후보·시험, primary 조회는 정상적인 기록 부재와 구분된다.
4. 반복·상충 측정을 모두 보존하며, 정규화 배열 순서 변경에도 결과 순서가 결정적이다.
5. 동일 CID의 서로 다른 SID가 섞이지 않고, 중복 source 대응·measurement_id·깨진 참조는 거절된다.
6. counter 의미, inconclusive, 지원되는 수치 0·비교 연산자·결측이 변형되지 않는다. 범주형 fixture에 잘못된 수치를 넣어 테스트하지 않는다.
7. curator 파일 누락·JSON 손상·hash 불일치·미지원 버전·경로 이탈을 거절한다. 참조·타입 검증 테스트에서는 hash가 유효한 별도 fixture를 사용하여 hash 오류만 검사하고 끝나지 않게 한다.
8. 다른 campaign/revision의 공개 입력과 비공개 자료를 혼합하면 거절된다.
9. 반복 조회와 반환 객체 수정 시도가 내부 저장 내용·다음 결과·초기 PublicCampaign을 바꾸지 않는다.
10. 결과 상태와 측정 개수의 모순을 거절한다. 출력에는 요청하지 않은 후보의 결과나 전체 coverage가 없다.
11. 공개 Adapter는 curator 없이 계속 동작하고 기존 공개 로딩 경로가 새 비공개 모듈을 호출하지 않는다.

### 5-2. 실제 snapshot 검증 — 신뢰된 개발자 실행 전용

다음 경로가 실제 존재하는지 먼저 확인하고 검증한다.

```text
data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/
data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/
```

- 네트워크 fetch나 원본 갱신 없이 고정 snapshot만 사용한다.
- smoke와 확장 snapshot을 각각 로딩해 후보·hidden 측정·매핑된 후보 수를 실제 계산한다. 보고된 참고 값은 smoke 후보 5개/후속 측정 1개, 확장 후보 1,682개/후속 측정 295개/기록 부재 후보 1,387개다. 이를 일반 구현의 상수로 넣지 않는다.
- 확장 자료의 hidden 레코드가 전체 정규화 자료의 해당 선택 후보·비primary 부분집합과 일치하는지 measurement_id와 내용으로 검증한다.
- 연결 기록이 있는 후보와 없는 후보를 개발자 검증용으로 골라 실제 조회 결과를 확인한다. 이 검증용 선택을 모델 후보 선정이나 성능 평가로 사용하지 않는다.
- 후속 판정별 수를 개발자 보고서에서 원본과 대조한다. 이전 보고 값 Active 30/Inactive 265와 다르면 원인을 설명하며 맞추기 위해 자료를 변경하지 않는다.
- 로딩·조회 전후 public 파일 hash가 동일하고 새 Observation/released_at이 생기지 않았는지 확인한다.
- 가능한 경우 기존 공개 Adapter/Auditor 검사도 재사용한다. 화학 처리 코드를 바꾸지 않았는데 RDKit 관련 테스트 체계를 새로 만들 필요는 없다.
- 마지막에 기존 0·1단계와 새 2-A 테스트 전체를 실행한다. 실제 실행 환경·명령·결과를 기록한다. 실제 파일이 없으면 통과/완료로 적지 않는다.

## 6. 문서·보고·종료

- docs/stage2_replay_lookup.md 또는 기존 적합한 문서에 입력/출력, 모듈 책임, 로딩·조회 예시, 오류와 no_record 구분, 여러 기록 반환, 버전·hash 범위, 2-B/2-C 책임을 작성한다.
- reports/stage2/01_replay_lookup.md에 다음을 기록한다.
  1. 선행 보완 세 항목과 인계 계약의 재검증 결과·실제 테스트 이름.
  2. 새 코드와 기존 코드 재사용 범위, 실제 공개 메서드 시그니처.
  3. fixture 테스트와 실제 snapshot 검증 결과를 분리한 표.
  4. 구현하지 않은 승인·예산·공개·실제 접근 격리 항목과 다음 연결점.
  5. 미해결 항목이 있으면 영향과 필요한 입력. 테스트 수를 성능·생물학적 검증으로 해석하지 않는다.
- 보고서와 비공개 통계는 개발자용 위치에 둔다. demo/public 산출물에 복사하지 않는다.
- snapshot 산출물이 실제로 바뀌어야 하는 선행 오류를 수정했다면 새 revision으로 보존한다. 문서·테스트만 바뀌었다면 기존 데이터 revision을 불필요하게 복제하지 않는다. 새 2-A 코드 버전과 데이터 revision은 별도로 기록한다.
- 최종 답변은 수정 요약, 검증 결과, 미해결 사항, 2-B가 호출할 정확한 연결점 순으로 작성한다. 2-B/2-C를 자동 구현하지 말고 2-A에서 종료한다.
