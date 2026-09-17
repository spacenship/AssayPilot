"""PubChem concise CSV 행을 보존적으로 정규화하는 순수 함수."""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping

from assaypilot.domain import Comparison, Verdict

from .schemas import AssayMapping, NormalizedMeasurement


class NormalizationError(ValueError):
    """설정으로 설명할 수 없는 원본 값이나 필수 식별자 누락 오류."""


_NUMBER = re.compile(r"^\s*(<=|>=|<|>|=)?\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*$")


def stable_id(prefix: str, *parts: object) -> str:
    """입력 문자열만 이용해 실행 시각과 무관한 안정 ID를 만든다."""
    digest = hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:20]
    return f"{prefix}-{digest}"


def parse_numeric(value: object) -> tuple[float, Comparison]:
    """실제 0과 부등호를 보존하며 모호한 문자열·bool·비유한 수를 거절한다."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise NormalizationError(f"ambiguous numeric value: {value!r}")
    match = _NUMBER.match(value)
    if not match:
        raise NormalizationError(f"ambiguous numeric value: {value!r}")
    number = float(match.group(2))
    if not math.isfinite(number):
        raise NormalizationError(f"non-finite numeric value: {value!r}")
    operator = match.group(1) or "="
    return number, Comparison(operator)


def _required_int(row: Mapping[str, str], key: str) -> int:
    value = row.get(key, "").strip()
    if not value or not value.isdecimal() or int(value) <= 0:
        raise NormalizationError(f"missing or invalid {key}: {value!r}")
    return int(value)


def normalize_concise_row(
    row: Mapping[str, str], mapping: AssayMapping, *, source_file_sha256: str,
    smiles_by_cid: Mapping[int, str], row_number: int,
) -> NormalizedMeasurement:
    """확인한 concise CSV 한 행을 개발자용 중간 측정으로 변환한다.

    SID를 관측 연결의 기준으로 유지하며 CID가 같아도 행을 병합하지 않는다.
    """
    if not any(value.strip() for value in row.values()):
        raise NormalizationError("fully blank result row")
    aid = _required_int(row, "AID")
    if aid != mapping.aid:
        raise NormalizationError(f"AID {aid} does not match configured AID {mapping.aid}")
    sid = _required_int(row, "SID")
    cid_text = row.get("CID", "").strip()
    if cid_text and (not cid_text.isdecimal() or int(cid_text) <= 0):
        raise NormalizationError(f"invalid CID: {cid_text!r}")
    cid = int(cid_text) if cid_text else None
    raw_verdict = row.get(mapping.raw_outcome_column, "").strip() or None
    if raw_verdict is None:
        verdict = Verdict.UNSPECIFIED
    else:
        try:
            verdict = mapping.verdict_mapping[raw_verdict]
        except KeyError as exc:
            raise NormalizationError(f"undefined raw verdict {raw_verdict!r}") from exc
    value = unit = comparison = None
    if mapping.raw_endpoint_column is not None:
        raw_value = row.get(mapping.raw_endpoint_column, "").strip()
        if raw_value:
            value, comparison = parse_numeric(raw_value)
            if mapping.conversion != "identity" or mapping.raw_unit != mapping.unit:
                raise NormalizationError("only explicit identity unit conversion is supported")
            unit = mapping.unit
    if raw_verdict is None and value is None:
        raise NormalizationError("row has neither mapped outcome nor numeric endpoint")
    source_row_id = stable_id("raw", mapping.aid, sid, row_number, source_file_sha256)
    return NormalizedMeasurement(
        measurement_id=stable_id("measurement", mapping.assay_id, source_row_id),
        assay_id=mapping.assay_id, aid=aid, sid=sid, cid=cid,
        original_smiles=smiles_by_cid.get(cid) if cid is not None else None,
        raw_verdict=raw_verdict, verdict=verdict, value=value, unit=unit,
        comparison=comparison, replicate_id=f"not_reported:{source_row_id}",
        condition_id=f"not_reported:{mapping.aid}", source_row_id=source_row_id,
        source_file_sha256=source_file_sha256, protocol_location=mapping.protocol_location,
        raw_row=dict(row),
    )
