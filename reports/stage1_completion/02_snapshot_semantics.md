# 1단계 마무리 03–04: snapshot 재현과 assay 의미 검토

실행일: 2026-09-17

## 적용 범위

사용자 요청은 첨부 지침 `docs/assaypilot-stage1-finish-03-04.md`를 이어서 수행하는 것이다. 첨부 지침의 항목 03(원본 snapshot 보존·오프라인 재현)과 04(AID endpoint·verdict·공개 evidence 의미 검토)만 적용했다. ReplayOracle, 예산 차감, 학습, agent loop, UI, 다음 단계 bundle은 시작하지 않았다.

이전 01–02 변경인 PubChem `SMILES` 원본 보존과 실제 RDKit audit은 유지했다. 이번 보완에서는 endpoint 범위와 관계 source를 명시하는 기존 정책에 더해, 원본 trace 연결·category-only 입력 검사·판정 의미의 공개/개발자 분리·실행 소스 보존을 실제 코드와 snapshot에 반영했다.

## 변경한 코드와 설정

| 경로 | 내용 |
| --- | --- |
| `src/assaypilot/data/schemas.py` | `AssayMapping`에 `endpoint_scope`, `endpoint_meaning`, `official_result_names`, `activity_name_policy`를 추가하고 category-only 설정에서 `unit=categorical`, 수치 열·단위를 금지한다. `NormalizedMeasurement`에 원본 행 번호를 보존한다. `CampaignConfig.relationship_cache_key`는 실행 assay가 아닌 관계 설명 raw cache를 해시·보존한다. |
| `src/assaypilot/data/normalize.py` | category-only 입력에서 `Activity Name`이 비어 있는지와 `Activity Value*` 수치 열이 비어 있는지를 실제 원본 행마다 검사한다. |
| `src/assaypilot/data/build.py` | public evidence JSON에 endpoint 범위·공식 result name·concise Activity Name 상태·raw 열 범위를 기록하고, 공개 primary 관측마다 SID·AID·원본 `Activity Outcome`·source row와 hash를 연결한다. 후속 측정과 통계는 기록하지 않는다. |
| `examples/stage1_configs/pubchem_tor_mep2_snapshot.json` | AID 2016/2272를 `categorical_activity_outcome_only`로 선언하고 공식 result name을 기록했다. AID 504468 description을 `aid504468_description.json` developer-only 관계 cache로 선언했지만 `assays`에는 추가하지 않았다. |
| `docs/stage1_data_pipeline.md` | category-only export, 관계 cache, snapshot 위치와 검토 범위를 문서화했다. |
| `scripts/compare_public_tree.py` | 두 public tree의 상대 경로·SHA-256을 비교하는 오프라인 재현 검증 명령이다. |
| `scripts/write_snapshot_manifest.py` | public manifest와 분리된 developer-only 전체 파일 hash, commit·dirty 상태, Python/dependency 정보를 기록하고, 실제 실행에 사용한 HEAD 대비 patch와 변경 source/config 파일을 `implementation/`에 복사한다. |

## 실제 원본 행과 공식 정의의 대조

### AID 2016 — primary

| 항목 | 확인 결과 |
| --- | --- |
| biological system/readout | *S. cerevisiae* 5개 GFP strain을 multiplex로 측정하는 TOR pathway screen. MEP2는 GLN3 branch이며 GFP fluorescence/flow cytometry readout이다. |
| role/evidence | `mep2-primary`, primary. 공식 정의 URL은 <https://pubchem.ncbi.nlm.nih.gov/bioassay/2016>, raw description은 `raw/aid2016_description.json`이다. |
| raw outcome/internal verdict | concise `Activity Outcome`: `Active`→`active`, `Inactive`→`inactive`, `Inconclusive`→`inconclusive`. PubChem에 기록된 `Activity Outcome`을 내부 verdict로 매핑하며, 이 구현이 threshold나 판정을 재계산하지 않는다. |
| Activity Name/endpoint | 실제 326,763개 행 모두 `Activity Name`이 비어 있다. 공개 endpoint는 `PubChem Activity Outcome`이며, 공식 정의의 `RESPONSE`는 DMSO 대비 GFP percent response, `Z_PRIME`는 assay quality 결과다. |
| numeric/unit/comparator | concise `Activity Value [uM]`는 326,763개 모두 비어 있다. 공식 protocol은 10 µM 단일 농도, Active 기준 `% response > 175`, event 수가 70 미만이면 inconclusive라고 설명하지만, 이 snapshot 행에는 해당 수치가 없다. 따라서 µM 값이나 IC50/EC50/binding을 만들지 않는다. |
| success/prerequisite | primary 자체의 성공 조건은 설정하지 않는다. 후보는 `primary_active` 고정 seed 규칙으로 선택하며 후속 미측정은 음성으로 바꾸지 않는다. |
| raw counts | Active 1,682, Inactive 323,709, Inconclusive 1,372. `AID`는 모든 행에서 2016이다. |

