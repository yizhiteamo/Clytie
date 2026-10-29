"""清单和 SDK 使用同一固定时长语法；换算不读取系统时钟。"""

import re
from decimal import Decimal

NUMBER = r"[0-9]+(?:[.,][0-9]+)?"
DURATION_PATTERN = (
    rf"^(?=.*[1-9])P(?=[0-9]|T[0-9])(?:{NUMBER}W|(?:{NUMBER}D)?"
    rf"(?:T(?=[0-9])(?:{NUMBER}H)?(?:{NUMBER}M)?(?:{NUMBER}S)?)?)(?![\s\S])"
)


def duration_seconds(value: str) -> Decimal:
    if not re.fullmatch(DURATION_PATTERN, value):
        raise ValueError("every must be a positive fixed ISO 8601 duration (W/D/H/M/S)")
    factors = {"W": 7 * 24 * 60 * 60, "D": 24 * 60 * 60, "H": 3600, "M": 60, "S": 1}
    return sum(
        (
            Decimal(number.replace(",", ".")) * factors[unit]
            for number, unit in re.findall(rf"({NUMBER})([WDHMS])", value)
        ),
        Decimal(0),
    )
