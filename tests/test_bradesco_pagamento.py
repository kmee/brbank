from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
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
)
from brbank.adapters.bradesco_pagamento import (
    BradescoPagamentoAdapter,
    BradescoPagamentoConfig,
)
from brbank.transporte import SEM_RETRY
from tests.bradesco_pagamento_falso import (
    BradescoPagamentoFalso,
    _causa,
    _problema,
)
from tests.sicoob_pagamento_falso import brcode

AGORA = datetime(2026, 10, 30, 9, 31, 4)
HOJE = AGORA.date()
AMANHA = HOJE + timedelta(days=1)
DEBITO = ContaBancaria(agencia="2856", conta="50035", digito="6")
CNPJ_PAGADOR = "11222333000181"
CNPJ_FAVORECIDO = "95752207000129"
BARRAS_BOLETO = "00195313800000015000000003051284190526000117"
BARRAS_GUIA = "83630000000641400052836100812355200812351310"
ROTA_VALIDAR = "validaTituloPagamento"
ROTA_PRE = "pre-efetivacao-pagamento"
ROTA_PAGAR_BOLETO = "solicitacao/executar"
ROTA_PIX = "solicitar-transferencia"
ROTA_GUIA = "pagamentoContaConsumo"
ROTA_TED = "efetiva"


def config(**extra) -> BradescoPagamentoConfig:
    base = {
        "conta": "c1",
        "client_id": "id",
        "client_secret": "segredo-xyz",
        "pagador_documento": CNPJ_PAGADOR,
        "pagador_nome": "Empresa Ficticia",
        "agencia": "2856",
        "conta_corrente": "50035",
        "digito_conta": "6",
    }
    base.update(extra)
    return BradescoPagamentoConfig(**base)


class PagamentoTed(Pagamento):
    """O contrato ainda nao tem o nome do favorecido, que o TED exige."""

    favorecido_nome: str | None = None


class AdapterComTed(BradescoPagamentoAdapter):
    capacidades_pagamento = (
        BradescoPagamentoAdapter.capacidades_pagamento
        | CapacidadePagamento.TRANSFERENCIA
    )


@pytest.fixture
def falso():
    return BradescoPagamentoFalso()


def _montar(falso, dormir, classe=BradescoPagamentoAdapter, **extra):
    return classe(
        config(**extra), client=falso.cliente(), dormir=dormir, agora=lambda: AGORA
    )


@pytest.fixture
def adapter(falso, dormir):
    return _montar(falso, dormir)


@pytest.fixture
def adapter_ted(falso, dormir):
    return _montar(falso, dormir, classe=AdapterComTed)


def pagamento(tipo, chave="x1", valor="15.00", **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": tipo,
        "valor": Decimal(valor),
        "data": HOJE,
    }
    return Pagamento(**{**campos, **extra})


def pix(chave="p1", **extra):
    destino = extra.pop(
        "pix",
        DestinoPix(
            tipo_chave=TipoChavePix.EMAIL,
            chave="fornecedor@example.com",
            documento=CNPJ_FAVORECIDO,
            nome="Fornecedor Ficticio",
        ),
    )
    return pagamento(TipoPagamento.PIX, chave, pix=destino, **extra)


def boleto(chave="b1", **extra):
    campos = {
        "codigo_barras": BARRAS_BOLETO,
        "beneficiario_documento": CNPJ_FAVORECIDO,
    }
    return pagamento(TipoPagamento.BOLETO, chave, **{**campos, **extra})


def guia(chave="g1", **extra):
    return pagamento(TipoPagamento.GUIA, chave, codigo_barras=BARRAS_GUIA, **extra)


def ted(chave="t1", **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": TipoPagamento.TRANSFERENCIA,
        "valor": Decimal("1000.80"),
        "data": HOJE,
        "conta_credito": ContaBancaria(
            agencia="6234", conta="5475", digito="4", compe="341"
        ),
        "favorecido_documento": CNPJ_FAVORECIDO,
        "favorecido_nome": "Fornecedor Ficticio",
    }
    return PagamentoTed(**{**campos, **extra})


def lote(*pagamentos, conta=DEBITO):
    return LotePagamento(
        chave_idempotencia="l1",
        tipo=pagamentos[0].tipo,
        conta_debito=conta,
        pagamentos=pagamentos,
    )


# -- contrato e configuracao ---------------------------------------------------


def test_contrato():
    assert issubclass(BradescoPagamentoAdapter, AdapterPagamento)
    caps = BradescoPagamentoAdapter.capacidades_pagamento
    for cap in (
        "PIX",
        "BOLETO",
        "GUIA",
        "LOTE_LOCAL",
        "RESERVA_NUMERO",
        "CANCELAMENTO",
    ):
        assert CapacidadePagamento[cap] in caps
    for cap in ("LIBERACAO", "PIX_QRCODE", "WEBHOOK"):
        assert CapacidadePagamento[cap] not in caps
    assert BradescoPagamentoAdapter.validado_sandbox is False


def test_transferencia_so_com_o_nome_no_contrato():
    tem_campo = "favorecido_nome" in Pagamento.model_fields
    caps = BradescoPagamentoAdapter.capacidades_pagamento
    assert (CapacidadePagamento.TRANSFERENCIA in caps) is tem_campo


def test_config_sem_segredo_no_repr():
    texto = repr(config(pix_client_secret="outro-segredo"))
    for segredo in ("segredo-xyz", "outro-segredo", CNPJ_PAGADOR, "50035"):
        assert segredo not in texto