### AID 2272 — confirmatory cherry-pick

| 항목 | 확인 결과 |
| --- | --- |
| biological system/readout | 같은 MEP2 *S. cerevisiae* GFP readout의 single-plex cherry-pick 확인이다. Alexa 633으로 mother/daughter를 구분하고 GFP를 측정한다. |
| role/evidence | `mep2-confirmatory`, confirmatory. 공식 정의 URL은 <https://pubchem.ncbi.nlm.nih.gov/bioassay/2272>, raw description은 `raw/aid2272_description.json`이다. prerequisite는 `mep2-primary`의 `active`다. |
| raw outcome/internal verdict | concise `Active`→`active`, `Inactive`→`inactive`. `Inconclusive`는 assay protocol에서 정의된 공식 범주로 설정했으며, checked CSV에 해당 행이 없다는 분포 사실은 developer-only audit 통계에만 남긴다. |
| Activity Name/endpoint | 실제 1,756개 행 모두 `Activity Name`이 비어 있다. 공식 결과 이름은 `RESPONSE`, `Z_PRIME`, `RESPONSE_MOTHERS`, `RESPONSE_DAUGHTERS`; concise export에는 이 수치가 없다. |
| numeric/unit/comparator | `Activity Value [uM]`는 1,756개 모두 비어 있다. 공식 protocol은 10 µM, Active 기준 `% response > 150`, event 수 70 미만 inconclusive를 설명하지만, 이 export에서 수치를 재구성하지 않는다. |
| success/prerequisite | campaign success condition은 “configured assay에서 confirmatory `active`”이다. missing follow-up은 inactive나 실패로 집계하지 않는다. |
| raw counts | Active 40, Inactive 1,716. `AID`는 모든 행에서 2272이다. |

### AID 504468 — 관계 evidence only

`raw/aid504468_description.json`은 PubChem AID 504468 description 원본이다. description은 AID 2016에서 326,763개 중 1,682개 active였고, 그 primary hit들이 AID 2272/2622 confirmatory selection으로 평가되었다고 명시한다. 이 관계는 assay 이름이나 AID 숫자의 유사성으로 추론하지 않고 이 공식 description에서 확인했다. 또한 이 AID 자체는 dose-response confirmatory SAR이며 EC50(µM)와 `EC50_MICROM` 등의 결과 정의를 갖는다. 이 관계와 결과 정의는 2016/2272의 concise categorical outcome을 수치 endpoint로 바꾸는 근거가 아니다. AID 504468은 `relationship_cache_key`로만 보존했고 `assays`와 공개 observation에는 넣지 않았다.

### 대표 concise 행

대표 행을 실제 CSV에서 직접 읽었다. 아래 `Activity Value [uM]`와 `Activity Name`은 모두 빈 문자열이다.

