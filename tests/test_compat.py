from datetime import datetime, timedelta, timezone

import pytest

from brbank._compat import StrEnum, datetime_iso


class Cor(StrEnum):
    AZUL = "AZUL"


def test_strenum_e_texto():
    assert Cor.AZUL == "AZUL"
    assert str(Cor.AZUL) == "AZUL"
    assert f"{Cor.AZUL}" == "AZUL"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        (
            "2026-10-19T10:15:00Z",
            datetime(2026, 10, 19, 10, 15, tzinfo=timezone.utc),
        ),
        (
            "2026-10-19T10:15:00.35Z",
            datetime(2026, 10, 19, 10, 15, 0, 350000, tzinfo=timezone.utc),
        ),
        (
            "2026-10-19T10:15:00.123456-03:00",
            datetime(
                2026, 10, 19, 10, 15, 0, 123456, tzinfo=timezone(timedelta(hours=-3))
            ),
        ),
        ("2026-10-19T10:15:00", datetime(2026, 10, 19, 10, 15)),
    ],
)
def test_datetime_iso(texto, esperado):
    assert datetime_iso(texto) == esperado


def test_datetime_iso_invalido():
    with pytest.raises(ValueError):
        datetime_iso("ontem")