@pytest.mark.parametrize(
    ("extra", "erro"),
    [
        ({"ambiente": "uat"}, "ambiente"),
        ({"agencia": "abc"}, "agencia"),
        ({"conta_corrente": "12x"}, "conta"),
        ({"digito_conta": ""}, "digito"),
        ({"pagador_nome": " "}, "pagador_nome"),
        ({"pagador_documento": "123"}, "pagador_documento"),
    ],
)
def test_config_invalida(extra, erro):
    with pytest.raises(Validacao, match=erro):
        config(**extra)


def test_exige_mtls():
    with pytest.raises(Validacao, match="mTLS"):
        BradescoPagamentoAdapter(config(ambiente="sandbox"))


async def test_conta_diferente_da_configurada(adapter, falso):
    outra = ContaBancaria(agencia="2856", conta="99999", digito="6")
    with pytest.raises(Validacao, match="diferente"):
        await adapter.pagar_lote(lote(pix(), conta=outra))
    assert falso.rotas() == []


async def test_pix_copia_e_cola_nao_declarado(adapter):
    with pytest.raises(NaoSuportado):
        await adapter.pagar_lote(
            lote(
                pagamento(
                    TipoPagamento.PIX_QRCODE, "q1", "10.00", brcode=brcode("10.00")
                )
            )
        )


async def test_transferencia_sem_capacidade(adapter):
    if "favorecido_nome" in Pagamento.model_fields:
        pytest.skip("o contrato ja tem o campo")
    with pytest.raises(NaoSuportado):
        await adapter.pagar_lote(lote(ted()))


async def test_lote_e_local(adapter):
    assert await adapter.reservar_numero_lote() != await adapter.reservar_numero_lote()
    with pytest.raises(NaoSuportado, match="LOTE_LOCAL"):
        await adapter.consultar_lote("n1", TipoPagamento.PIX)
    with pytest.raises(NaoSuportado):
        await adapter.liberar_lote("n1")


# -- Pix -------------------------------------------------------------------------


async def test_pix_por_chave(adapter, falso):
    registrado = await adapter.pagar_lote(lote(pix(descricao="Fornecedor")))
    corpo = falso.de_rota(ROTA_PIX)[0]
    assert corpo == {
        "idtransacao": adapter.id_pix(pix()),
        "pagador": {"tipoChave": "AGENCIACONTA", "agencia": "2856", "conta": "50035"},
        "recebedor": {
            "tipoChave": "EMAIL",
            "chavePix": "fornecedor@example.com",
            "cpfCnpj": CNPJ_FAVORECIDO,
            "nomeFavorecido": "Fornecedor Ficticio",
        },
        "valor": "15.00",
        "descricao": "Fornecedor",
    }
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.PAGO
    assert linha.id_banco == corpo["idtransacao"]
    assert linha.autenticacao == "E60746948202610301715L2856oOXfn8"
    assert linha.valor == Decimal("15.00")
    assert registrado.status is StatusLote.PROCESSADO


async def test_pix_usa_host_e_token_proprios(adapter, falso):
    await adapter.pagar_lote(lote(pix()))
    assert falso.tokens == [("qrpix.bradesco.com.br", "/auth/server/oauth/token")]
    assert falso.chamadas[-1][1] == "qrpix.bradesco.com.br"
    assert falso.autorizacoes == ["Bearer tok"]


async def test_token_sem_bearer_e_caminho_configuravel(falso, dormir):
    adapter = _montar(
        falso, dormir, token_sem_bearer=True, caminho_token_pix="/v2/oauth/token"
    )
    await adapter.pagar_lote(lote(pix()))
    assert falso.autorizacoes == ["tok"]
    assert falso.tokens == [("qrpix.bradesco.com.br", "/v2/oauth/token")]


async def test_sandbox_usa_um_host_so(falso, dormir):
    adapter = _montar(falso, dormir, ambiente="sandbox")
    await adapter.pagar_lote(lote(pix()))
    await adapter.pagar_lote(lote(guia()))
    hosts = {c[1] for c in falso.chamadas}
    assert hosts == {"openapisandbox.prebanco.com.br"}


async def test_pix_por_dados_bancarios(adapter, falso):
    destino = DestinoPix(
        conta=ContaBancaria(
            agencia="3987", conta="200958", digito="x", ispb="60746948", compe="237",
            poupanca=True,
        ),
        documento=CNPJ_FAVORECIDO,
        nome="Fornecedor Ficticio",
    )  # fmt: skip
    registrado = await adapter.pagar_lote(lote(pix(pix=destino)))
    assert registrado.pagamentos[0].status is StatusPagamento.PAGO
    assert falso.de_rota(ROTA_PIX)[0]["recebedor"] == {
        "tipoChave": "AGENCIACONTA",
        "cpfCnpj": CNPJ_FAVORECIDO,
        "ispb": "60746948",
        "agencia": "3987",
        "conta": "200958",
        "digitoConta": "X",
        "tipoConta": "CONTA_POUPANCA",
        "nomeFavorecido": "Fornecedor Ficticio",
        "banco": "237",
    }


