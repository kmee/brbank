"""Contrato de pagamento: Pix copia e cola, lote local e registro por entry point."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from brbank import (
    AdapterNaoEncontrado,
    CapacidadePagamento,
    ContaBancaria,
    LotePagamento,
    NaoSuportado,
    Pagamento,
    PagamentoRegistrado,
    StatusLote,
    StatusPagamento,
    TipoPagamento,
    listar_adapters_pagamento,
    obter_classe_pagamento,
    status_lote_local,
)
from brbank.adapters.bb_pagamento import BancoDoBrasilPagamentoAdapter
from tests.bb_pagamento_falso import BBPagamentoFalso
from tests.sicoob_pagamento_falso import brcode
from tests.test_bb_pagamento import config as config_bb

DIA = date(2026, 10, 29)
URL_QR = "qrpix.example.com.br/qr/v2/9d36b84fc70b478fb95c12729b90ca25"


def qr(**extra) -> Pagamento:
    campos = {
        "chave_idempotencia": "q1",
        "tipo": TipoPagamento.PIX_QRCODE,
        "valor": Decimal("10"),
        "data": DIA,
        "brcode": brcode("10.00"),
    }
    return Pagamento(**{**campos, **extra})


def test_qrcode_valido_normaliza_e_esconde_o_brcode():
    pagamento = qr(brcode="  " + brcode("10.00") + "\n")
    assert pagamento.brcode == brcode("10.00")
    assert pagamento.valor == Decimal("10.00")
    assert pagamento.pagar_com_juros_multa is False
    assert "RECEBEDOR" not in repr(pagamento)


@pytest.mark.parametrize(
    ("kw", "erro"),
    [
        ({"brcode": None}, "exige"),
        ({"brcode": brcode("10.00")[:-4] + "0000"}, "CRC"),
        ({"brcode": "texto qualquer"}, "CRC"),
        ({"valor": Decimal("11")}, "valor fixo"),
    ],
)
def test_qrcode_invalido(kw, erro):
    with pytest.raises(ValidationError, match=erro):
        qr(**kw)


def test_qrcode_sem_valor_ou_dinamico_aceita_qualquer_valor():
    assert qr(brcode=brcode(), valor=Decimal("7.5")).valor == Decimal("7.50")
    dinamico = qr(brcode=brcode("10.00", url=URL_QR), valor=Decimal("12.34"))
    assert dinamico.valor == Decimal("12.34")


def test_brcode_so_no_tipo_qrcode():
    with pytest.raises(ValidationError, match="brcode"):
        Pagamento(
            chave_idempotencia="g1",
            tipo=TipoPagamento.GUIA,
            valor=Decimal("1"),
            data=DIA,
            codigo_barras="83630000000641400052836100812355200812351310",
            brcode=brcode(),
        )


def test_capacidades_novas_nao_mudam_as_antigas():
    # Flag acrescentada no fim: o valor das capacidades antigas fica igual.
    assert CapacidadePagamento.BOLETO.value == 1
    assert CapacidadePagamento.WEBHOOK.value == 128
    assert CapacidadePagamento.PIX_QRCODE.value == 256
    assert CapacidadePagamento.LOTE_LOCAL.value == 512
    assert CapacidadePagamento[TipoPagamento.PIX_QRCODE.value]


async def test_bb_recusa_qrcode(dormir):
    adapter = BancoDoBrasilPagamentoAdapter(
        config_bb(), client=BBPagamentoFalso().cliente(), dormir=dormir
    )
    lote = LotePagamento(
        chave_idempotencia="l1",
        tipo=TipoPagamento.PIX_QRCODE,
        conta_debito=ContaBancaria(agencia="1607", conta="99738672", digito="x"),
        pagamentos=(qr(),),
    )
    assert not adapter.suporta_pagamento(CapacidadePagamento.PIX_QRCODE)
    with pytest.raises(NaoSuportado, match="PIX_QRCODE"):
        await adapter.pagar_lote(lote)


def test_registro_de_adapters_de_pagamento():
    assert {"bb", "sicoob"} <= set(listar_adapters_pagamento())
    assert obter_classe_pagamento("bb") is BancoDoBrasilPagamentoAdapter
    suportam_qr = [
        nome
        for nome in listar_adapters_pagamento()
        if CapacidadePagamento.PIX_QRCODE
        in obter_classe_pagamento(nome).capacidades_pagamento
    ]
    assert "sicoob" in suportam_qr and "bb" not in suportam_qr
    with pytest.raises(AdapterNaoEncontrado, match="pagamento"):
        obter_classe_pagamento("nenhum")


def _linha(status: StatusPagamento) -> PagamentoRegistrado:
    return PagamentoRegistrado(banco="x", id_banco="1", status=status)


@pytest.mark.parametrize(
    ("estados", "lote"),
    [
        ((StatusPagamento.RECUSADO,) * 2, StatusLote.RECUSADO),
        ((StatusPagamento.RECUSADO, StatusPagamento.PAGO), StatusLote.ACEITO_PARCIAL),
        ((StatusPagamento.PAGO, StatusPagamento.REJEITADO), StatusLote.PROCESSADO),
        (
            (StatusPagamento.PAGO, StatusPagamento.INDETERMINADO),
            StatusLote.EM_PROCESSAMENTO,
        ),
        ((StatusPagamento.ACEITO,), StatusLote.EM_PROCESSAMENTO),
    ],
)
def test_status_lote_local(estados, lote):
    assert status_lote_local(tuple(_linha(s) for s in estados)) is lote


def test_status_lote_local_vazio():
    with pytest.raises(ValueError):
        status_lote_local(())
