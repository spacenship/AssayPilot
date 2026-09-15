"""모든 도메인 계약의 기본 규칙과 값 타입."""
from decimal import Decimal
from copy import deepcopy
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _money(value: object) -> Decimal:
    """정확한 십진 금액만 허용하며 float/bool의 암묵 변환을 막는다."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("money requires Decimal, decimal string, or integer")
    try:
        amount = Decimal(value)
    except Exception as exc:
        raise ValueError("invalid decimal amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("money must be finite and nonnegative")
    return amount


ID = Annotated[str, Field(strict=True, min_length=1, pattern=r"\S")]
Text = ID
Money = Annotated[Decimal, BeforeValidator(_money)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Contract(BaseModel):
    """미선언 필드를 거절하며 변경은 validated_replace로 검증 후 교체한다."""
    model_config = ConfigDict(extra="forbid", validate_assignment=True, revalidate_instances="always")

    def validated_replace(self, **changes: Any) -> Self:
        """최상위 필드를 대체한 독립 새 객체를 검증해 반환한다.

        실패하면 ValidationError를 발생시키며 원본과 전달받은 변경 객체는
        변경하지 않는다. 중첩 필드는 병합하지 않고 통째로 대체한다.
        참조 정합성은 반환된 객체에 별도 validate_* 검사를 적용해야 한다.
        """
        payload = self.model_dump(round_trip=True)
        payload.update(changes)
        return type(self).model_validate(deepcopy(payload))


class Envelope(Contract):
    """독립 직렬화 묶음의 규격 버전."""
    schema_version: Literal["0.1.0"] = "0.1.0"


class Verdict(StrEnum):
    """실측 판정. 미측정은 이 열거형이 아닌 Observation 부재로 표현한다."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    INCONCLUSIVE = "inconclusive"
    UNSPECIFIED = "unspecified"


class Comparison(StrEnum):
    """측정값 및 성공 조건의 비교 연산자."""
    EQ = "="
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="


class Cost(Contract):
    """단위와 가정 여부를 가진 설정 비용 또는 예산 한도."""
    amount: Money
    unit: Text
    assumed: bool
