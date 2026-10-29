from datetime import date, timedelta
from decimal import Decimal

import pytest

from brbank import (
    AdapterPagamento,
    CapacidadePagamento,
    ContaBancaria,
    DestinoPix,
    LotePagamento,
    NaoSuportado,
    Pagamento,
    StatusLote,
    StatusPagamento,
    TipoChavePix,
    TipoPagamento,
    Validacao,
    obter_classe_pagamento,
    vencimento_do_boleto,
)
from brbank.adapters.inter_pagamento import (
    InterPagamentoAdapter,
    InterPagamentoConfig,
)
from brbank.instrumento import _dv_mod11_barras
from brbank.mascara import MASCARA
from tests.inter_pagamento_falso import InterPagamentoFalso
from tests.sicoob_pagamento_falso import brcode

HOJE = date(2026, 10, 29)
DEBITO = ContaBancaria(agencia="1", conta="12345", digito="6")
BARRAS_BOLETO = "00195313800000015000000003051284190526000117"
BARRAS_GUIA = "83630000000641400052836100812355200812351310"
URL_QR = "qrpix.example.com.br/qr/v2/9d36b84fc70b478fb95c12729b90ca25"


def config(**extra) -> InterPagamentoConfig:
    base = {
        "conta": "c1",
        "client_id": "id",
        "client_secret": "segredo-xyz",
        "ambiente": "sandbox",
        "conta_corrente": "1234567",
    }
    base.update(extra)
    return InterPagamentoConfig(**base)


@pytest.fixture
def falso():
    return InterPagamentoFalso()


@pytest.fixture
def adapter(falso, dormir):
    return InterPagamentoAdapter(
        config(), client=falso.cliente(), dormir=dormir, hoje=lambda: HOJE
    )


def pagamento(tipo, chave="x1", valor="10.00", **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": tipo,
        "valor": Decimal(valor),
        "data": HOJE,
    }
    return Pagamento(**{**campos, **extra})


def qr(chave="q1", **extra):
    return pagamento(
        TipoPagamento.PIX_QRCODE,
        chave,
        brcode=extra.pop("brcode", brcode("10.00")),
        **extra,
    )


def boleto(chave="b1", **extra):
    campos = {
        "codigo_barras": BARRAS_BOLETO,
        "beneficiario_documento": "95752207000129",
    }
    return pagamento(TipoPagamento.BOLETO, chave, "15.00", **{**campos, **extra})


def lote(*pagamentos):
    return LotePagamento(
        chave_idempotencia="l1",
        tipo=pagamentos[0].tipo,
        conta_debito=DEBITO,
        pagamentos=pagamentos,
    )


def test_contrato_e_registro():
    assert issubclass(InterPagamentoAdapter, AdapterPagamento)
    assert obter_classe_pagamento("inter") is InterPagamentoAdapter
    caps = InterPagamentoAdapter.capacidades_pagamento
    for cap in ("PIX", "PIX_QRCODE", "BOLETO", "GUIA", "LOTE_LOCAL", "CANCELAMENTO"):
        assert CapacidadePagamento[cap] in caps
    assert CapacidadePagamento.LIBERACAO not in caps
    assert CapacidadePagamento.TRANSFERENCIA not in caps
    assert InterPagamentoAdapter.validado_sandbox is False
    assert "segredo-xyz" not in repr(config())


def test_config_e_mtls():
    with pytest.raises(Validacao, match="mTLS"):
        InterPagamentoAdapter(config())
    with pytest.raises(Validacao, match="conta_corrente"):
        config(conta_corrente="0123")
    with pytest.raises(Validacao, match="ambiente"):
        config(ambiente="uat")


async def test_pix_copia_e_cola(adapter, falso):
    registrado = await adapter.pagar_lote(lote(qr(descricao="Fornecedor")))
    corpo = falso.de_rota("pix")[0]
    assert corpo == {
        "valor": 10.0,
        "dataPagamento": "2026-10-29",
        "destinatario": {"tipo": "PIX_COPIA_E_COLA", "pixCopiaECola": brcode("10.00")},
        "descricao": "Fornecedor",
    }
    cabecalho = falso.cabecalhos[0]
    assert cabecalho["x-conta-corrente"] == "1234567"
    assert cabecalho["x-id-idempotente"] == adapter.chave_idempotente(qr())
    assert falso.escopos_pedidos == ["pagamento-pix.write"]
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.EM_PROCESSAMENTO
    assert linha.id_banco in falso.pix
    assert registrado.status is StatusLote.EM_PROCESSAMENTO


