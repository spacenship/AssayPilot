# AssayPilot — 1단계 데이터 준비와 공개 캠페인

여러 HTS 캠페인의 공개 데이터와 후속 모듈 사이의 **데이터 타입, Protocol, 정합성 검사**를 정의하고, PubChem PUG-REST 원본 캐시에서 정규화한 초기 공개 캠페인을 만듭니다. 일차 스크리닝 hit는 직접 결합이나 치료 효능의 증거가 아닙니다.

## 설치와 실행

Python 3.11 이상과 Pydantic 2.x를 사용합니다. 테스트 추가 의존성은 pytest입니다. 이 작업에서는 기존 conda `drug` 환경(Python 3.12)을 사용했습니다.

```bash
cd AssayPilot
conda run -n drug python -m pip install --no-user --no-cache-dir -e '.[test]'
conda run -n drug python examples/stage0_contract_demo.py
conda run -n drug python examples/stage1_data_demo.py
conda run -n drug python -m pytest -q
```

예제와 테스트는 설치 후 네트워크·외부 API·GPU 없이 실행됩니다. 데모는 스크립트 위치를 기준으로 JSON을 읽으므로 다른 작업 디렉터리에서도 실행할 수 있습니다.

## 읽는 순서

1. `examples/synthetic_campaign.json`: 후보 3개, 시험 3개, 공개 실측 5개 및 미측정 조합.
2. `src/assaypilot/domain/common.py`, `catalog.py`: 기본 값과 후보·시험·캠페인·근거.
3. `records.py`, `exchange.py`, `containers.py`: 관측·제안·예측·예산·실행 교환과 공개 경계.
4. `validation.py`: 입력을 변경하지 않는 참조·순환 검사.
5. `protocols.py`: 다음 단계 구현이 연결되는 계약.
6. `examples/stage0_contract_demo.py`, `tests/`: 객체 생성, JSON 왕복, 정상·오류 사례.
7. `src/assaypilot/data/`, `examples/stage1_*`: 캐시 수집, 엄격 정규화, 공개/개발자 bundle 분리와 오프라인 fixture.

상세 규칙과 타입별 입출력은 [0단계 설계 문서](docs/stage0_contracts.md)와 [1단계 데이터 문서](docs/stage1_data.md)에 있습니다.

## 사용 예

```python
from pathlib import Path
from assaypilot.domain import PublicCampaign, validate_public_campaign

campaign = PublicCampaign.model_validate_json(
    Path("examples/synthetic_campaign.json").read_text()
)
result = validate_public_campaign(campaign)
for issue in result.issues:
    print(issue.target_id, issue.field, issue.code, issue.reason)
assert result.ok
```

모델 생성은 단일 객체 제약을 검사합니다. **다른 객체의 존재 여부·순환·단위 일치 등은 해당 `validate_*` 함수를 별도로 호출해야 합니다.** 검사 결과가 `ok=False`이면 후속 모듈에 넘기지 않는 것이 호출자의 책임입니다.

## 결과 수신 시점과 객체 교체

초기 로딩 검사 `validate_public_campaign`과 `validate_observation_batch`는
`PublicCampaign.as_of`를 사용합니다. 이후 결과 수신 검사는 현재 실행 시점을
필수 키워드 인자로 전달합니다. 초기 스냅샷 이후 관측도 현재 시점 이하면
허용하고, 현재보다 미래인 관측은 계속 거절합니다.

```python
audit = validate_execution(action, receipt, result, campaign, as_of=state.as_of)
```

`state.as_of`에는 호출자가 현재 실행 시점을 담아야 합니다. 타임존 없는 시각은
거절하며, `as_of`를 생략하면 오류입니다.

변경은 직접 필드 대입 대신 `validated_replace`로 새 객체를 검증한 뒤 교체합니다.
이 메서드는 원본과 입력값을 변경하지 않으며 중첩 객체도 독립적으로 복사합니다.

```python
proposed = state.validated_replace(status="paused")  # 실패하면 ValidationError
audit = validate_run_state(proposed, campaign)
if audit.ok:
    state = proposed  # 모델 검사와 참조 검사가 모두 성공했을 때만 교체
```

여러 필드는 한 호출에서 함께 대체하며, 중첩 필드는 부분 병합하지 않고 통째로
대체합니다. `validate_assignment`는 실패 시 원상 복구를 보장하지 않으므로 직접
대입이나 list/dict 내부 변경에 의존하지 않습니다. 이 규약은 메모리 객체의 교체이며
실제 예산 차감이나 DB 트랜잭션을 구현하지 않습니다.

