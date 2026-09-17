# AssayPilot 0단계~1단계 구현 및 검증 기록

이 문서는 현재 저장소에 구현된 0단계 계약과 1단계 데이터 준비 계층, 그리고 이 작업에서 실제 실행한 검증 결과를 기록한다.

## 범위

- 0단계 domain 계약과 Protocol은 유지했다. `CampaignAdapter.load(DataSource) -> PublicCampaign`, `Auditor.audit(PublicCampaign) -> AuditResult`를 포함한 이후 모듈 Protocol은 `src/assaypilot/domain/protocols.py`에 있다.
- 0단계의 `validate_execution(..., as_of=...)` 현재 기준 시점 요구와 `validated_replace(...)`를 통한 검증 후 객체 교체 규약은 유지했다.
- 1단계에서 domain 패키지는 data 구현을 import하지 않는다. 이번 1단계 변경에서 `src/assaypilot/domain/` 파일은 수정하지 않았다.
- 구현 범위는 PubChem 원본 수집·캐시, 정규화, 공개/개발자용 분리, 공개 bundle Adapter·Auditor, CLI, fixture, 회귀 테스트와 문서다.
- ReplayOracle 결과 공개, Executor 구현, RunState 갱신, 예산 예약·차감, 실제 접근 격리, 모델 학습 및 UI는 구현하지 않았다.

## 추가·수정 파일

| 위치 | 내용 |
| --- | --- |
| `src/assaypilot/data/schemas.py` | 준비 설정 `CampaignConfig`, assay 매핑, 원본 파일 명세, `NormalizedMeasurement`, `DataAuditReport` |
| `src/assaypilot/data/pubchem.py` | PUG-REST fetch, 캐시·메타데이터·SHA-256, retry/rate limit, concise CSV 파서 |
| `src/assaypilot/data/normalize.py` | AID/SID/CID, raw verdict, 수치·비교 연산자, 안정 ID 정규화 |
| `src/assaypilot/data/build.py` | cache 기반 build, 후보 선택, public/curator 분리, manifest 생성 |
| `src/assaypilot/data/adapter.py` | `PublicBundleAdapter` |
| `src/assaypilot/data/audit.py` | `PublicCampaignAuditor` 및 측정 통계 보조 함수 |
| `src/assaypilot/data/cli.py`, `main.py`, `__main__.py` | `fetch`, `build`, `inspect` argparse 진입점 |
| `examples/stage1_fixture/` | 네트워크 없는 synthetic concise CSV, description JSON, compound CSV |
| `examples/stage1_configs/` | linked synthetic, primary-only synthetic, PubChem MEP2 snapshot 설정 |
| `examples/stage1_data_demo.py` | fixture cache로 build → Adapter.load → Auditor.audit를 실행하는 예제 |
| `tests/test_stage1_data_pipeline.py` | 1단계 회귀 테스트 |
| `pyproject.toml` | `data = ["httpx>=0.27,<1"]`, `chem = ["rdkit"]` 선택 의존성 |
| `README.md`, `docs/stage1_data.md`, `docs/stage1_data_pipeline.md` | 실행·데이터 경계·실데이터 snapshot 문서 |

## 데이터 준비 구현

### 수집과 cache

- `assaypilot.data fetch`만 네트워크를 사용한다.
- cache 파일 옆의 `.meta.json`에 request path, URL, 수집 시각, response format, SHA-256을 기록한다.
- cache 재사용에는 원본 SHA-256과 request path가 모두 일치해야 한다.
- 기본 요청 속도는 초당 2회 이하이며, HTTP 429와 5xx는 유한 횟수 재시도한다.
- 빈 응답, HTML 응답, 요청한 CSV/JSON 형식과 맞지 않는 content type은 오류로 처리한다.
- `build`는 네트워크에 접근하지 않고 로컬 cache만 읽는다.

### 정규화와 오류 처리

- 지원하는 결과 형식은 PubChem concise CSV이며 `AID`, `SID`, `CID`, `Activity Outcome` 열이 필요하다.
- SID는 실측 연결 식별자, CID는 PubChem 표준화 구조 연결 식별자로 보존한다. CID가 같아도 행을 합치지 않는다.
- 같은 SID가 서로 다른 CID에 대응하면 build 오류로 중단한다.
- raw verdict는 설정의 명시적 mapping으로만 `active`, `inactive`, `inconclusive`, `unspecified`로 바꾼다. 정의되지 않은 raw verdict는 오류다.
- 숫자 `0`, `<`, `<=`, `>`, `>=`를 보존한다. bool, NaN, infinity, 모호한 문자열은 수치로 받지 않는다.
- 원본에 verdict 또는 수치가 없는 행, 빈 필수 SID, 설정 AID와 다른 AID는 오류다.
- 현재 단위 변환은 명시적인 동일 단위 `identity`만 지원한다.
- 원본에 반복·조건 ID가 없으면 `not_reported:<stable-id>` 규칙을 사용한다.