async def test_chave_idempotente_estavel(adapter, falso):
    assert adapter.chave_idempotente(qr("a")) == adapter.chave_idempotente(qr("a"))
    assert adapter.chave_idempotente(qr("a")) != adapter.chave_idempotente(qr("b"))
    um = await adapter.pagar_lote(lote(qr("a")))
    dois = await adapter.pagar_lote(lote(qr("a")))
    assert um.pagamentos[0].id_banco == dois.pagamentos[0].id_banco


@pytest.mark.parametrize(
    ("retorno", "status"),
    [
        ("APROVACAO", StatusPagamento.ACEITO),
        ("AGENDADO", StatusPagamento.AGENDADO),
    ],
)
async def test_tipo_retorno(adapter, falso, retorno, status):
    falso.tipo_retorno = retorno
    registrado = await adapter.pagar_lote(lote(qr()))
    assert registrado.pagamentos[0].status is status


async def test_pix_por_chave_e_por_dados_bancarios(adapter, falso):
    conta = ContaBancaria(
        agencia="1", conta="7654321", digito="0", ispb="00416968", poupanca=True
    )
    registrado = await adapter.pagar_lote(
        lote(
            pagamento(
                TipoPagamento.PIX,
                "p1",
                pix=DestinoPix(tipo_chave=TipoChavePix.TELEFONE, chave="61999999999"),
            ),
            pagamento(
                TipoPagamento.PIX,
                "p2",
                pix=DestinoPix(conta=conta, documento="28133847044", nome="Fulano"),
            ),
            pagamento(
                TipoPagamento.PIX,
                "p3",
                pix=DestinoPix(conta=conta, documento="28133847044"),
            ),
        )
    )
    chave, dados = falso.de_rota("pix")
    assert chave["destinatario"] == {"tipo": "CHAVE", "chave": "+5561999999999"}
    assert dados["destinatario"] == {
        "tipo": "DADOS_BANCARIOS",
        "contaCorrente": "76543210",
        "tipoConta": "CONTA_POUPANCA",
        "cpfCnpj": "28133847044",
        "agencia": "1",
        "nome": "Fulano",
        "instituicaoFinanceira": {"ispb": "00416968"},
    }
    sem_nome = registrado.pagamentos[2]
    assert sem_nome.status is StatusPagamento.RECUSADO
    assert "nome" in sem_nome.motivos[0]
    assert registrado.status is StatusLote.ACEITO_PARCIAL


async def test_recusa_do_banco_e_data_passada(adapter, falso):
    falso.falhas["pix"] = [422]
    registrado = await adapter.pagar_lote(
        lote(qr("q1"), qr("q2", data=HOJE - timedelta(days=1)), qr("q3"))
    )
    expirado, passado, pago = registrado.pagamentos
    assert expirado.status is StatusPagamento.RECUSADO
    assert expirado.motivos == (
        "Nao foi possivel processar o QR Code",
        "QR Code expirado.",
    )
    assert passado.status is StatusPagamento.RECUSADO
    assert "passado" in passado.motivos[0]
    assert pago.status is StatusPagamento.EM_PROCESSAMENTO


@pytest.mark.parametrize("falha", ["timeout", 500])
async def test_sem_resposta_e_indeterminado_sem_retry(adapter, falso, falha):
    falso.falhas["pix"] = [falha]
    registrado = await adapter.pagar_lote(lote(qr("q1"), qr("q2")))
    assert falso.chamadas.count(("POST", "/banking/v2/pix")) == 1
    incerto, seguinte = registrado.pagamentos
    assert incerto.status is StatusPagamento.INDETERMINADO
    assert seguinte.motivos == ("NAO_ENVIADO",)