## 범위와 공개 데이터 경계

- 모든 예제 근거·측정값·예측값·승인 메타데이터는 **SYNTHETIC**입니다. 다른 표적 예제는 스키마 재사용만 확인하며 모델 일반화 성능을 검증하지 않습니다.
- `active`, `inactive`, `inconclusive`, `unspecified`는 실측의 판정입니다. 미측정은 관측 부재이며 임의의 inactive 또는 예측값으로 채우지 않습니다.
- 공개 모델은 봉인 라벨·정답 경로·Oracle 객체 필드를 허용하지 않습니다. **스키마 분리가 파일·도구 접근 격리를 보장하지 않습니다. 격리는 2단계 구현 대상입니다.**
- `assaypilot.data`는 명시적 CLI에서만 PubChem PUG-REST를 호출하고, SHA-256 메타데이터가 맞는 원본 캐시를 기본 재사용합니다. `build`는 네트워크에 연결하지 않습니다.
- `PublicBundleAdapter`는 `public/manifest.json`의 허용 파일과 해시만 확인해 `CampaignAdapter.load(source: DataSource) -> PublicCampaign`을 구현합니다. `PublicCampaignAuditor`는 도메인 참조 검사와, 설치된 경우 RDKit SMILES 검사를 수행합니다.
- 이번 단계는 특징 계산, 모델 학습, LLM, 예산 예약·차감, Oracle/DB, 실행 루프, UI를 구현하지 않습니다. RDKit은 선택 의존성(`.[chem]`)이며 없으면 화학 구조 검증을 통과했다고 주장하지 않고 감사 결과에 표시합니다.

## 1단계 오프라인 실행

fixture는 실제 생물학적 근거나 PubChem 검증 데이터가 아닌 경계 검사용 합성 데이터입니다. 따라서 네트워크 없이 결과를 재현할 수 있습니다.

```bash
conda run -n drug python -m pip install --no-user --no-cache-dir -e '.[test]'
conda run -n drug python -m assaypilot.data build \
  examples/stage1_configs/synthetic_linked.json \
  examples/stage1_fixture /tmp/assaypilot-stage1-bundle
conda run -n drug python -m assaypilot.data inspect /tmp/assaypilot-stage1-bundle/public/manifest.json
```

RDKit이 없으면 `inspect`는 `rdkit_unavailable`을 출력하고 종료 코드 2를 반환한다. 이는 공개 참조 검사가 실패했다는 뜻이 아니라 화학 구조 검증이 수행되지 않아 전체 감사 통과로 표시할 수 없다는 뜻이다. 구조 감사까지 하려면 `.[chem]`을 설치한다.

실제 수집은 `fetch`를 명시적으로 실행하고 선택 의존성 `httpx`를 설치한 환경에서만 수행합니다. AID·endpoint·판정 매핑·단위·후속 시험 링크는 설정 파일에서 검토한 뒤 입력해야 합니다.

```bash
conda run -n drug python -m pip install --no-user --no-cache-dir -e '.[data]'
conda run -n drug python -m assaypilot.data fetch path/to/campaign.json path/to/raw-cache
conda run -n drug python -m assaypilot.data build path/to/campaign.json path/to/raw-cache path/to/output-bundle
```

검토를 마친 실데이터 재현 설정은 `examples/stage1_configs/pubchem_tor_mep2_snapshot.json`이다. 이 설정은 PubChem의 AID 504468 설명이 밝힌 MEP2 primary AID 2016과 confirmatory cherry-pick AID 2272 관계를 사용한다. cache snapshot에서 snapshot 날짜를 seed로 삼아 primary active SID 다섯 개를 뽑으며, 그중 한 SID에만 AID 2272 측정이 있다. 나머지 네 후보의 follow-up 부재는 음성으로 바꾸지 않는다. 전체 32만여 primary 행을 읽으므로 다운로드와 build에는 시간과 디스크 공간이 필요하다.

```bash
conda run -n drug python -m assaypilot.data fetch \
  examples/stage1_configs/pubchem_tor_mep2_snapshot.json /tmp/assaypilot-pubchem-cache
conda run -n drug python -m assaypilot.data build \
  examples/stage1_configs/pubchem_tor_mep2_snapshot.json /tmp/assaypilot-pubchem-cache /tmp/assaypilot-pubchem-bundle
```