### 산출물 경계

`build`의 출력은 다음과 같다.

| 경로 | 파일 |
| --- | --- |
| `public/` | `campaign.json`, `evidence/*.json`, `manifest.json` |
| `curator/` | `normalized_measurements.json`, `hidden_followup_measurements.json`, `data_audit_report.json`, `provenance.json` |

- public에는 선택 후보의 primary 관측만 `Observation`으로 넣는다.
- follow-up 측정은 curator의 `NormalizedMeasurement`로만 저장하며 `released_at`을 부여하지 않는다.
- public manifest에는 public 파일의 SHA-256만 기록한다. curator 파일 경로나 follow-up 통계는 기록하지 않는다.
- `PublicBundleAdapter`는 `public/manifest.json`의 허용 파일과 SHA-256을 확인하고 `PublicCampaign`을 반환한다. 경로 이탈, 누락 파일, 해시 불일치, 지원하지 않는 `DataSource.kind`는 거절한다.
- `PublicCampaignAuditor`는 `validate_public_campaign`을 실행하고, RDKit가 있으면 public candidate SMILES를 파싱한다. RDKit가 없으면 `rdkit_unavailable` 이슈를 반환한다.

## 설정과 예제

### Synthetic 설정

- `synthetic_linked.json`: primary AID 101과 confirmatory AID 102를 사용한다. primary active 후보 2개와 curator follow-up 측정 2개를 만든다.
- `synthetic_primary_only.json`: primary AID 101만 사용한다. follow-up이 없다는 `no_linked_followup` warning을 `DataAuditReport`에 남긴다.
- fixture는 synthetic이며 실제 PubChem 응답이나 생물학적 근거라고 표시하지 않는다.

### PubChem MEP2 snapshot 설정

파일: `examples/stage1_configs/pubchem_tor_mep2_snapshot.json`

- AID 2016을 MEP2 primary, AID 2272를 MEP2 confirmatory cherry-pick assay로 설정했다.
- 관계 근거로 설정한 공식 PubChem AID 504468 설명은 MEP2 primary AID 2016과 cherry-pick confirmatory AID 2272를 명시한다.
- 설정의 `initial_as_of`는 `2026-09-15T00:00:00Z`다.
- primary active SID 전체에서 stable ID 정렬 후 seed `20260915`로 5개를 선택한다. 이 seed는 snapshot 날짜다.
- 선택 CID 다섯 개의 `ConnectivitySMILES`만 별도 PubChem property 요청으로 가져온다.
- 설정의 비용 단위는 실제 비용이 아니라 `synthetic_credit`이고 `assumed: true`다.
- follow-up이 없는 후보는 inactive로 변환하지 않는다.

## 실제 실행한 검증

실행 환경은 conda `drug`였다. Pydantic 2.13.5와 pytest 9.1.1이 설치돼 있었고, `httpx 0.28.1`을 `.[data]` 선택 의존성으로 설치했다.

### 전체 테스트와 예제

```bash
conda run -n drug python -m pytest -q
```

결과:

```text
116 passed in 0.39s
```

```bash
conda run -n drug python examples/stage0_contract_demo.py
```

결과:

```text
SYNTHETIC synthetic-alpha: 3 candidates, 3 assays, 5 observations; references + JSON OK
SYNTHETIC synthetic-beta: 1 candidates, 1 assays, 0 observations; references + JSON OK
SYNTHETIC exchange contracts: references + JSON OK; available budget = 100.00
Stage 0 only: no training, budget mutation, Oracle execution, or biological validation performed.
```

```bash
conda run -n drug python examples/stage1_data_demo.py
```

결과:

```text
data_kind=synthetic campaign=stage1-synthetic-linked candidates=2 primary_observations=2
public_contract_checked=True hidden_followup_measurements=2 chemical_audit_ok=False
chemical audit skipped: install assaypilot[chem] to enable RDKit parsing
```

### 1단계 회귀 검증 범위