@pytest.mark.parametrize(
    ("tipo", "chave", "tipo_banco", "esperada"),
    [
        (TipoChavePix.TELEFONE, "(11) 91234-5678", "TELEFONE", "+5511912345678"),
        (TipoChavePix.DOCUMENTO, "95.752.207/0001-29", "CPFCNPJ", CNPJ_FAVORECIDO),
        (
            TipoChavePix.ALEATORIA,
            "A892D4C6-D00F-4F21-8C95-E165B3DC88AE",
            "EVP",
            "a892d4c6-d00f-4f21-8c95-e165b3dc88ae",
        ),
    ],
)
async def test_pix_tipos_de_chave(adapter, falso, tipo, chave, tipo_banco, esperada):
    destino = DestinoPix(tipo_chave=tipo, chave=chave)
    await adapter.pagar_lote(lote(pix(pix=destino)))
    recebedor = falso.de_rota(ROTA_PIX)[0]["recebedor"]
    assert recebedor == {"tipoChave": tipo_banco, "chavePix": esperada}


@pytest.mark.parametrize(
    ("destino", "motivo"),
    [
        (
            DestinoPix(tipo_chave=TipoChavePix.TELEFONE, chave="123"),
            "telefone",
        ),
        (
            DestinoPix(
                conta=ContaBancaria(agencia="1", conta="2", digito="3", compe="237"),
                documento=CNPJ_FAVORECIDO,
                nome="Nome",
            ),
            "ISPB",
        ),
        (
            DestinoPix(
                conta=ContaBancaria(
                    agencia="1", conta="2", digito="3", ispb="60746948"
                ),
                nome="Nome",
            ),
            "documento e nome",
        ),
        (
            DestinoPix(
                conta=ContaBancaria(
                    agencia="12345", conta="2", digito="3", ispb="60746948"
                ),
                documento=CNPJ_FAVORECIDO,
                nome="Nome",
            ),
            "agencia",
        ),
        (
            DestinoPix(
                tipo_chave=TipoChavePix.EMAIL,
                chave="a@example.com",
                documento="12ABC34500DE99",
            ),
            "numerico",
        ),
    ],
)
async def test_pix_destino_recusado_sem_chamar_o_banco(adapter, falso, destino, motivo):
    registrado = await adapter.pagar_lote(lote(pix(pix=destino)))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.RECUSADO
    assert motivo in linha.motivos[0]
    assert falso.rotas() == []


async def test_pix_nao_agenda(adapter, falso):
    registrado = await adapter.pagar_lote(lote(pix(data=AMANHA)))
    assert registrado.pagamentos[0].status is StatusPagamento.RECUSADO
    assert "nao agenda" in registrado.pagamentos[0].motivos[0]
    assert falso.rotas() == []


@pytest.mark.parametrize(
    ("status_banco", "status"),
    [
        ("EM_PROCESSAMENTO", StatusPagamento.EM_PROCESSAMENTO),
        ("REJEITADO", StatusPagamento.REJEITADO),
    ],
)
async def test_pix_status_na_resposta(adapter, falso, status_banco, status):
    falso.status_pix = status_banco
    registrado = await adapter.pagar_lote(lote(pix()))
    assert registrado.pagamentos[0].status is status


async def test_pix_rejeitado_leva_o_motivo(adapter, falso):
    falso.status_pix = "REJEITADO"
    registrado = await adapter.pagar_lote(lote(pix()))
    assert "1658" in registrado.pagamentos[0].motivos


async def test_pix_recusa_clara_e_as_outras_linhas_seguem(adapter, falso):
    falso.falhas[ROTA_PIX] = [(_problema, 412, "ME003", "Saldo insuficiente")]
    registrado = await adapter.pagar_lote(lote(pix("p1"), pix("p2")))
    um, dois = registrado.pagamentos
    assert um.status is StatusPagamento.RECUSADO
    assert "ME003" in um.motivos
    assert dois.status is StatusPagamento.PAGO
    assert registrado.status is StatusLote.ACEITO_PARCIAL


@pytest.mark.parametrize("falha", ["timeout", 500, 503, "html", "vazio"])
async def test_pix_sem_resposta_e_indeterminado(adapter, falso, falha):
    falso.falhas[ROTA_PIX] = [falha]
    registrado = await adapter.pagar_lote(lote(pix("p1"), pix("p2")))
    um, dois = registrado.pagamentos
    assert um.status is StatusPagamento.INDETERMINADO
    assert um.id_banco == adapter.id_pix(pix("p1"))
    assert dois.status is StatusPagamento.RECUSADO
    assert dois.motivos == ("NAO_ENVIADO",)
    assert len(falso.de_rota(ROTA_PIX)) == 1
    assert registrado.status is StatusLote.ACEITO_PARCIAL


@pytest.mark.parametrize("codigo", ["M0240", "M0241", "M0108", "4120", "4121", "5001"])
async def test_pix_ja_feito_ou_falha_interna_nao_e_recusa(adapter, falso, codigo):
    falso.falhas[ROTA_PIX] = [(_problema, 412, codigo, "detalhe")]
    registrado = await adapter.pagar_lote(lote(pix()))
    assert registrado.pagamentos[0].status is StatusPagamento.INDETERMINADO


async def test_pix_status_desconhecido_e_indeterminado(adapter, falso):
    falso.status_pix = "NOVO"
    registrado = await adapter.pagar_lote(lote(pix()))
    linha = registrado.pagamentos[0]
    assert linha.status is StatusPagamento.INDETERMINADO
    assert linha.id_banco