@pytest.mark.parametrize(
    ("status", "esperado", "motivos"),
    [
        ("PAGO", StatusPagamento.PAGO, ()),
        ("AGUARDANDO_APROVACAO", StatusPagamento.ACEITO, ()),
        ("REPROVADO", StatusPagamento.CANCELADO, ()),
        ("FALHA", StatusPagamento.REJEITADO, ("AM04",)),
        ("PIX_PAGO", StatusPagamento.PAGO, ()),
    ],
)
async def test_consulta_pix(adapter, falso, status, esperado, motivos):
    registrado = await adapter.pagar_lote(lote(qr()))
    falso.status_pix = status
    codigo = registrado.pagamentos[0].id_banco
    consultado = await adapter.consultar_pagamento(codigo, TipoPagamento.PIX_QRCODE)
    assert consultado.id_banco == codigo
    assert consultado.status is esperado
    assert consultado.motivos == motivos
    assert consultado.autenticacao == "E00416968202610291000abcdefghijk"
    assert consultado.valor == Decimal("10.00")
    assert consultado.resposta_bruta["transacaoPix"]["recebedor"]["nome"] == MASCARA


async def test_consulta_pix_status_desconhecido(adapter, falso):
    registrado = await adapter.pagar_lote(lote(qr()))
    falso.status_pix = "PARCIALMENTE_PAGO"
    with pytest.raises(Validacao, match="nao mapeado"):
        await adapter.consultar_pagamento(
            registrado.pagamentos[0].id_banco, TipoPagamento.PIX_QRCODE
        )


async def test_boleto_com_vencimento_do_codigo(adapter, falso):
    registrado = await adapter.pagar_lote(lote(boleto()))
    corpo = falso.de_rota("pagamento")[0]
    vencimento = vencimento_do_boleto(BARRAS_BOLETO, hoje=HOJE)
    assert corpo == {
        "codBarraLinhaDigitavel": BARRAS_BOLETO,
        "valorPagar": "15.00",
        "dataPagamento": "2026-10-29",
        "dataVencimento": vencimento.isoformat(),
        "cpfCnpjBeneficiario": "95752207000129",
    }
    assert "x-id-idempotente" not in falso.cabecalhos[0]
    assert falso.escopos_pedidos == ["pagamento-boleto.write"]
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.EM_PROCESSAMENTO
    consultado = await adapter.consultar_pagamento(linha.id_banco, TipoPagamento.BOLETO)
    assert consultado.status is StatusPagamento.PAGO
    assert consultado.autenticacao == "AUT123"
    assert consultado.valor == Decimal("15.00")


async def test_guia_exige_vencimento(adapter, falso):
    sem = pagamento(TipoPagamento.GUIA, "g1", codigo_barras=BARRAS_GUIA)
    com = pagamento(
        TipoPagamento.GUIA, "g2", codigo_barras=BARRAS_GUIA, vencimento=HOJE
    )
    registrado = await adapter.pagar_lote(lote(sem, com))
    recusada, aceita = registrado.pagamentos
    assert recusada.status is StatusPagamento.RECUSADO
    assert "vencimento" in recusada.motivos[0]
    assert aceita.status is StatusPagamento.EM_PROCESSAMENTO
    assert falso.de_rota("pagamento")[0]["dataVencimento"] == "2026-10-29"
    assert "cpfCnpjBeneficiario" not in falso.de_rota("pagamento")[0]


async def test_cancelar_e_consultas_invalidas(adapter, falso):
    registrado = await adapter.pagar_lote(lote(boleto()))
    await adapter.cancelar_pagamentos(DEBITO, (registrado.pagamentos[0].id_banco,))
    assert falso.chamadas[-1][0] == "DELETE"
    with pytest.raises(Validacao, match="404"):
        await adapter.cancelar_pagamentos(DEBITO, ("nao-existe",))
    with pytest.raises(Validacao, match="nada"):
        await adapter.cancelar_pagamentos(DEBITO, ())
    with pytest.raises(Validacao, match="nao encontrado"):
        await adapter.consultar_pagamento("nao-existe", TipoPagamento.BOLETO)
    with pytest.raises(NaoSuportado, match="LOTE_LOCAL"):
        await adapter.consultar_lote("x", TipoPagamento.BOLETO)


async def test_transferencia_nao_declarada(adapter):
    ted = pagamento(
        TipoPagamento.TRANSFERENCIA,
        "t1",
        conta_credito=ContaBancaria(agencia="1", conta="1", digito="0", compe="001"),
        favorecido_documento="28133847044",
    )
    with pytest.raises(NaoSuportado, match="TRANSFERENCIA"):
        await adapter.pagar_lote(lote(ted))