| 파일 | raw outcome | SID | CID | 빠진 필드 |
| --- | --- | ---: | ---: | --- |
| `aid2016_concise.csv` | Active | 842264 | 644523 | Activity Name, Activity Value [uM] |
| `aid2016_concise.csv` | Inactive | 842121 | 6603008 | Activity Name, Activity Value [uM] |
| `aid2016_concise.csv` | Inconclusive | 842124 | 644371 | Activity Name, Activity Value [uM] |
| `aid2272_concise.csv` | Active | 14735741 | 5333955 | Activity Name, Activity Value [uM] |
| `aid2272_concise.csv` | Inactive | 844677 | 647008 | Activity Name, Activity Value [uM] |

AID 2272 concise snapshot에는 Inconclusive category가 없었다. 따라서 이 파일에 없는 category나 공식 assay의 numeric result를 내부 verdict로 추정하지 않는다.

## 요청한 5개 점검의 수정 결과

| 점검 | 확인·수정 결과 |
| --- | --- |
| 실행 코드 보존 | `snapshot_manifest.json`의 `implementation_artifacts`에 HEAD 대비 `implementation/working_tree.patch`와 변경된 source/config 13개를 보관했다. patch SHA-256은 `0531982dd92fffe955781463c64210bbe0aa5b6c817e35bca687a242b186ac4a`이며 `implementation/RESTORE.txt`에 적용·복사 절차를 기록했다. |
| 관측–원본 근거 연결 | 새 public AID 2016 evidence에 초기 primary 관측 5개 각각의 observation ID, SID, AID, 원본 `Activity Outcome`, source row ID·번호·파일 SHA-256과 최소 raw row를 넣었다. AID 2272 confirmatory evidence에는 trace를 넣지 않았다. |
| 판정 의미–분포 분리 | AID 2016/2272의 `inconclusive` 의미를 공식 assay 범주로 고정했다. AID 2272 checked CSV에 해당 행이 없다는 사실과 전체 verdict counts는 curator `data_audit_report.json` 및 이 개발자 보고서에만 남겼고, public campaign/evidence에는 넣지 않았다. |
| endpoint 정책 적용 | schema가 category-only에 `unit=categorical`, `raw_endpoint_column=null`, `raw_unit=null`을 강제하고, normalizer가 실제 행의 `Activity Name` 비어 있음과 `Activity Value*` 비어 있음을 검사한다. 잘못된 Activity Name·수치값·uM 설정을 거절하는 테스트를 추가했다. |
| 판정 생성 주체 표현 | 근거 없는 계산 주체 서술을 제거하고 “PubChem에 기록된 `Activity Outcome`을 내부 verdict로 매핑하며 자체적으로 재계산하지 않는다”로 통일했다. |

## 공개 evidence 검토

최종 public evidence는 다음을 포함한다.

- 공식 PubChem AID와 protocol URL, assay name
- `endpoint_scope=categorical_activity_outcome_only`
- 공식 result name 목록
- `Activity Name`이 concise 행에서 비어 있다는 정책
- raw outcome 열 이름과 공개 endpoint의 의미
- 공개 primary 관측별 최소 추적 행: observation ID, SID, AID, 원본 `Activity Outcome`, 원본 행 번호·파일 hash와 raw row

`bundle/public/manifest.json`의 `files`가 `campaign.json`과 두 evidence 파일을 직접 관리하고, public observation은 해당 `EvidenceRef.location`을 통해 이 evidence를 참조한다. primary trace는 공개 관측을 원본 행으로 확인하기 위한 최소 내용이며, confirmatory evidence에는 trace를 넣지 않는다. public evidence에는 follow-up 범위, Active 개수·양성률 등 분포 통계, curator 경로를 넣지 않았다. manifest SHA-256 일치 여부는 파일 무결성 검증이며 assay의 생물학적 적합성이나 endpoint의 과학적 타당성을 보증하지 않는다.

실제 공개 파일을 curator 없이 복사한 뒤 adapter와 RDKit auditor를 실행했다. 두 inspect 결과는 다음과 같다.

```text
campaign=pubchem-tor-mep2-20260915 candidates=5 assays=mep2-primary,mep2-confirmatory observations=5 audit_ok=True
chemical_audit_backend=rdkit candidates=5 structures_present=5 checked=5 passed=5 failed=0
```

로그는 `data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/verification/inspect_public_copy.log`에 있다.

