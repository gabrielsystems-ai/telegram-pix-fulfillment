import json
from datetime import datetime
from decimal import Decimal, DecimalException

from vip.errors import GatewayError

FINANCIAL_STATES = frozenset(
    {"PENDING", "COMPLETED", "FAILED", "REFUNDED", "CHARGED_BACK", "EXPIRED"}
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("invalid_json_number")


def decode_object(content: bytes) -> dict:
    try:
        if len(content) > 1_000_000:
            raise ValueError("response_too_large")
        data = json.loads(
            content,
            parse_float=Decimal,
            parse_constant=_invalid_constant,
            object_pairs_hook=_unique_object,
        )
        if not isinstance(data, dict):
            raise ValueError("unexpected_response_shape")
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise GatewayError("omega_unexpected_response") from None


def text(value, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise GatewayError("omega_unexpected_response")
    return value


def money(value) -> Decimal:
    try:
        if type(value) not in (int, Decimal):
            raise ValueError("not_a_json_number")
        result = Decimal(value)
        if (
            not result.is_finite()
            or result < 0
            or result * 100 != (result * 100).to_integral_value()
        ):
            raise ValueError("invalid_currency_amount")
        return result
    except (ValueError, DecimalException):
        raise GatewayError("omega_invalid_amount") from None


def timestamp(value) -> int | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError("invalid_timestamp")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("ambiguous_timezone")
        return int(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        raise GatewayError("omega_unexpected_response") from None