async def test_webhook(adapter, falso):
    assert await adapter.consultar_webhook_pagamento() is None
    await adapter.configurar_webhook_pagamento("https://erp.example.com/inter")
    await adapter.configurar_webhook_pagamento(
        "https://erp.example.com/inter-boleto", tipo="boleto-pagamento"
    )
    assert await adapter.consultar_webhook_pagamento() == (
        "https://erp.example.com/inter"
    )
    await adapter.remover_webhook_pagamento("boleto-pagamento")
    assert falso.webhooks == {"pix-pagamento": "https://erp.example.com/inter"}
    with pytest.raises(Validacao, match="tipo"):
        await adapter.consultar_webhook_pagamento("cobranca")
    with pytest.raises(Validacao, match="https"):
        await adapter.configurar_webhook_pagamento("http://x")


def test_parse_webhook_pix_e_boleto(adapter):
    eventos = adapter.parse_webhook_pagamento(
        """[
          {"codigoSolicitacao": "c42f0787-02cb-4b31-827e-459ec9d7ece1",
           "endToEnd": "E00416968202610291000abcdefghijk", "status": "EFETIVADO",
           "valor": "0.01", "dataHoraMovimento": "2026-10-29T10:00:00",
           "tipoMovimentacao": "PAGAMENTO", "chave": "fulano@example.com"},
          {"codigoTransacao": "0d4f8e3a-1d1c-4c6b-8d0e-3a2b1c0d9e8f",
           "status": "REALIZADO", "valorPago": "15.00", "nsu": "123",
           "dataPagamento": "2026-10-29", "nomeBeneficiario": "X"},
          {"codigoTransacao": "1d4f8e3a-1d1c-4c6b-8d0e-3a2b1c0d9e8f",
           "status": "ERRO", "erros": [{"codigoErro": "E1", "descricaoErro": "x"}]}
        ]"""
    )
    pix, boleto_pago, boleto_erro = eventos
    assert pix.status is StatusPagamento.PAGO
    assert pix.autenticacao == "E00416968202610291000abcdefghijk"
    assert pix.valor == Decimal("0.01")
    assert pix.resposta_bruta["chave"] == MASCARA
    assert boleto_pago.status is StatusPagamento.PAGO
    assert boleto_pago.data == HOJE
    assert boleto_erro.status is StatusPagamento.REJEITADO
    assert boleto_erro.motivos == ("E1",)


@pytest.mark.parametrize(
    ("corpo", "erro"),
    [
        ("{", "JSON"),
        ("1", "formato"),
        ([1], "formato"),
        ([{"status": "EFETIVADO"}], "sem codigo"),
        ([{"codigoSolicitacao": "x", "status": "OUTRO"}], "nao mapeado"),
    ],
)
def test_parse_webhook_invalido(adapter, corpo, erro):
    with pytest.raises(Validacao, match=erro):
        adapter.parse_webhook_pagamento(corpo)


def test_vencimento_do_boleto():
    # Fator 1000 antes e depois do reinicio de 2025.
    codigo = "0019" + "0" + "1000" + "0" * 35
    dv = _dv_mod11_barras(codigo[:4] + codigo[5:])
    codigo = codigo[:4] + str(dv) + codigo[5:]
    assert vencimento_do_boleto(codigo, hoje=date(2000, 7, 1)) == date(2000, 7, 3)
    assert vencimento_do_boleto(codigo, hoje=HOJE) == date(2025, 2, 22)
    assert vencimento_do_boleto(BARRAS_GUIA) is None
    with pytest.raises(ValueError):
        vencimento_do_boleto("123")


def test_dinamico_vai_inteiro(adapter):
    corpo = adapter._corpo_pix(qr(brcode=brcode(url=URL_QR)))
    assert corpo["destinatario"]["pixCopiaECola"] == brcode(url=URL_QR)


async def test_resposta_ilegivel_de_pagamento_e_indeterminado(adapter, falso):
    falso.tipo_retorno = "OUTRO"
    registrado = await adapter.pagar_lote(lote(qr("q1"), qr("q2")))
    incerto, seguinte = registrado.pagamentos
    assert incerto.status is StatusPagamento.INDETERMINADO
    assert incerto.id_banco in falso.pix
    assert seguinte.motivos == ("NAO_ENVIADO",)