async def test_pix_reenvio_leva_o_mesmo_id_e_nao_paga_duas_vezes(adapter, falso):
    assert adapter.id_pix(pix("a")) == adapter.id_pix(pix("a"))
    assert adapter.id_pix(pix("a")) != adapter.id_pix(pix("b"))
    assert adapter.id_pix(pix("a")).isalnum() and len(adapter.id_pix(pix("a"))) <= 35
    await adapter.pagar_lote(lote(pix("a")))
    segundo = await adapter.pagar_lote(lote(pix("a")))
    assert segundo.pagamentos[0].status is StatusPagamento.INDETERMINADO
    assert len(falso.pix) == 1


async def test_pix_consulta(adapter, falso):
    registrado = await adapter.pagar_lote(lote(pix()))
    id_banco = registrado.pagamentos[0].id_banco
    consulta = await adapter.consultar_pagamento(id_banco, TipoPagamento.PIX)
    assert consulta.status is StatusPagamento.PAGO
    assert consulta.valor == Decimal("15.00")
    assert consulta.data == date(2026, 10, 30)
    assert consulta.autenticacao
    assert falso.chamadas[-1][1] == "qrpix.bradesco.com.br"


async def test_pix_consulta_nao_encontrada_e_status_desconhecido(adapter, falso):
    with pytest.raises(Validacao) as exc:
        await adapter.consultar_pagamento("naoexiste", TipoPagamento.PIX)
    assert exc.value.status_http == 404
    with pytest.raises(Validacao, match="invalido"):
        await adapter.consultar_pagamento("../x", TipoPagamento.PIX)
    with pytest.raises(Validacao, match="exige"):
        await adapter.consultar_pagamento("", TipoPagamento.PIX)
    registrado = await adapter.pagar_lote(lote(pix()))
    falso.status_pix = "NOVO"
    with pytest.raises(Validacao, match="nao mapeado"):
        await adapter.consultar_pagamento(
            registrado.pagamentos[0].id_banco, TipoPagamento.PIX
        )


async def test_pix_sem_dado_pessoal_na_resposta_guardada(adapter):
    registrado = await adapter.pagar_lote(lote(pix()))
    texto = str(registrado.pagamentos[0].resposta_bruta)
    assert "fornecedor@example.com" not in texto
    assert CNPJ_FAVORECIDO not in texto
    assert "Fornecedor Ficticio" not in texto
    assert "segredo-xyz" not in texto


# -- boleto ---------------------------------------------------------------------


async def test_boleto_tres_passos(adapter, falso):
    registrado = await adapter.pagar_lote(lote(boleto(descricao="Nota 12")))
    assert [r.rsplit("/", 1)[-1] for r in falso.rotas()] == [
        ROTA_VALIDAR,
        ROTA_PRE,
        "executar",
    ]
    assert falso.de_rota(ROTA_VALIDAR)[0] == {
        "tipoEntrada": 1,
        "dadosEntrada": "0000000000" + BARRAS_BOLETO,
        "bancoTitulo": 1,
        "agenciaDeb": 2856,
        "contaDeb": 50035,
    }
    pre = falso.de_rota(ROTA_PRE)[0]
    assert pre["agencia"] == 2856
    assert pre["valorTitulo"] == 15.0
    assert pre["dataVencimento"] == 20261231
    assert pre["dataMovimento"] == pre["dataPagamento"] == 20261030
    assert pre["horaTransacao"] == 93104
    assert pre["formaPagamento"] == 2
    corpo = falso.de_rota(ROTA_PAGAR_BOLETO)[0]
    assert corpo["valorTitulo"] == 15.0
    assert corpo["indicadorFuncao"] == "1"
    assert corpo["codigoAgencia"] == 2856
    assert corpo["cnpjCpfPtdor"] == 11222333
    assert corpo["filialCnpjPtdor"] == 1
    assert corpo["ctrlCnpjPtdor"] == 81
    assert corpo["linhaExtrato"] == "Nota 12"
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.PAGO
    assert linha.id_banco == str(corpo["transactionId"])
    assert linha.autenticacao == "7"


async def test_boleto_portador_cpf(falso, dormir):
    adapter = _montar(falso, dormir, pagador_documento="52998224725")
    await adapter.pagar_lote(lote(boleto()))
    corpo = falso.de_rota(ROTA_PAGAR_BOLETO)[0]
    assert corpo["cnpjCpfPtdor"] == 529982247
    assert corpo["filialCnpjPtdor"] == 0
    assert corpo["ctrlCnpjPtdor"] == 25


async def test_boleto_valor_em_centavos_e_opcional(falso, dormir):
    adapter = _montar(falso, dormir, boleto_valor_em_centavos=True)
    await adapter.pagar_lote(lote(boleto()))
    assert falso.de_rota(ROTA_PAGAR_BOLETO)[0]["valorTitulo"] == 1500
    assert falso.de_rota(ROTA_PRE)[0]["valorTitulo"] == 15.0


async def test_boleto_id_numerico(adapter):
    assert adapter.id_boleto(boleto("123456789")) == "123456789"
    assert adapter.id_boleto(boleto("000123")) == "123"
    for chave in ("0", "1234567890", "NF-12/A"):
        derivado = adapter.id_boleto(boleto(chave))
        assert derivado.isdigit() and 0 < int(derivado) <= 999_999_999
        assert derivado == adapter.id_boleto(boleto(chave))


