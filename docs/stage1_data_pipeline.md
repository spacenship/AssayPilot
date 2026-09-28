# 1단계: 수집·정규화·감사·초기 공개 캠페인

## 실행 경계

`assaypilot.data fetch`만 PubChem PUG-REST에 접근한다. 요청 경로, 응답 content type, 응답 SHA-256, 수집 시각을 cache 파일 옆의 `.meta.json`에 저장한다. 같은 SHA-256 캐시는 `--refresh` 전까지 재사용한다. HTTP 429와 5xx는 제한된 횟수만 재시도하고, 2 요청/초를 넘지 않는다. 숫자와 HTTP-date 형식의 `Retry-After`를 모두 지원하되 대기 시간에는 유한 상한을 둔다. 응답과 metadata는 임시 파일에서 함께 준비한 뒤 교체하며, 교체 중 실패하면 기존 정상 cache와 metadata를 복구하고 `.partial`·`.backup`을 남기지 않는다.

`assaypilot.data build`는 cache만 읽는다. 설정의 AID, concise CSV 열, SID/CID, raw verdict 매핑, 수치와 단위를 확인한 뒤 정규화한다. 숫자 `0`, `<`, `<=`, `>`, `>=`는 보존하며 모호한 수치, 빈 필수 식별자, 정의되지 않은 raw verdict, AID 불일치는 오류로 중단한다. 출력은 임시 디렉터리에 먼저 작성하고 전체 공개·curator bundle 검증 뒤 교체한다. 파일 작성을 시작한 뒤 오류가 나도 임시 출력만 제거하며, 기존 정상 산출물을 훼손하지 않는다.

후보 구조에는 PubChem property 요청의 `SMILES` 열을 사용한다. 이 문자열은 `Candidate.original_smiles`에 그대로 보존되며, 정규화 문자열로 덮어쓰지 않는다. `ConnectivitySMILES` 요청은 별도 cache에 남기고 curator provenance에 요청 경로·응답 SHA-256·용도를 기록한다. `ConnectivitySMILES`는 입체배치나 동위원소를 복원하는 데 사용하지 않는다. `SMILES`가 없는 CID는 기본 정책(`smiles_required`)에서 build 오류가 된다. 명시적으로 `smiles_then_connectivity_with_warning`을 설정한 경우에만 connectivity fallback을 사용하며, 감사 보고서에 warning을 남긴다.

PubChem concise 형식의 `AID`, `SID`, `CID`, `Activity Outcome`을 필수로 둔다. SID는 동일한 시료 관측을 연결하는 식별자이고 CID는 구조를 붙이는 PubChem 물질 식별자다. CID가 같아도 결과 행을 합치지 않으며, 하나의 SID가 서로 다른 CID로 나타나면 중단한다.

## 공개와 개발자용 산출물

`build` 출력은 다음 두 디렉터리로 나뉜다.

| 위치 | 내용 | `PublicBundleAdapter` 접근 |
| --- | --- | --- |
| `public/` | `campaign.json`, 시험 정의 evidence, SHA-256 manifest, 초기 primary 관측 | 가능 |
| `curator/` | 모든 정규화 행, 선택 후보의 후속 측정, 원본 해시, 감사 보고서와 설정 | 불가 |

초기 후보는 오직 primary의 공개 규칙과 고정 seed로 고른다. 모든 정규화 행은 `curator/normalized_measurements.json`에 남고, 선택 후보에 연결된 primary 이외 행은 `curator/hidden_followup_measurements.json`에도 부분집합으로 기록되며 후보 선택과 public manifest에 영향을 주지 않는다. follow-up raw label을 바꿔도 public bundle 바이트가 바뀌지 않는 회귀 검사를 둔다.

유효한 입력에서 선택 후보에 연결된 후속 측정이 0건이어도 public primary bundle을 생성한다. 이 경우 `no_linked_followup` warning과 `followup_candidate_sids=0`, `followup_candidate_assay_pairs=0`, `unmeasured_selected_candidates=selected`를 curator `data_audit_report.json`에 기록하며 replay용 후속 자료가 확보되었다고 주장하지 않는다. 파일 누락, 파싱 오류, 잘못된 AID·판정·구조처럼 입력 자체가 유효하지 않은 경우는 정상적인 0건으로 처리하지 않고 오류로 중단한다.

