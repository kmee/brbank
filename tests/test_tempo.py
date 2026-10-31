"""A data de hoje dos adapters e a de Brasilia, nao a do servidor."""

import inspect
from datetime import date, datetime, timezone

import pytest

from brbank import obter_classe, obter_classe_pagamento
from brbank.tempo import hoje_brasil


def test_de_noite_em_brasilia_o_utc_ja_virou_o_dia():
    agora = datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc)
    assert hoje_brasil(agora) == date(2026, 10, 30)
    assert hoje_brasil(datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)) == date(
        2026, 10, 31
    )


@pytest.mark.parametrize(
    "classe",
    [
        *(obter_classe_pagamento(b) for b in ("sicoob", "inter", "itau", "santander")),
        *(
            obter_classe(b)
            for b in (
                "bb",
                "bradesco",
                "banrisul",
                "caixa",
                "itau",
                "santander",
                "sicoob",
            )
        ),
    ],
)
def test_adapters_usam_a_data_de_brasilia(classe):
    assert inspect.signature(classe.__init__).parameters["hoje"].default is hoje_brasil