async def test_boleto_de_outro_banco_leva_o_controle_cip(adapter, falso):
    falso.consulta_cip = "S"
    falso.ctrl_cip = "0237CP01251271370358"
    registrado = await adapter.pagar_lote(lote(boleto()))
    assert registrado.pagamentos[0].status is StatusPagamento.PAGO
    assert falso.de_rota(ROTA_PRE)[0]["numeroCtrlCip"] == falso.ctrl_cip
    assert falso.de_rota(ROTA_PAGAR_BOLETO)[0]["numeroCtrlCip"] == falso.ctrl_cip


async def test_boleto_cip_sem_controle_nao_paga(adapter, falso):
    falso.consulta_cip = "S"
    registrado = await adapter.pagar_lote(lote(boleto()))
    assert registrado.pagamentos[0].status is StatusPagamento.RECUSADO
    assert falso.de_rota(ROTA_PAGAR_BOLETO) == []


async def test_boleto_vencimento_do_chamador_prevalece(adapter, falso):
    await adapter.pagar_lote(lote(boleto(vencimento=date(2026, 11, 5))))
    assert falso.de_rota(ROTA_PRE)[0]["dataVencimento"] == 20261105


async def test_boleto_sem_vencimento(adapter, falso):
    falso.vencimento_boleto = 0
    registrado = await adapter.pagar_lote(lote(boleto()))
    assert "vencimento" in registrado.pagamentos[0].motivos[0]
    assert falso.de_rota(ROTA_PAGAR_BOLETO) == []


async def test_boleto_poupanca_recusado(falso, dormir):
    adapter = _montar(falso, dormir)
    conta = ContaBancaria(agencia="2856", conta="50035", digito="6", poupanca=True)
    registrado = await adapter.pagar_lote(lote(boleto(), conta=conta))
    assert registrado.pagamentos[0].status is StatusPagamento.RECUSADO
    assert falso.rotas() == []


async def test_boleto_agendado(adapter, falso):
    registrado = await adapter.pagar_lote(lote(boleto(data=AMANHA)))
    assert registrado.pagamentos[0].status is StatusPagamento.AGENDADO


async def test_boleto_dia_nao_util_vira_agendado(adapter, falso):
    falso.quitacao_boleto = 20261103
    registrado = await adapter.pagar_lote(lote(boleto()))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.AGENDADO
    assert linha.data == date(2026, 11, 3)


@pytest.mark.parametrize("rota", [ROTA_VALIDAR, ROTA_PRE])
async def test_boleto_recusa_antes_de_pagar(adapter, falso, rota):
    falso.falhas[rota] = [(_causa, 412, "CBCA0146 - VALOR DIVERGENTE")]
    registrado = await adapter.pagar_lote(lote(boleto("b1"), boleto("b2")))
    um, dois = registrado.pagamentos
    assert um.status is StatusPagamento.RECUSADO
    assert "CBCA0146 - VALOR DIVERGENTE" in um.motivos
    assert dois.status is StatusPagamento.PAGO
    assert len(falso.de_rota(ROTA_PAGAR_BOLETO)) == 1


@pytest.mark.parametrize("rota", [ROTA_VALIDAR, ROTA_PRE])
async def test_boleto_banco_fora_antes_de_pagar_nao_e_indeterminado(
    falso, dormir, rota
):
    adapter = BradescoPagamentoAdapter(
        config(),
        client=falso.cliente(),
        dormir=dormir,
        agora=lambda: AGORA,
        retry=SEM_RETRY,
    )
    falso.falhas[rota] = ["timeout"]
    registrado = await adapter.pagar_lote(lote(boleto("b1"), boleto("b2")))
    um, dois = registrado.pagamentos
    assert um.status is StatusPagamento.RECUSADO
    assert um.motivos[0] == "NAO_ENVIADO"
    assert dois.motivos == ("NAO_ENVIADO",)
    assert falso.de_rota(ROTA_PAGAR_BOLETO) == []


async def test_boleto_erro_no_corpo_com_http_200(adapter, falso):
    falso.falhas[ROTA_PRE] = [
        (
            lambda: httpx.Response(
                200, json={"status": 412, "causa": "CBCA0071 - SALDO"}
            ),
        )
    ]
    registrado = await adapter.pagar_lote(lote(boleto()))
    assert "CBCA0071 - SALDO" in registrado.pagamentos[0].motivos


@pytest.mark.parametrize("falha", ["timeout", 500, "html", "vazio"])
async def test_boleto_efetivacao_sem_resposta(adapter, falso, falha):
    falso.falhas[ROTA_PAGAR_BOLETO] = [falha]
    registrado = await adapter.pagar_lote(lote(boleto("b1"), boleto("b2")))
    um, dois = registrado.pagamentos
    assert um.status is StatusPagamento.INDETERMINADO
    assert um.id_banco == adapter.id_boleto(boleto("b1"))
    assert dois.motivos == ("NAO_ENVIADO",)
    assert len(falso.de_rota(ROTA_PAGAR_BOLETO)) == 1


async def test_boleto_status_500_no_corpo_e_indeterminado(adapter, falso):
    falso.falhas[ROTA_PAGAR_BOLETO] = [
        (lambda: httpx.Response(200, json={"status": 500, "causa": "x"}),)
    ]
    registrado = await adapter.pagar_lote(lote(boleto()))
    assert registrado.pagamentos[0].status is StatusPagamento.INDETERMINADO


