"""Metadados que as telas leem sem instanciar o adapter (nome e nosso numero)."""

import dataclasses
import typing

import pytest

from brbank.adapters.pix_bacen import PERFIS
from brbank.registro import listar_adapters, obter_classe


def _classe_config(classe):
    return typing.get_type_hints(classe.__init__)["config"]


@pytest.mark.parametrize("nome", listar_adapters())
def test_adapter_tem_nome_de_exibicao(nome):
    classe = obter_classe(nome)
    assert classe.nome_exibicao.strip()
    assert classe.nome_exibicao != classe.banco


@pytest.mark.parametrize("nome", listar_adapters())
def test_digitos_so_onde_a_config_aceita_gerador(nome):
    """Quem declara os digitos aceita `gerar_nosso_numero`, e vice-versa."""
    classe = obter_classe(nome)
    campos = {c.name for c in dataclasses.fields(_classe_config(classe))}
    aceita_gerador = "gerar_nosso_numero" in campos
    assert (classe.digitos_nosso_numero is not None) == aceita_gerador
    if aceita_gerador:
        assert 1 <= classe.digitos_nosso_numero <= 17


@pytest.mark.parametrize("nome", sorted(PERFIS))
def test_perfil_pix_tem_nome_de_exibicao(nome):
    assert PERFIS[nome].nome_exibicao.strip()
