# 1단계 마무리 01–02: 구조 수집과 RDKit 감사

실행일: 2026-09-17

이 기록은 1단계 마무리 지침의 다음 두 항목에 한정한다.

1. 입체정보를 보존하는 SMILES 수집 정책 보완
2. 실제 RDKit 화학 감사 완료

기존 `docs/stage1_implementation_report.md`의 2026-09-15 실행 기록은 수정하지 않았다. 이 문서는 이번 실행의 별도 기록이다.

## 변경 내용

| 파일 | 변경 이유 |
| --- | --- |
| `src/assaypilot/data/schemas.py` | `CampaignConfig`에 `SMILES` source cache, 별도 `ConnectivitySMILES` cache, 구조 선택 정책을 명시했다. 기본 정책은 `smiles_required`다. |
| `src/assaypilot/data/build.py` | `Candidate.original_smiles`와 `NormalizedMeasurement.original_smiles`에 PubChem `SMILES` 열의 수신 문자열을 그대로 넣는다. 별도 `ConnectivitySMILES` response는 바꾸지 않고 보관하며 curator provenance에 속성명, cache key, 요청 경로, response SHA-256, 용도를 기록한다. |
| `src/assaypilot/data/build.py` | `SMILES`가 없는 선택 CID는 `missing_smiles` 오류로 build를 중단한다. `smiles_then_connectivity_with_warning`을 명시한 경우에만 ConnectivitySMILES fallback을 쓰고 `connectivity_smiles_fallback` warning을 남긴다. |
| `src/assaypilot/data/audit.py` | `PublicCampaignAuditor`가 실제 `rdkit.Chem.MolFromSmiles(..., sanitize=True)`를 사용한다. 검사 backend, 후보 수, 구조 존재 수, 검사·통과·실패 수를 `ChemicalAuditSummary`로 남긴다. Protocol의 `Auditor.audit(PublicCampaign) -> AuditResult` 반환 계약은 변경하지 않았다. |
| `src/assaypilot/data/cli.py` | `inspect`가 chemical audit summary를 출력한다. |
| `examples/stage1_configs/*.json` | synthetic과 MEP2 설정에서 `SMILES`와 `ConnectivitySMILES`를 서로 다른 raw cache와 PUG-REST 요청으로 선언했다. MEP2의 구조 요청은 `.../property/SMILES/CSV`와 `.../property/ConnectivitySMILES/CSV`다. |
| `examples/stage1_fixture/compounds_smiles.csv`, `compounds_connectivity.csv` | 입체배치가 반대인 `C[C@H](O)[13CH3]`와 `C[C@@H](O)[13CH3]`, 동위원소 `[13CH3]`, 입체정보가 없는 `CC(N)O` fixture를 추가했다. 이전 단일 ConnectivitySMILES fixture는 제거했다. |
| `tests/test_stage1_data_pipeline.py` | 입체배치·동위원소·입체정보 없는 SMILES 보존, 명시적 fallback warning, 속성 요청 경로가 다른 cache 미재사용, 실제 RDKit 정상·잘못된 SMILES 경로를 검사한다. |
| `docs/stage1_data_pipeline.md` | 현재 구조 속성 정책과 inspect 출력 범위를 기록했다. |
| `.gitignore` | 실행 산출물 `outputs/`를 작업 데이터로 보관하고 Git 추적에서 제외했다. |

`SMILES`는 PubChem CID의 표준화 구조 문자열이다. 이 구현은 이를 depositor가 제출한 원래 구조라고 표시하지 않는다. 임의의 stereoisomer, 동위원소, salt 또는 tautomer 변환을 추가하지 않는다.

## 사용한 구조 속성과 provenance

MEP2 실제 build의 curator provenance는 다음 구조 정책을 기록했다.

```text
selection_policy: smiles_required
candidate_original_smiles.property: SMILES
candidate_original_smiles.cache_key: compounds_smiles.csv
candidate_original_smiles.request_path:
  compound/cid/1150217,1043268,1129613,1093365,1037240/property/SMILES/CSV
candidate_original_smiles.response_sha256:
  4b06e87f2d2548e13c7742c240a459530f142fdd31dd14c16efa1f9a03dcd67b
connectivity_reference.property: ConnectivitySMILES
connectivity_reference.cache_key: compounds_connectivity.csv
connectivity_reference.request_path:
  compound/cid/1150217,1043268,1129613,1093365,1037240/property/ConnectivitySMILES/CSV
connectivity_reference.response_sha256:
  20bdbd4d15566c00ecc2fb2cdcba1edbeb82664e855a277ec6f21a8cb2fe8c66
```

2026-09-17에 받은 새 PubChem response의 실제 헤더는 `"CID","SMILES"`였다. 이 다섯 CID의 이번 `SMILES` 응답에는 `@` 또는 동위원소 표기가 없었다. fixture는 입체정보·동위원소 보존을 별도로 검증한다.

이전 설정은 `compounds.csv` 하나에 `ConnectivitySMILES`만 요청했다. 현재 설정은 `compounds_smiles.csv`를 후보 원본 구조로 사용하고, 이전 ConnectivitySMILES response를 `compounds_connectivity.csv`로 별도 보존한다. 후보 선정 규칙, seed `20260915`, SID/CID 연결, assay·판정·비용 의미는 변경하지 않았다.

