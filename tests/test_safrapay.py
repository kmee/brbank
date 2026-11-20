from datetime import date
from decimal import Decimal

import pytest

from brbank import (
    Capacidade,
    Duplicado,
    Encargos,
    Indisponivel,
    Instrucao,
    NaoSuportado,
    StatusCanonico,
    TipoInstrucao,
    Validacao,
)
from brbank.adapters.safrapay import (
    TABELA_STATUS,
    URL_PRODUCAO,
    URL_SANDBOX,
    SafrapayAdapter,
    SafrapayConfig,
)
from brbank.modelos import CanalLiquidacao, MotivoBaixa
from tests import safrapay_falso
from tests.conftest import fazer_titulo
from tests.safrapay_falso import SafrapayFalso

_ID_QUALQUER = "0b2edbfb-4770-43f6-afa8-af30d7ff8fc5"


def _adapter(dormir, falso=None, **config):
    falso = falso or SafrapayFalso()
    cfg = SafrapayConfig(
        conta="conta-safrapay",
        merchant_token=safrapay_falso.CHAVE,
        ambiente="sandbox",
        **config,
    )
    return SafrapayAdapter(cfg, client=falso.cliente(), dormir=dormir), falso


# --- configuracao -------------------------------------------------------------


def test_config_valida_meio_ambiente_e_chave():
    with pytest.raises(Validacao, match="meio"):
        SafrapayConfig(conta="c", merchant_token="mk_x", meio="CARTAO")
    with pytest.raises(Validacao, match="ambiente"):
        SafrapayConfig(conta="c", merchant_token="mk_x", ambiente="hml")
    with pytest.raises(Validacao, match="merchant_token"):
        SafrapayConfig(conta="c", merchant_token=" ")
    assert SafrapayConfig(conta="c", merchant_token="mk_x", meio="pix").meio == "PIX"
    assert SafrapayConfig(conta="c", merchant_token="mk_x").url_base == URL_PRODUCAO
    sandbox = SafrapayConfig(conta="c", merchant_token="mk_x", ambiente="sandbox")
    assert sandbox.url_base == URL_SANDBOX
    assert "mk_x" not in repr(sandbox)


def test_capacidades_e_instrucoes_por_meio():
    assert SafrapayAdapter.validado_sandbox is False
    assert not SafrapayAdapter.capacidades & Capacidade.HIBRIDO
    assert not SafrapayAdapter.capacidades & Capacidade.PIX_COBV
    assert SafrapayAdapter.instrucoes_da_config({}) == {
        TipoInstrucao.PRORROGACAO,
        TipoInstrucao.BAIXA,
    }
    assert SafrapayAdapter.instrucoes_da_config({"meio": "pix"}) == {
        TipoInstrucao.BAIXA
    }


# --- autenticacao -------------------------------------------------------------