@pytest.mark.parametrize(
    "causa", ["CBCA0108 - DUPLICADO", "CBCA0037 - TEMPO EXCEDIDO", "2135 - DUPLICIDADE"]
)
async def test_boleto_duplicidade_nao_e_recusa(adapter, falso, causa):
    falso.falhas[ROTA_PAGAR_BOLETO] = [(_causa, 412, causa)]
    registrado = await adapter.pagar_lote(lote(boleto()))
    assert registrado.pagamentos[0].status is StatusPagamento.INDETERMINADO


@pytest.mark.parametrize("rota", [ROTA_VALIDAR, ROTA_PRE])
async def test_boleto_duplicidade_no_passo_previo_nao_e_recusa(adapter, falso, rota):
    falso.falhas[rota] = [(_causa, 412, "CBCA0108 - DUPLICADO")]
    registrado = await adapter.pagar_lote(lote(boleto()))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.INDETERMINADO
    assert linha.id_banco == adapter.id_boleto(boleto())
    assert falso.de_rota(ROTA_PAGAR_BOLETO) == []


async def test_boleto_recusa_na_efetivacao(adapter, falso):
    falso.falhas[ROTA_PAGAR_BOLETO] = [(_causa, 412, "CBCA0071 - SALDO INSUFICIENTE")]
    registrado = await adapter.pagar_lote(lote(boleto()))
    assert registrado.pagamentos[0].status is StatusPagamento.RECUSADO


@pytest.mark.parametrize(
    ("situacao", "status"),
    [
        (1, StatusPagamento.PAGO),
        (2, StatusPagamento.CANCELADO),
        (3, StatusPagamento.DEVOLVIDO),
        (7, StatusPagamento.AGENDADO),
        (8, StatusPagamento.CANCELADO),
        (9, StatusPagamento.CANCELADO),
        (11, StatusPagamento.PAGO),
        (12, StatusPagamento.REJEITADO),
    ],
)
async def test_boleto_consulta(adapter, falso, situacao, status):
    registrado = await adapter.pagar_lote(lote(boleto()))
    falso.situacao_boleto = situacao
    consulta = await adapter.consultar_pagamento(
        registrado.pagamentos[0].id_banco, TipoPagamento.BOLETO
    )
    assert consulta.status is status
    assert consulta.valor == Decimal("15.00")
    assert consulta.data == date(2026, 10, 30)
    assert consulta.autenticacao == "7"
    assert "PAGADOR FICTICIO" not in str(consulta.resposta_bruta)
    assert bool(consulta.motivos) is (situacao == 12)


async def test_boleto_consulta_erros(adapter, falso):
    with pytest.raises(Validacao, match="invalido"):
        await adapter.consultar_pagamento("abc", TipoPagamento.BOLETO)
    with pytest.raises(Validacao) as exc:
        await adapter.consultar_pagamento("99", TipoPagamento.BOLETO)
    assert exc.value.status_http == 412
    registrado = await adapter.pagar_lote(lote(boleto()))
    falso.situacao_boleto = 99
    with pytest.raises(Validacao, match="nao mapeada"):
        await adapter.consultar_pagamento(
            registrado.pagamentos[0].id_banco, TipoPagamento.BOLETO
        )


async def test_boleto_sem_dado_pessoal_na_resposta_guardada(adapter):
    registrado = await adapter.pagar_lote(lote(boleto()))
    assert "PAGADOR FICTICIO" not in str(registrado.pagamentos[0].resposta_bruta)


async def test_cancelar_agendamento_de_boleto(adapter, falso):
    registrado = await adapter.pagar_lote(lote(boleto(data=AMANHA)))
    id_banco = registrado.pagamentos[0].id_banco
    await adapter.cancelar_pagamentos(DEBITO, (id_banco,))
    assert falso.de_rota("exclusao/executar")[0] == {
        "versao": 2,
        "bancoDebito": 237,
        "agenciaDebito": 2856,
        "digitoAgencia": 0,
        "contaDebito": 50035,
        "digitoConta": "6",
        "numeroProtocolo": 0,
        "idTransacao": int(id_banco),
    }


async def test_cancelar_erros(adapter, falso):
    with pytest.raises(Validacao, match="nada"):
        await adapter.cancelar_pagamentos(DEBITO, ())
    with pytest.raises(Validacao, match="so agendamento"):
        await adapter.cancelar_pagamentos(DEBITO, ("abc|2026-10-30",))
    with pytest.raises(Validacao):
        await adapter.cancelar_pagamentos(DEBITO, ("99",))


# -- guia ------------------------------------------------------------------------


async def test_guia_dois_passos(adapter, falso):
    registrado = await adapter.pagar_lote(lote(guia(descricao="Conta de luz")))
    validar, pagar = falso.de_rota(ROTA_GUIA)
    assert validar["tipoRegistro"] == 0
    assert pagar == {
        "agencia": 2856,
        "digitoAgencia": 0,
        "conta": 50035,
        "digitoConta": "6",
        "tipoConta": 1,
        "codigoBarras": BARRAS_GUIA,
        "dataDebito": "2026-10-30",
        "idTransacao": adapter.id_guia(guia()),
        "valorPrincipal": 15.0,
        "descricaoCliente": "Conta de luz",
        "tipoRegistro": 1,
    }
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.PAGO
    assert linha.id_banco == f"{adapter.id_guia(guia())}|2026-10-30"
    assert linha.autenticacao == "45154760"


async def test_guia_id_estavel_e_curto(adapter):
    id_guia = adapter.id_guia(guia("g1"))
    assert id_guia == adapter.id_guia(guia("g1"))
    assert id_guia != adapter.id_guia(guia("g2"))
    assert len(id_guia) == 9 and id_guia.isalnum()