`DataAuditReport`의 개발자용 통계에는 assay별 원본·포함·제외·오류 행 수와 verdict 분포, 고유 SID/CID와 SID↔CID 다중 대응, 반복·상충 `(assay_id, SID)` 그룹, primary 원본 행·active 원본 행·고유 SID·최종 선택 수, 선택 후보에 연결된 후속 SID·후보×시험 조합·판정 분포·미측정 수를 분모와 함께 기록한다. 이 값은 실제 산출물인 `curator/data_audit_report.json`에만 두며 공개 campaign·evidence·런타임 inspect에는 노출하지 않는다.

## 설정과 provenance

`CampaignConfig`는 스키마 버전, AID와 assay role, raw cache key, endpoint/unit, raw verdict mapping, 변환 정책, 비용, 선행조건, 후보 선택 규칙과 `initial_as_of`를 명시한다. raw file key와 assay ID는 중복될 수 없고, 후보 규칙은 정의된 assay를 가리켜야 한다. 현재 구현은 명시적인 동일 단위(`identity`)만 허용한다.

원본 response, config, 모든 public 파일은 SHA-256으로 추적한다. public evidence에는 시험 정의와 원본 위치를 넣고, 공개된 primary 관측마다 SID·AID·원본 `Activity Outcome`·원본 행 hash와 최소 raw row를 연결한다. 후속 측정 범위와 data audit 통계는 넣지 않는다. concise export가 범주형 `Activity Outcome`만 제공할 때는 `endpoint_scope=categorical_activity_outcome_only`, `unit=categorical`, `raw_endpoint_column=null`, `raw_unit=null`을 설정하고, 정규화 단계에서 `Activity Name`이 비어 있는지와 `Activity Value*` 수치 열이 비어 있는지를 실제 검사한다. AID 관계 설명(`relationship_cache_key`)은 developer-only raw cache로 보존하며 실행 assay로 만들지 않는다. `PublicBundleAdapter`는 manifest의 상대 경로 탈출, 누락 파일, 해시 불일치, 공개 외 경로를 거절한다.

## 감사와 한계

`PublicCampaignAuditor`는 먼저 stage 0의 `validate_public_campaign`을 실행한다. RDKit가 설치돼 있으면 공개 후보 SMILES를 `MolFromSmiles(..., sanitize=True)`로 파싱·sanitization하고, `inspect`는 후보 수·구조 수·검사·통과·실패 수를 출력한다. RDKit가 없으면 `rdkit_unavailable` 감사 이슈와 검사 0건을 반환한다. 이 경우 화학 구조 검증은 수행되지 않았으므로 통과로 해석하지 않는다. 이 검사는 SMILES 표현의 유효성 검사이며 활성·결합·안전성이나 약물 적합성을 평가하지 않는다.

`examples/stage1_fixture/`과 `synthetic_*.json`은 네트워크 없는 테스트 fixture다. 실제 PubChem AID와 그 primary/follow-up 관계, endpoint와 단위, assay description은 별도 source review로 확인해야 한다. 이 저장소는 아직 특정 표적의 실제 후보나 생물학적 결론을 배포하지 않는다.

## 03–04 snapshot과 endpoint 의미 검토

`data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-before-03-04/`에 03–04 수정 전의 cache, bundle, inspect 결과와 당시 설정을 보존한다. 보완 revision `revision-20260917-r2/`에는 원본 response와 `.meta.json`, 설정 사본, public/curator bundle, 검증 로그, 실행 patch·변경 source를 담은 `implementation/`, `snapshot_manifest.json`을 둔다. 마지막 manifest는 public manifest와 분리된 developer-only 전체 SHA-256 목록이다.

