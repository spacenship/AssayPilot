# 1단계: 수집·정규화·감사·초기 공개 캠페인

## 실행 경계

`assaypilot.data fetch`만 PubChem PUG-REST에 접근한다. 요청 경로, 응답 content type, 응답 SHA-256, 수집 시각을 cache 파일 옆의 `.meta.json`에 저장한다. 같은 SHA-256 캐시는 `--refresh` 전까지 재사용한다. HTTP 429와 5xx는 제한된 횟수만 재시도하고, 2 요청/초를 넘지 않는다.

`assaypilot.data build`는 cache만 읽는다. 설정의 AID, concise CSV 열, SID/CID, raw verdict 매핑, 수치와 단위를 확인한 뒤 정규화한다. 숫자 `0`, `<`, `<=`, `>`, `>=`는 보존하며 모호한 수치, 빈 필수 식별자, 정의되지 않은 raw verdict, AID 불일치는 오류로 중단한다. 빌드 실패 시 출력 디렉터리를 만들지 않는다.

PubChem concise 형식의 `AID`, `SID`, `CID`, `Activity Outcome`을 필수로 둔다. SID는 동일한 시료 관측을 연결하는 식별자이고 CID는 구조를 붙이는 PubChem 물질 식별자다. CID가 같아도 결과 행을 합치지 않으며, 하나의 SID가 서로 다른 CID로 나타나면 중단한다.

## 공개와 개발자용 산출물

`build` 출력은 다음 두 디렉터리로 나뉜다.

| 위치 | 내용 | `PublicBundleAdapter` 접근 |
| --- | --- | --- |
| `public/` | `campaign.json`, 시험 정의 evidence, SHA-256 manifest, 초기 primary 관측 | 가능 |
| `curator/` | 모든 정규화 행, 선택 후보의 후속 측정, 원본 해시, 감사 보고서와 설정 | 불가 |

초기 후보는 오직 primary의 공개 규칙과 고정 seed로 고른다. 후속 시험 측정은 `curator/hidden_followup_measurements.json`에만 남으며 후보 선택과 public manifest에 영향을 주지 않는다. follow-up raw label을 바꿔도 public bundle 바이트가 바뀌지 않는 회귀 검사를 둔다.

primary-only 설정은 연결된 후속 측정이 없다는 warning을 감사 보고서에 기록한다. `data_kind="pubchem"`에서 연결된 follow-up이 없으면 build를 오류로 처리하므로 실제 연결을 증명하지 않은 캠페인을 만들지 않는다.

## 설정과 provenance

`CampaignConfig`는 스키마 버전, AID와 assay role, raw cache key, endpoint/unit, raw verdict mapping, 변환 정책, 비용, 선행조건, 후보 선택 규칙과 `initial_as_of`를 명시한다. raw file key와 assay ID는 중복될 수 없고, 후보 규칙은 정의된 assay를 가리켜야 한다. 현재 구현은 명시적인 동일 단위(`identity`)만 허용한다.

원본 response, config, 모든 public 파일은 SHA-256으로 추적한다. public evidence에는 시험 정의와 원본 위치만 넣고 결과 행이나 후속 측정 범위, data audit 통계는 넣지 않는다. `PublicBundleAdapter`는 manifest의 상대 경로 탈출, 누락 파일, 해시 불일치, 공개 외 경로를 거절한다.

## 감사와 한계

`PublicCampaignAuditor`는 먼저 stage 0의 `validate_public_campaign`을 실행한다. RDKit가 설치돼 있으면 공개 후보 SMILES를 파싱한다. RDKit가 없으면 `rdkit_unavailable` 감사 이슈를 반환한다. 이 경우 화학 구조 검증은 수행되지 않았으므로 통과로 해석하지 않는다.

`examples/stage1_fixture/`과 `synthetic_*.json`은 네트워크 없는 테스트 fixture다. 실제 PubChem AID와 그 primary/follow-up 관계, endpoint와 단위, assay description은 별도 source review로 확인해야 한다. 이 저장소는 아직 특정 표적의 실제 후보나 생물학적 결론을 배포하지 않는다.

## 확인한 PubChem snapshot 예제

`examples/stage1_configs/pubchem_tor_mep2_snapshot.json`은 실데이터용 별도 설정이다. PubChem AID 504468의 설명은 MEP2 primary screen을 AID 2016으로, 그 cherry-pick confirmatory dose-response를 AID 2272로 명시한다. 2026-09-15에 PUG-REST concise CSV를 실제로 받아 확인한 결과 AID 2016의 326,763행 중 active 1,682행과 AID 2272의 1,756행 사이에는 active-primary SID 기준으로 295개의 공유 SID가 있었다. 설정은 이 전체 primary cache에서 stable ID 정렬과 snapshot date seed 20260915로 다섯 SID를 선택하며, 그 snapshot에서 AID 2272와 연결된 행은 하나다.

구조 property 요청은 이 다섯 CID를 snapshot에 맞춰 명시한다. 원본이 갱신되어 후보가 달라지면 구조 cache 누락으로 build가 중단된다. 이는 후속 결과나 구조를 보고 후보를 조용히 바꾸지 않기 위한 의도된 재검토 지점이다. 결과의 학술적·치료적 의미는 검증하지 않으며, `active`는 해당 PubChem assay outcome일 뿐이다.