async def test_guia_agendada(adapter, falso):
    registrado = await adapter.pagar_lote(lote(guia(data=AMANHA)))
    assert registrado.pagamentos[0].status is StatusPagamento.AGENDADO


async def test_guia_recusa_na_validacao_nao_paga(adapter, falso):
    falso.retorno_guia = 15
    registrado = await adapter.pagar_lote(lote(guia()))
    assert registrado.pagamentos[0].status is StatusPagamento.RECUSADO
    assert len(falso.de_rota(ROTA_GUIA)) == 1
    assert "15" in registrado.pagamentos[0].motivos


async def test_guia_recusa_422_com_retorno(adapter, falso):
    falso.falhas[ROTA_GUIA] = [
        (lambda: httpx.Response(422, json={"retorno": 18, "mensagem": "Valor"}),)
    ]
    registrado = await adapter.pagar_lote(lote(guia()))
    assert registrado.pagamentos[0].status is StatusPagamento.RECUSADO


@pytest.mark.parametrize("falha", ["timeout", 500, "html", "vazio"])
async def test_guia_efetivacao_sem_resposta(adapter, falso, falha):
    original = falso._falha

    def so_na_efetivacao(caminho, corpo, request):
        if corpo.get("tipoRegistro") == 1:
            falso.falhas[ROTA_GUIA] = [falha]
        return original(caminho, corpo, request)

    falso._falha = so_na_efetivacao
    registrado = await adapter.pagar_lote(lote(guia()))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.INDETERMINADO
    assert linha.id_banco == f"{adapter.id_guia(guia())}|2026-10-30"


async def test_guia_duplicada_nao_e_recusa(adapter, falso):
    await adapter.pagar_lote(lote(guia()))
    segundo = await adapter.pagar_lote(lote(guia()))
    assert segundo.pagamentos[0].status is StatusPagamento.INDETERMINADO


@pytest.mark.parametrize("retorno", [57, "006"])
async def test_guia_duplicidade_na_validacao_nao_e_recusa(adapter, falso, retorno):
    falso.falhas[ROTA_GUIA] = [
        (lambda: httpx.Response(200, json={"retorno": retorno}),)
    ]
    registrado = await adapter.pagar_lote(lote(guia()))
    assert registrado.pagamentos[0].status is StatusPagamento.INDETERMINADO
    assert len(falso.de_rota(ROTA_GUIA)) == 1


async def test_guia_duplicidade_422_na_validacao_nao_e_recusa(adapter, falso):
    falso.falhas[ROTA_GUIA] = [(lambda: httpx.Response(422, json={"retorno": 57}),)]
    registrado = await adapter.pagar_lote(lote(guia()))
    assert registrado.pagamentos[0].status is StatusPagamento.INDETERMINADO


async def test_guia_2xx_sem_retorno_e_indeterminado(adapter, falso):
    original = falso._pagar_guia

    def sem_retorno(corpo):
        resposta = original(corpo)
        if corpo["tipoRegistro"] == 1:
            return httpx.Response(200, json={"ok": True})
        return resposta

    falso._pagar_guia = sem_retorno
    registrado = await adapter.pagar_lote(lote(guia()))
    assert registrado.pagamentos[0].status is StatusPagamento.INDETERMINADO


@pytest.mark.parametrize(
    ("tipos", "status"),
    [
        ({3: True, 4: False, 5: False}, StatusPagamento.PAGO),
        ({3: False, 4: True, 5: False}, StatusPagamento.AGENDADO),
        ({3: False, 4: False, 5: True}, StatusPagamento.REJEITADO),
    ],
)
async def test_guia_consulta_pelo_tipo(adapter, falso, tipos, status):
    registrado = await adapter.pagar_lote(lote(guia()))
    falso.guias_por_tipo = tipos
    consulta = await adapter.consultar_pagamento(
        registrado.pagamentos[0].id_banco, TipoPagamento.GUIA
    )
    assert consulta.status is status
    assert consulta.valor == Decimal("15.00")
    assert consulta.data == HOJE
    assert consulta.autenticacao == "45551127"
    assert BARRAS_GUIA not in str(consulta.resposta_bruta)
    assert falso.chamadas[-1][2].endswith("/2856/50035/1")


async def test_guia_consulta_nao_encontrada_e_id_invalido(adapter):
    with pytest.raises(Validacao) as exc:
        await adapter.consultar_pagamento("ABC|2026-10-30", TipoPagamento.GUIA)
    assert exc.value.status_http == 404
    with pytest.raises(Validacao, match="invalido"):
        await adapter.consultar_pagamento("ABC", TipoPagamento.GUIA)


# -- TED -------------------------------------------------------------------------


async def test_ted(adapter_ted, falso):
    registrado = await adapter_ted.pagar_lote(lote(ted()))
    corpo = falso.de_rota(ROTA_TED)[0]
    assert corpo["agenciaRemetente"] == 2856
    assert corpo["contaRemetenteComDigito"] == 500356
    assert corpo["tipoDePessoaRemetente"] == "J"
    assert corpo["nomeClienteRemetente"] == "Empresa Ficticia"
    assert corpo["bancoDestinatario"] == 341
    assert corpo["contaDestinatario"] == 54754
    assert corpo["numeroInscricao"] == "095752207"
    assert corpo["numeroFilial"] == "00001"
    assert corpo["numeroControle"] == "29"
    assert corpo["valorDaTransferencia"] == 1000.8
    assert corpo["finalidadeDaTransferencia"] == 5
    assert corpo["dataMovimento"] == "30.10.2026"
    assert corpo["tipoDeDoc"] == "E"
    assert len(corpo["codigoIdentificadorDaTransferencia"]) <= 25
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.EM_PROCESSAMENTO
    assert linha.id_banco == "278611430.10.2026"
    assert linha.valor == Decimal("1000.80")
    assert "FICTICIO" not in str(linha.resposta_bruta)
    assert "Fornecedor" not in str(linha.resposta_bruta)


