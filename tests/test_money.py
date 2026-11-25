import pytest

from polybit.errors import AmountError
from polybit.money import MAX_AMOUNT, UNIT, format_amount, parse_amount


@pytest.mark.parametrize(
    "text, units",
    [
        ("1", UNIT),
        ("0.5", UNIT // 2),
        ("1,000", 1000 * UNIT),
        ("25 PB", 25 * UNIT),
        ("25pb", 25 * UNIT),
        ("0.000001", 1),
        ("1.500000", 1_500_000),
        ("1e3", 1000 * UNIT),
    ],
)
def test_parse_valid(text, units):
    assert parse_amount(text) == units


@pytest.mark.parametrize(
    "text",
    ["", "abc", "0", "-5", "0.0000001", "nan", "inf", "1e40", "1,000,000,001"],
)
def test_parse_invalid(text):
    with pytest.raises(AmountError):
        parse_amount(text)


def test_parse_max_boundary():
    assert parse_amount("1000000000") == MAX_AMOUNT


@pytest.mark.parametrize(
    "units, text",
    [
        (0, "0 PB"),
        (UNIT, "1 PB"),
        (1_500_000, "1.5 PB"),
        (1, "0.000001 PB"),
        (1234 * UNIT, "1,234 PB"),
        (-2 * UNIT, "-2 PB"),
    ],
)
def test_format(units, text):
    assert format_amount(units) == text


def test_roundtrip():
    for units in [1, 7, 999_999, 1_000_001, 123_456_789_012]:
        assert parse_amount(format_amount(units)) == units