async def test_token_trocado_uma_vez_e_renovado_apos_401(dormir):
    adapter, falso = _adapter(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.consultar_titulo(registrado.id_banco)
    assert falso.tokens_emitidos == 1
    pedido_token = falso.cabecalhos[0]
    assert pedido_token["authorization"] == safrapay_falso.CHAVE  # sem Bearer
    assert all(
        c["authorization"].startswith("Bearer jwt-falso-") for c in falso.cabecalhos[1:]
    )
    falso.revogar_tokens()
    await adapter.consultar_titulo(registrado.id_banco)
    assert falso.tokens_emitidos == 2


async def test_chave_recusada_vira_validacao(dormir):
    falso = SafrapayFalso(chave="mk_outra")
    adapter, _ = _adapter(dormir, falso)
    with pytest.raises(Validacao, match="credenciais"):
        await adapter.consultar_titulo(_ID_QUALQUER)


# --- boleto -------------------------------------------------------------------


async def test_registrar_boleto_monta_o_corpo_documentado(dormir):
    adapter, falso = _adapter(dormir, dias_baixa_automatica=7)
    titulo = fazer_titulo().model_copy(
        update={"mensagens": ("Nao receber", "apos 30 dias")}
    )
    registrado = await adapter.registrar_titulo(titulo)

    cobranca = falso.ultimo_corpo["charge"]
    assert cobranca["merchantChargeId"] == "chave-1"
    assert cobranca["source"] == 7
    assert cobranca["deadline"] == "2026-12-10"
    assert cobranca["bankIssuerDaysToCancel"] == 7
    assert cobranca["transactions"] == [
        {"amount": 15000, "instructions": "Nao receber apos 30 dias"}
    ]
    cliente = cobranca["customer"]
    assert cliente["documentType"] == 1 and cliente["entityType"] == 1
    assert cliente["document"] == "52998224725"
    assert cliente["address"]["state"] == "MG"
    assert cliente["address"]["zipCode"] == "30110000"
    assert cliente["address"]["country"] == "BR"

    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.seu_numero == "FAT-chave-1"
    assert registrado.nosso_numero == "0000000000094"
    assert len(registrado.linha_digitavel) == 47
    assert len(registrado.codigo_barras) == 44
    assert registrado.pix_copia_cola is None
    assert registrado.valor_pago is None


async def test_registro_repetido_vira_duplicado(dormir):
    adapter, falso = _adapter(dormir)
    primeiro = await adapter.registrar_titulo(fazer_titulo())
    with pytest.raises(Duplicado) as erro:
        await adapter.registrar_titulo(fazer_titulo())
    assert erro.value.id_banco == primeiro.id_banco
    assert [m for m, c in falso.chamadas if c == "/v2/charge/boleto"] == ["POST"]


async def test_post_de_emissao_nao_repete_depois_de_503(dormir):
    adapter, falso = _adapter(dormir)
    falso.falhas_503_post = 1
    with pytest.raises(Indisponivel):
        await adapter.registrar_titulo(fazer_titulo())
    posts = [c for m, c in falso.chamadas if m == "POST" and c == "/v2/charge/boleto"]
    assert posts == ["/v2/charge/boleto"]


async def test_consulta_repete_depois_de_503(dormir, dormidas):
    adapter, falso = _adapter(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.falhas_503 = 2
    assert (await adapter.consultar_titulo(registrado.id_banco)).status is (
        StatusCanonico.REGISTRADO
    )
    assert len(dormidas) == 2


async def test_encargos_recusados_e_abatimento_reduz_o_valor(dormir):
    adapter, falso = _adapter(dormir)
    com_multa = fazer_titulo().model_copy(
        update={"encargos": Encargos(multa_percentual=Decimal("2"))}
    )
    with pytest.raises(Validacao, match="multa"):
        await adapter.registrar_titulo(com_multa)
    com_abatimento = fazer_titulo("c2").model_copy(
        update={"encargos": Encargos(abatimento=Decimal("50.00"))}
    )
    await adapter.registrar_titulo(com_abatimento)
    assert falso.ultimo_corpo["charge"]["transactions"][0]["amount"] == 10000


async def test_boleto_abaixo_do_minimo_e_sem_endereco(dormir):
    adapter, falso = _adapter(dormir)
    with pytest.raises(Validacao, match="R\\$ 5,00"):
        await adapter.registrar_titulo(fazer_titulo(valor="4.99"))
    titulo = fazer_titulo()
    sem_endereco = titulo.model_copy(
        update={"pagador": titulo.pagador.model_copy(update={"endereco": None})}
    )
    with pytest.raises(Validacao, match="endereco"):
        await adapter.registrar_titulo(sem_endereco)
    assert not any(c.startswith("/v2/charge/boleto") for _, c in falso.chamadas)


async def test_falha_com_http_200_e_success_false(dormir):
    falso = SafrapayFalso(erro_em_200=True)
    adapter, _ = _adapter(dormir, falso)
    titulo = fazer_titulo()
    sem_email = titulo.model_copy(
        update={"pagador": titulo.pagador.model_copy(update={"email": None})}
    )
    with pytest.raises(Validacao, match=r"1: A propriedade Customer\.Email") as erro:
        await adapter.registrar_titulo(sem_email)
    assert erro.value.status_http == 200


async def test_consulta_situacoes_do_boleto(dormir):
    adapter, falso = _adapter(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.expirar(registrado.id_banco)
    # Boleto vencido continua pagavel ate o emissor cancelar.
    assert (await adapter.consultar_titulo(registrado.id_banco)).status is (
        StatusCanonico.REGISTRADO
    )
    falso.definir_status(registrado.id_banco, "ErrorCreation")
    assert (await adapter.consultar_titulo(registrado.id_banco)).status is (
        StatusCanonico.REJEITADO
    )
    falso.definir_status(registrado.id_banco, "Pending")
    assert (await adapter.consultar_titulo(registrado.id_banco)).status is (
        StatusCanonico.PENDENTE_REGISTRO
    )
    falso.definir_status(registrado.id_banco, "Captured")
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_liquidacao_traz_valor_e_canal(dormir):
    adapter, falso = _adapter(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    pago = await adapter.consultar_titulo(registrado.id_banco)
    assert pago.status is StatusCanonico.LIQUIDADO
    assert pago.valor_pago == Decimal("150.00")
    assert pago.canal_liquidacao is CanalLiquidacao.CODIGO_BARRAS
    assert pago.data_credito is None


async def test_id_que_nao_e_uuid_nao_chega_ao_gateway(dormir):
    adapter, falso = _adapter(dormir)
    with pytest.raises(Validacao, match="UUID"):
        await adapter.consultar_titulo("../v2/charge")
    assert falso.chamadas == []


async def test_inexistente_vira_validacao_legivel(dormir):
    adapter, _ = _adapter(dormir)
    with pytest.raises(Validacao, match="8: Cobranca") as erro:
        await adapter.consultar_titulo(_ID_QUALQUER)
    assert erro.value.status_http == 400
    assert not erro.value.retryable


# --- instrucoes ---------------------------------------------------------------


async def test_prorrogacao_fica_pendente_ate_o_banco_confirmar(dormir):
    adapter, falso = _adapter(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    instrucao = Instrucao(
        chave_idempotencia="p1",
        tipo=TipoInstrucao.PRORROGACAO,
        nova_data_vencimento=date(2026, 12, 20),
    )
    resultado = await adapter.enviar_instrucao(registrado.id_banco, instrucao)
    assert falso.ultimo_corpo == {
        "chargeId": registrado.id_banco,
        "newDueDate": "2026-12-20",
    }
    assert resultado.status is StatusCanonico.REGISTRADO
    assert resultado.resposta_bruta["transactions"][0]["transactionStatus"] == (
        "PendingNewDeadline"
    )
    sem_data = Instrucao(chave_idempotencia="p2", tipo=TipoInstrucao.PRORROGACAO)
    with pytest.raises(Validacao, match="nova_data_vencimento"):
        await adapter.enviar_instrucao(registrado.id_banco, sem_data)
    abatimento = Instrucao(
        chave_idempotencia="p3", tipo=TipoInstrucao.ABATIMENTO, valor=Decimal("1")
    )
    with pytest.raises(NaoSuportado):
        await adapter.enviar_instrucao(registrado.id_banco, abatimento)


async def test_baixa_no_dia_da_emissao_explica_a_regra(dormir):
    falso = SafrapayFalso(regra_dia_seguinte=True)
    adapter, _ = _adapter(dormir, falso)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    with pytest.raises(Validacao, match="dia seguinte"):
        await adapter.baixar_titulo(registrado.id_banco)
    falso.virar_o_dia()
    baixado = await adapter.baixar_titulo(registrado.id_banco)
    assert baixado.status is StatusCanonico.BAIXADO
    # Ja baixado: nao pede de novo.
    chamadas = len(falso.chamadas)
    assert (await adapter.baixar_titulo(registrado.id_banco)).status is (
        StatusCanonico.BAIXADO
    )
    assert not any(m == "PUT" for m, _ in falso.chamadas[chamadas:])


async def test_baixa_de_cobranca_paga_e_recusada_sem_chamar_o_cancelamento(dormir):
    adapter, falso = _adapter(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    with pytest.raises(Validacao, match="estorno"):
        await adapter.baixar_titulo(registrado.id_banco)
    assert not any("cancelation" in c for _, c in falso.chamadas)


# --- PDF ----------------------------------------------------------------------


async def test_pdf_publico_sem_token(dormir):
    adapter, falso = _adapter(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert (await adapter.obter_pdf(registrado.id_banco)).startswith(b"%PDF")
    assert falso.chamadas[-1] == (
        "GET",
        f"/v2/charge/{registrado.id_banco}/boleto/{registrado.nosso_numero}",
    )
    assert "authorization" not in falso.cabecalhos[-1]


# --- Pix ----------------------------------------------------------------------


async def test_pix_sem_vencimento_nem_endereco(dormir):
    adapter, falso = _adapter(dormir, meio="PIX")
    titulo = fazer_titulo()
    sem_endereco = titulo.model_copy(
        update={"pagador": titulo.pagador.model_copy(update={"endereco": None})}
    )
    registrado = await adapter.registrar_titulo(sem_endereco)
    cobranca = falso.ultimo_corpo["charge"]
    assert cobranca["source"] == 1
    assert "deadline" not in cobranca and "address" not in cobranca["customer"]
    assert registrado.pix_copia_cola.startswith("000201")
    assert registrado.linha_digitavel is None and registrado.nosso_numero is None
    with pytest.raises(Validacao, match="Pix"):
        await adapter.obter_pdf(registrado.id_banco)
    prorrogacao = Instrucao(
        chave_idempotencia="x",
        tipo=TipoInstrucao.PRORROGACAO,
        nova_data_vencimento=date(2026, 12, 20),
    )
    with pytest.raises(NaoSuportado):
        await adapter.enviar_instrucao(registrado.id_banco, prorrogacao)


async def test_pix_expirado_e_baixado_por_decurso(dormir):
    adapter, falso = _adapter(dormir, meio="PIX")
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.expirar(registrado.id_banco)
    expirado = await adapter.consultar_titulo(registrado.id_banco)
    assert expirado.status is StatusCanonico.BAIXADO
    assert expirado.motivo_baixa is MotivoBaixa.DECURSO_PRAZO


async def test_pix_pago_nao_se_baixa(dormir):
    adapter, falso = _adapter(dormir, meio="PIX")
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    pago = await adapter.consultar_titulo(registrado.id_banco)
    assert pago.canal_liquidacao is CanalLiquidacao.PIX
    with pytest.raises(Validacao, match="estorno"):
        await adapter.baixar_titulo(registrado.id_banco)


# --- webhook ------------------------------------------------------------------


_AUTORIZACAO = safrapay_falso.autorizacao_webhook()


def _parse(adapter, corpo, autorizacao=_AUTORIZACAO):
    cabecalhos = {} if autorizacao is None else {"Authorization": autorizacao}
    return adapter.parse_webhook(corpo, cabecalhos)


def test_webhook_pix_pago(dormir):
    adapter, _ = _adapter(dormir)
    eventos = _parse(adapter, safrapay_falso.webhook(_ID_QUALQUER, valor=2000))
    assert len(eventos) == 1
    evento = eventos[0]
    assert evento.referencia == _ID_QUALQUER
    assert evento.tipo is StatusCanonico.LIQUIDADO
    assert evento.valor_pago == Decimal("20.00")
    assert evento.canal_liquidacao is CanalLiquidacao.PIX
    assert evento.ocorrido_em.isoformat().startswith("2026-12-09T12:00:42")
    assert "PAGADOR EXEMPLO" not in str(evento.resposta_bruta)


def test_webhook_boleto_cancelado_e_vencido(dormir):
    adapter, _ = _adapter(dormir)
    cancelado = _parse(
        adapter, safrapay_falso.webhook(_ID_QUALQUER, "CANCELADO", tipo_pagamento=4)
    )
    assert [e.tipo for e in cancelado] == [StatusCanonico.BAIXADO]
    vencido = _parse(
        adapter, safrapay_falso.webhook(_ID_QUALQUER, "EXPIRADO", tipo_pagamento=4)
    )
    assert [e.tipo for e in vencido] == [StatusCanonico.REGISTRADO]


def test_webhook_aceita_basic_e_recusa_token_errado(dormir):
    adapter, _ = _adapter(dormir)
    corpo = safrapay_falso.webhook(_ID_QUALQUER)
    basico = "Basic " + safrapay_falso.autorizacao_webhook()
    assert _parse(adapter, corpo, basico)
    with pytest.raises(Validacao, match="MerchantToken"):
        _parse(adapter, corpo, safrapay_falso.autorizacao_webhook("mk_outra"))
    with pytest.raises(Validacao, match="MerchantToken"):
        _parse(adapter, corpo, None)
    sem_conferir, _ = _adapter(dormir, conferir_webhook=False)
    assert _parse(sem_conferir, corpo, None)


def test_webhook_de_cartao_ou_status_novo_e_ignorado(dormir):
    adapter, _ = _adapter(dormir)
    cartao = safrapay_falso.webhook(_ID_QUALQUER, tipo_pagamento=2)
    assert _parse(adapter, cartao) == []
    novo = safrapay_falso.webhook(_ID_QUALQUER).replace(
        b'"TransactionStatus": 8', b'"TransactionStatus": 99'
    )
    assert _parse(adapter, novo) == []
    assert _parse(adapter, b'{"ChargeId": null}') == []
    with pytest.raises(Validacao, match="formato"):
        _parse(adapter, b"[]")


def test_tabela_de_status_documentada():
    assert TABELA_STATUS.versao == "2026.11"
    assert TABELA_STATUS.traduzir("PendingPayment") is StatusCanonico.REGISTRADO
    assert TABELA_STATUS.traduzir("8") is StatusCanonico.LIQUIDADO