## Snapshot 보존

03–04 수정 전 산출물은 다음 위치에 복사해 보존했다.

```text
data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-before-03-04/
```

03–04 보완 revision은 다음 구조다.

```text
data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/
├── config/pubchem_tor_mep2_snapshot.json
├── raw/                         # response bytes + 각 .meta.json
├── bundle/public/
├── bundle/curator/
├── verification/
├── implementation/              # 실행 patch + 변경 source/config 복구 자료
└── snapshot_manifest.json       # public manifest와 분리된 전체 SHA-256 목록
```

최종 snapshot의 중요한 source hash는 다음과 같다.

```text
raw/aid2016_concise.csv       92676cf1a3b692ba7c55f0831674a6b38a820fd24c1e584ed6e2df3529a0e603
raw/aid2016_description.json  effefab27dc5eab642d95a4b11bda8edb6081f9cb7280301c949963371ba58e2
raw/aid2272_concise.csv       216687f278a1af586f8aafbb33306b6475c14375e6ccaf755f57ed96f44a48c1
raw/aid2272_description.json  981a4a37fbf81ca6283715d508f6c6fcb89a89ad1c9edd99f222690f138068c1
raw/aid504468_description.json de4c899c2d3d3ecadda50e028d7cb0368ce9fc32ab11ae093000ba66541cf5ae
raw/compounds_smiles.csv      4b06e87f2d2548e13c7742c240a459530f142fdd31dd14c16efa1f9a03dcd67b
raw/compounds_connectivity.csv 20bdbd4d15566c00ecc2fb2cdcba1edbeb82664e855a277ec6f21a8cb2fe8c66
```

실행 당시 구현 식별자는 `HEAD=5298c52aa52e73d726e21f3aa4803964cf21fc29`였고 working tree에는 이전 Stage 1 변경과 이번 변경이 uncommitted 상태였다. Python 3.12.14, pydantic 2.13.5, httpx 0.28.1, RDKit 2026.3.6을 `snapshot_manifest.json`에 기록했다. `implementation/working_tree.patch`에는 HEAD 대비 tracked 변경 patch를, `implementation/changed_files/`에는 untracked/new source와 config를 복사해 두었으며 `implementation/RESTORE.txt`에 복구 절차를 남겼다. 원본 response와 metadata는 Git에 추가하지 않았고, snapshot은 작업 디렉터리의 별도 데이터 보관 위치에 남겼다.

## 오프라인 재현과 결과

네트워크를 사용하지 않고 동일 raw cache로 새 output을 만들었다.

```bash
conda run -n drug python -m assaypilot.data build \
  data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/config/pubchem_tor_mep2_snapshot.json \
  data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/raw \
  data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/verification/reproduced_bundle
```

public tree 비교 명령과 결과:

```bash
conda run -n drug python scripts/compare_public_tree.py \
  data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/bundle/public \
  data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/verification/reproduced_bundle/public \
  --output data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/verification/public_comparison.json
```

실제 실행 경로와 저장된 비교 결과는 위 명령의 `pubchem-tor-mep2-20260915/revision-20260917-r2` 경로에 있다. 결과는 다음과 같다.

```json
{"identical": true, "missing_in_reproduced": [], "extra_in_reproduced": [], "changed": []}
```

`campaign.json`, 두 evidence 파일, `manifest.json`의 상대 경로와 바이트 SHA-256이 모두 동일했다. `curator/data_audit_report.json`의 `created_at`처럼 실행 시각을 담는 개발자용 runtime metadata는 비교 대상 public tree에서 제외했다. 재현 output을 기존 성공 output에 덮어쓰지 않았으며 입력 raw, 새 결과, build log, 비교 결과를 모두 보존했다.

최종 확인:

```bash
conda run -n drug python -m pytest -q
```

```text
124 passed in 0.49s
```

본 검증은 raw cache 고정성, 공개 package 무결성, SMILES의 RDKit 파싱, assay category 의미의 명시성을 확인한다. biological validity, binding, efficacy, safety, model performance를 주장하지 않는다.