async def test_ted_mesma_titularidade_e_cpf(falso, dormir):
    adapter = _montar(falso, dormir, classe=AdapterComTed)
    await adapter.pagar_lote(lote(ted(favorecido_documento=CNPJ_PAGADOR)))
    assert falso.de_rota(ROTA_TED)[0]["tipoDeDoc"] == "D"
    await adapter.pagar_lote(lote(ted("t2", favorecido_documento="52998224725")))
    corpo = falso.de_rota(ROTA_TED)[1]
    assert corpo["tipodePessoaDestinatario"] == "F"
    assert corpo["numeroInscricao"] == "529982247"
    assert corpo["numeroFilial"] == "00000"
    assert corpo["numeroControle"] == "25"


@pytest.mark.parametrize(
    ("campos", "motivo"),
    [
        ({"favorecido_nome": None}, "nome"),
        ({"data": AMANHA}, "nao agenda"),
        (
            {"conta_credito": ContaBancaria(agencia="1", conta="2", digito="3")},
            "COMPE",
        ),
        (
            {
                "conta_credito": ContaBancaria(
                    agencia="1", conta="2", digito="3", compe="237"
                )
            },
            "Bradesco",
        ),
        (
            {
                "conta_credito": ContaBancaria(
                    agencia="1", conta="2", digito="X", compe="341"
                )
            },
            "numerica",
        ),
        ({"favorecido_documento": "12ABC34500DE99"}, "numerico"),
    ],
)
async def test_ted_recusado_sem_chamar_o_banco(adapter_ted, falso, campos, motivo):
    registrado = await adapter_ted.pagar_lote(lote(ted(**campos)))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.RECUSADO
    assert motivo in linha.motivos[0]
    assert falso.rotas() == []


async def test_ted_retorno_de_erro(adapter_ted, falso):
    falso.retorno_ted = 42
    registrado = await adapter_ted.pagar_lote(lote(ted()))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.RECUSADO
    assert "42" in linha.motivos


@pytest.mark.parametrize("falha", ["timeout", 500, "html", "vazio"])
async def test_ted_sem_resposta_nao_tem_id_para_consultar(adapter_ted, falso, falha):
    falso.falhas[ROTA_TED] = [falha]
    registrado = await adapter_ted.pagar_lote(lote(ted("t1"), ted("t2")))
    um, dois = registrado.pagamentos
    assert um.status is StatusPagamento.INDETERMINADO
    assert um.id_banco == ""
    assert dois.motivos == ("NAO_ENVIADO",)
    assert len(falso.de_rota(ROTA_TED)) == 1


async def test_ted_2xx_com_chave_e_retorno_desconhecido_e_indeterminado(
    adapter_ted, falso
):
    falso.falhas[ROTA_TED] = [
        (
            lambda: httpx.Response(
                200,
                json={"codigoDeRetorno": 999, "chaveUnicaParaApi": "2786114abc"},
            ),
        )
    ]
    registrado = await adapter_ted.pagar_lote(lote(ted()))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.INDETERMINADO
    assert linha.id_banco == "278611430.10.2026"


@pytest.mark.parametrize(
    ("status_banco", "status"),
    [
        ("EM PROCESSAMENTO", StatusPagamento.EM_PROCESSAMENTO),
        ("em_processamento", StatusPagamento.EM_PROCESSAMENTO),
        ("PROCESSADA", StatusPagamento.PAGO),
        ("DEVOLVIDA", StatusPagamento.DEVOLVIDO),
    ],
)
async def test_ted_consulta(adapter_ted, falso, status_banco, status):
    falso.status_ted = status_banco
    consulta = await adapter_ted.consultar_pagamento(
        "278611430.10.2026", TipoPagamento.TRANSFERENCIA
    )
    assert consulta.status is status
    assert consulta.valor == Decimal("1000.80")
    assert "FICTICIO" not in str(consulta.resposta_bruta)
    assert bool(consulta.motivos) is (status is StatusPagamento.DEVOLVIDO)


async def test_ted_consulta_erros(adapter_ted, falso):
    with pytest.raises(Validacao, match="invalido"):
        await adapter_ted.consultar_pagamento("abc", TipoPagamento.TRANSFERENCIA)
    with pytest.raises(Validacao) as exc:
        await adapter_ted.consultar_pagamento(
            "111111130.10.2026", TipoPagamento.TRANSFERENCIA
        )
    assert exc.value.status_http == 422
    falso.status_ted = "NOVO"
    with pytest.raises(Validacao, match="nao mapeado"):
        await adapter_ted.consultar_pagamento(
            "278611430.10.2026", TipoPagamento.TRANSFERENCIA
        )


async def test_consulta_de_tipo_sem_rota(adapter):
    with pytest.raises(NaoSuportado):
        await adapter.consultar_pagamento("x", TipoPagamento.PIX_QRCODE)