## 환경

모든 Python 명령은 conda `drug` 환경에서 실행했다.

```text
Python 3.12.14
pydantic 2.13.5
httpx 0.28.1
pytest 9.1.1
rdkit 2026.03.6
assaypilot 0.1.0
```

## 실행과 결과

### 오프라인 회귀와 synthetic 감사

```bash
conda run -n drug python -m pytest -q
```

```text
120 passed in 0.44s
```

추가·변경한 검증은 다음을 포함한다.

- `SMILES` 문자열의 `@`, `@@`, `[13CH3]`가 정규화와 public bundle 뒤에도 그대로 남는다.
- 반대 입체배치 두 문자열은 같은 ConnectivitySMILES로 합쳐지지 않는다.
- 입체정보 없는 `CC(N)O`도 그대로 처리된다.
- ConnectivitySMILES fallback은 명시한 정책에서만 warning과 함께 실행된다.
- ConnectivitySMILES request cache는 `SMILES` request로 재사용되지 않는다.
- 실제 RDKit이 정상 fixture SMILES 두 개를 sanitize하고, `not a smiles`를 invalid SMILES로 검출한다.
- RDKit 미설치 분기의 `rdkit_unavailable` 진단도 mock된 import 경로에서 유지한다.

```bash
conda run -n drug python examples/stage1_data_demo.py
```

```text
data_kind=synthetic campaign=stage1-synthetic-linked candidates=2 primary_observations=2
public_contract_checked=True hidden_followup_measurements=2 chemical_audit_ok=True
```

synthetic 검사 범위는 후보 2개, 구조 존재 2개, RDKit 검사 2개, 통과 2개, 실패 0개다.

### 실제 PubChem MEP2 bundle

기존 2026-09-15 AID 2016·2272 cache와 ConnectivitySMILES cache는 metadata의 request path와 SHA-256이 일치해 재사용했다. 새 `SMILES` 속성 response 하나만 2026-09-17에 수집했다.

```bash
conda run -n drug python -m assaypilot.data fetch \
  examples/stage1_configs/pubchem_tor_mep2_snapshot.json \
  outputs/stage1_completion/01_structure_audit/cache

conda run -n drug python -m assaypilot.data build \
  examples/stage1_configs/pubchem_tor_mep2_snapshot.json \
  outputs/stage1_completion/01_structure_audit/cache \
  outputs/stage1_completion/01_structure_audit/bundle

conda run -n drug python -m assaypilot.data inspect \
  outputs/stage1_completion/01_structure_audit/bundle/public/manifest.json
```

build 결과:

| 항목 | 값 |
| --- | ---: |
| 정규화 행 | 328,519 |
| 제외 행 | 0 |
| AID 2016 primary 행 | 326,763 |
| AID 2272 confirmatory 행 | 1,756 |
| active / inactive / inconclusive | 1,722 / 325,425 / 1,372 |
| 선택 후보 | 5 |
| curator follow-up 측정 | 1 |
| build DataIssue | 0 |

inspect 결과:

```text
campaign=pubchem-tor-mep2-20260915 candidates=5 assays=mep2-primary,mep2-confirmatory observations=5 audit_ok=True
chemical_audit_backend=rdkit candidates=5 structures_present=5 checked=5 passed=5 failed=0
```

실제 MEP2 감사 범위는 후보 5개, 구조 존재 5개, RDKit 검사 5개, 통과 5개, 실패 0개다. 검사하지 못한 후보는 없다. 이 결과는 SMILES 표현의 RDKit 파싱·sanitization 결과이며 biological activity, binding, safety 또는 drug suitability 검증 결과가 아니다.

## 산출물과 재사용 위치

| 경로 | 내용 |
| --- | --- |
| `examples/stage1_configs/pubchem_tor_mep2_snapshot.json` | 다음 작업이 사용할 구조 속성·후보 선택·AID 설정 |
| `outputs/stage1_completion/01_structure_audit/cache/` | 재사용한 assay/ConnectivitySMILES cache와 2026-09-17 SMILES response 및 `.meta.json` |
| `outputs/stage1_completion/01_structure_audit/bundle/` | 새 public/curator bundle |
| `outputs/stage1_completion/01_structure_audit/inspect.txt` | 실제 RDKit inspect 로그 |
| `reports/stage1_completion/01_structure_audit.md` | 이번 실행 기록 |

cache 크기는 58 MB, bundle 크기는 341 MB다. `outputs/`는 Git 추적에서 제외했다.

## 남은 범위

- 이번 새 SMILES response는 기존 2026-09-15 assay cache와 시점이 다르다. 기존 raw cache가 남아 있어 assay 결과를 다시 수집하지 않았으며, provenance와 각 cache metadata가 수집 시각·request path·SHA-256을 보존한다.
- 완전한 snapshot 보관·재현 절차는 이 묶음에서 구현하지 않았다.
- 대규모 후보군 확장, ReplayOracle, 예산 처리, 학습·LLM·UI는 구현하지 않았다.

## 출처

- PubChem PUG-REST: <https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest>
- PubChemPy `connectivity_smiles` 설명: <https://docs.pubchempy.org/en/latest/api.html#pubchempy.Compound.connectivity_smiles>