AID 2016/2272의 concise CSV는 `Activity Outcome`만 내부 판정으로 읽는다. `Activity Outcome`은 PubChem에 기록된 값을 내부 `verdict`로 매핑할 뿐이며, 이 구현이 판정을 재계산하지 않는다. 각 행의 `Activity Name`과 `Activity Value [uM]`는 실제로 비어 있으므로, 공식 assay description의 GFP percent response, Z-prime, mother/daughter response를 행별 수치로 재구성하지 않는다. 공식 `Inconclusive` 의미는 assay protocol의 정의로 설정하고, 이번 snapshot에서 관측되지 않은 범주는 developer-only audit 통계에만 남긴다. AID 504468 description은 2016 primary와 2272 cherry-pick confirmatory 관계를 뒷받침하는 raw 근거이며, 실행 assay 목록에는 포함하지 않는다. 상세 매핑과 재현 결과는 `reports/stage1_completion/02_snapshot_semantics.md`에 기록한다.

## 05–06 경계 검증과 확장 후보군

작은 smoke 설정 `examples/stage1_configs/pubchem_tor_mep2_snapshot.json`과 `data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2/`는 5개 후보 구성을 그대로 유지한다. 후속 행의 판정·수치·순서·설명 텍스트를 바꾸거나 전부 제거하는 fixture를 build해도 public 후보, Observation, evidence, manifest 해시는 동일하며, follow-up 0건은 curator 진단과 미측정 통계로만 남긴다. 같은 SID·시험의 반복 행과 상충 판정은 제거·평균·덮어쓰지 않고 원본 측정으로 보존하며, 원본에 반복 조건 정보가 없으면 `not_reported`로 표시한다.

확장 설정 `examples/stage1_configs/pubchem_tor_mep2_primary_active_all.json`과 revision `data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all/`은 공개 primary 정보만으로 후보를 정한다. AID 2016의 Active **원본 행 1,682개**를 먼저 세고, 그 행에서 고유 SID를 계산한 결과 1,682 SID를 선택했다. AID 2272 후속 존재 여부는 후보 선정에 사용하지 않는다. 실제 후속 coverage는 295 SID(Active 30, Inactive 265), 미측정 선택 후보는 1,387개다. 구조는 선택된 primary CID를 100개씩 batch 요청해 `SMILES`와 `ConnectivitySMILES`를 각각 보존하고, RDKit sanitize는 1,682/1,682 통과했다. 확장 build·fetch·public-only adapter/auditor·고정 cache 오프라인 재현 결과는 `reports/stage1_completion/03_boundaries_handoff.md`에 기록한다.

## 확인한 PubChem snapshot 예제

`examples/stage1_configs/pubchem_tor_mep2_snapshot.json`은 실데이터용 별도 설정이다. 보존된 AID 2272 공식 description은 primary hit의 **single-concentration confirmatory** 결과와 single-plex readout을 설명한다. AID 504468은 별도의 dose-response confirmatory SAR assay이므로 AID 2272의 categorical concise 행에서 IC50·EC50 등의 수치를 재구성하지 않는다. 2026-09-15에 PUG-REST concise CSV를 실제로 받아 확인한 결과 AID 2016의 326,763행 중 active 1,682행과 AID 2272의 1,756행 사이에는 active-primary SID 기준으로 295개의 공유 SID가 있었다. 설정은 이 전체 primary cache에서 stable ID 정렬과 snapshot date seed 20260915로 다섯 SID를 선택하며, 그 snapshot에서 AID 2272와 연결된 행은 하나다.

구조 property 요청은 이 다섯 CID의 `SMILES`와 `ConnectivitySMILES`를 각각 명시한다. 전자는 candidate 원본 구조이고, 후자는 별도 provenance reference다. 원본이 갱신되어 후보가 달라지면 구조 cache 누락으로 build가 중단된다. 이는 후속 결과나 구조를 보고 후보를 조용히 바꾸지 않기 위한 의도된 재검토 지점이다. 결과의 학술적·치료적 의미는 검증하지 않으며, `active`는 해당 PubChem assay outcome일 뿐이다.