- synthetic build → public load → domain public 검사
- public 디렉터리만 복사한 상태에서 Adapter load
- follow-up raw label 변경 후 public `campaign.json`과 `manifest.json` 바이트 동일성
- primary-only warning
- 설정된 실데이터 AID와 역할
- 잘못된 AID 및 정의되지 않은 raw verdict에서 output 없이 build 실패
- 수치 0과 비교 연산자 보존
- bool, NaN, infinity, 모호한 수치 거절
- 수치만 있는 측정의 `unspecified` 보존과 완전 빈 행 거절
- SID/CID 충돌에서 build 실패
- public 파일 해시 변조 거절
- RDKit 미설치 이슈와 RDKit가 있다고 가정한 invalid SMILES 이슈
- cache 재사용, 다른 request path cache 미재사용, 503 후 retry, JSON content type 허용

### 실제 PubChem network smoke

다음 PUG-REST 요청을 임시 cache에 실행했다.

```text
assay/aid/504526/concise/CSV
```

응답은 성공했고 다음 헤더를 확인했다.

```text
"AID","SID","CID","Activity Outcome","Target Accession","Target GeneID","Activity Value [uM]","Activity Name","Assay Name","Assay Type","PubMed ID","RNAi"
```

### 실제 PubChem MEP2 build

다음 명령을 임시 경로에서 실행했다.

```bash
conda run -n drug python -m assaypilot.data fetch \
  examples/stage1_configs/pubchem_tor_mep2_snapshot.json /tmp/assaypilot-real.zzD33F/cache
conda run -n drug python -m assaypilot.data build \
  examples/stage1_configs/pubchem_tor_mep2_snapshot.json /tmp/assaypilot-real.zzD33F/cache /tmp/assaypilot-real.zzD33F/bundle
```

build `DataAuditReport` 결과:

| 항목 | 값 |
| --- | ---: |
| 포함 정규화 행 | 328,519 |
| 제외 행 | 0 |
| AID 2016 primary 행 | 326,763 |
| AID 2272 confirmatory 행 | 1,756 |
| active 판정 | 1,722 |
| inactive 판정 | 325,425 |
| inconclusive 판정 | 1,372 |
| 선택 후보 | 5 |
| curator follow-up 측정 | 1 |
| 감사 이슈 | 없음 |

PUG-REST concise CSV 확인 시 AID 2016 primary의 active 행은 1,682개였고, AID 2272의 1,756행과 active-primary SID 기준으로 공유한 SID는 295개였다.

생성된 public bundle에는 다음 검사를 실행했다.

```text
actual_public_domain_audit_ok= True
candidates= 5 observations= 5 assays= ['mep2-primary', 'mep2-confirmatory']
```

`PublicCampaignAuditor`의 `inspect`는 RDKit이 없는 `drug` 환경에서 다음을 출력하고 종료 코드 2를 반환했다.

```text
campaign=pubchem-tor-mep2-20260915 candidates=5 assays=mep2-primary,mep2-confirmatory observations=5 audit_ok=False
rdkit_unavailable pubchem-tor-mep2-20260915 candidates: chemical SMILES audit was not performed
```

따라서 public domain 참조 검사는 통과했지만 RDKit 화학 구조 감사는 실행되지 않았다.

## 공식 출처

- PubChem PUG-REST tutorial: <https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest-tutorial>
- PubChem BioAssay 문서: <https://pubchem.ncbi.nlm.nih.gov/docs/bioassays>
- PubChem RDF BioAssay 관계 문서: <https://pubchem.ncbi.nlm.nih.gov/docs/rdf-bioassay>
- PubChem AID 504468: <https://pubchem.ncbi.nlm.nih.gov/bioassay/504468>

## 현재 제한 사항

- RDKit은 선택 의존성으로 선언되어 있으나, 검증에 사용한 `drug` 환경에는 설치되어 있지 않았다. 따라서 실제 bundle의 chemical SMILES parsing 결과는 없다.
- 실제 PubChem 원본 cache와 생성 bundle은 `/tmp/assaypilot-real.zzD33F/`에 만들었고 저장소에 추가하지 않았다.
- fixture의 결과와 통과한 테스트는 모델 성능, 생물학적 타당성, 직접 결합 또는 치료 효능을 검증하지 않는다.
- public/curator 파일 분리는 데이터 의존성 분리이며, 동일 프로세스·계정의 파일 접근을 막는 접근 격리는 구현하지 않았다.
