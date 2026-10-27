import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from brbank import (
    AdapterDda,
    CapacidadeDda,
    DebitoDda,
    EventoDda,
    NaoSuportado,
    PaginaDebitos,
    ResumoDda,
    StatusDda,
    TipoEventoDda,
    normalizar_linha_digitavel,
)
from brbank.dda import codigo_barras_da_linha, hash_corpo
from brbank.simulador.dados import montar_linha_digitavel

LINHA = montar_linha_digitavel(
    banco="341", valor=Decimal("1240.00"), vencimento=date(2026, 11, 5), semente="t"
)


def _debito(**extra) -> DebitoDda:
    base = {
        "banco": "btg",
        "id_banco": "d-1",
        "cnpj_pagador": "11.222.333/0001-81",
        "linha_digitavel": LINHA,
        "beneficiario_documento": "45.000.001/0001-56",
        "beneficiario_nome": "ALFA EMBALAGENS LTDA",
        "valor": "1240.00",
    }
    return DebitoDda(**{**base, **extra})


def test_linha_gerada_confere_os_dvs():
    assert len(LINHA) == 47
    assert normalizar_linha_digitavel(LINHA) == LINHA


def test_linha_aceita_pontuacao_e_espaco():
    formatada = f"{LINHA[:5]}.{LINHA[5:10]} {LINHA[10:15]}.{LINHA[15:21]} {LINHA[21:]}"
    assert normalizar_linha_digitavel(formatada) == LINHA


@pytest.mark.parametrize("posicao", [9, 20, 31, 32])
def test_linha_com_dv_errado_e_recusada(posicao):
    digito = str((int(LINHA[posicao]) + 1) % 10)
    errada = LINHA[:posicao] + digito + LINHA[posicao + 1 :]
    with pytest.raises(ValueError, match="verificador"):
        normalizar_linha_digitavel(errada)
    assert normalizar_linha_digitavel(errada, validar_dv=False) == errada


@pytest.mark.parametrize("texto", ["", "123", "a" * 47, "1" * 46, "1" * 49])
def test_linha_com_tamanho_ou_caractere_invalido(texto):
    with pytest.raises(ValueError):
        normalizar_linha_digitavel(texto)


def test_linha_de_arrecadacao_confere_so_o_tamanho():
    linha = "8" + "1" * 47
    assert normalizar_linha_digitavel(linha) == linha
    barras = codigo_barras_da_linha(linha)
    assert barras is not None and len(barras) == 44


def test_codigo_de_barras_da_linha_47():
    barras = codigo_barras_da_linha(LINHA)
    assert barras is not None and len(barras) == 44
    assert barras[:3] == "341"
    # banco, moeda, DV geral e fator (4) vem antes do valor (10 digitos)
    assert barras[9:19] == "0000124000"
    assert codigo_barras_da_linha("1" * 47) is None


def test_debito_normaliza_e_nao_vaza_pii_no_repr():
    debito = _debito(
        valor=Decimal("10.005"),
        resposta_bruta={"nome": "Maria", "status": "CREATED"},
    )
    assert debito.valor == Decimal("10.01")
    assert debito.cnpj_pagador == "11222333000181"
    assert debito.beneficiario_documento == "45000001000156"
    assert debito.status is StatusDda.ABERTO
    assert debito.resposta_bruta == {"nome": "***", "status": "CREATED"}
    texto = repr(debito)
    for pii in ("ALFA", "45000001", "11222333", LINHA, "Maria"):
        assert pii not in texto


def test_debito_aceita_cnpj_alfanumerico_do_pagador():
    assert _debito(cnpj_pagador="12.abc.345/01de-35").cnpj_pagador == "12ABC34501DE35"


@pytest.mark.parametrize(
    "campo",
    [
        {"valor": 10.5},
        {"valor": "0"},
        {"valor": "-1"},
        {"cnpj_pagador": "123"},
        {"linha_digitavel": "123"},
        {"beneficiario_documento": " "},
        {"status": "QUALQUER"},
        {"extra": 1},
    ],
)
def test_debito_recusa_dado_invalido(campo):
    with pytest.raises(ValidationError):
        _debito(**campo)


def test_debito_e_imutavel():
    with pytest.raises(ValidationError):
        _debito().valor = Decimal(1)  # type: ignore[misc]


def test_pagina_e_resumo():
    pagina = PaginaDebitos(itens=(_debito(),), cursor_proximo="2")
    assert pagina.itens[0].id_banco == "d-1"
    resumo = ResumoDda(
        quantidade=2,
        valor_total="10.50",
        por_status={StatusDda.ABERTO: 2},
    )
    assert resumo.valor_total == Decimal("10.50")
    with pytest.raises(ValidationError):
        ResumoDda(quantidade=-1, valor_total="0")


def test_evento_chave_de_dedup_e_mascara():
    evento = EventoDda(
        banco="btg",
        id_evento="e-1",
        tipo=TipoEventoDda.DEBITO_NOVO,
        resposta_bruta={"payer": "X", "ok": 1},
    )
    assert evento.chave_dedup == ("btg", "e-1")
    assert evento.resposta_bruta == {"payer": "***", "ok": 1}
    with pytest.raises(ValidationError):
        EventoDda(banco="btg", id_evento="", tipo=TipoEventoDda.DDA_ATIVADO)


def test_hash_corpo_e_estavel_e_sensivel_ao_conteudo():
    a = {"event": "x", "data": {"b": 1, "a": Decimal("1.50")}}
    b = json.loads(json.dumps({"data": {"a": "1.50", "b": 1}, "event": "x"}))
    assert hash_corpo(a) == hash_corpo(b)
    assert hash_corpo(a) != hash_corpo({**a, "event": "y"})
    assert len(hash_corpo(a)) == 32


async def test_adapter_dda_padrao_nao_suporta_nada():
    class SoNome(AdapterDda):
        banco = "x"

    adapter = SoNome()
    assert not adapter.suporta_dda(CapacidadeDda.CONSULTA)
    chamadas = [
        adapter.ativar_dda("1"),
        adapter.desativar_dda("1"),
        adapter.listar_debitos("1"),
        adapter.resumo_debitos("1"),
        adapter.alterar_debito("1", "d", oculto=True),
    ]
    for corrotina in chamadas:
        with pytest.raises(NaoSuportado):
            await corrotina
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook_dda(b"{}")


def test_capacidades_combinam():
    todas = (
        CapacidadeDda.ATIVACAO
        | CapacidadeDda.CONSULTA
        | CapacidadeDda.RESUMO
        | CapacidadeDda.ALTERACAO
        | CapacidadeDda.WEBHOOK
    )
    assert not CapacidadeDda.NENHUMA
    assert CapacidadeDda.WEBHOOK in todas
