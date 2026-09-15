"""모든 도메인 계약의 기본 규칙과 값 타입."""
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

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
    """미선언 필드를 거절하고 할당 시에도 객체 제약을 검사하는 기본 모델."""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


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
