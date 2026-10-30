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
)
from brbank.adapters.santander_pagamento import (
    ESTADOS,
    SantanderPagamentoAdapter,
    SantanderPagamentoConfig,
)
from brbank.erros import Indisponivel
from brbank.mascara import MASCARA
from tests.santander_pagamento_falso import SantanderPagamentoFalso
from tests.sicoob_pagamento_falso import brcode

HOJE = date(2026, 10, 30)
DEBITO = ContaBancaria(agencia="1", conta="13037543", digito="1")
CREDITO = ContaBancaria(agencia="637", conta="1039170", digito="7", compe="341")
CNPJ_BENEF = "11222333000181"
BOLETO = "34191790010104351004791020150008291070026000"
GUIA = "84640000000287350024030401503490334270804040"


def config(**extra) -> SantanderPagamentoConfig:
    base = {
        "conta": "c1",
        "client_id": "cliente-xyz",
        "client_secret": "segredo-xyz",
        "workspace_id": "ws-1",
        "pagador_documento": "91.300.766/0001-47",
        "pagador_nome": "KMEE Ltda",
        "ambiente": "sandbox",
    }
    base.update(extra)
    return SantanderPagamentoConfig(**base)


@pytest.fixture
def falso():
    return SantanderPagamentoFalso()


def novo(falso, dormir, **extra):
    return SantanderPagamentoAdapter(
        config(**extra), client=falso.cliente(), dormir=dormir, hoje=lambda: HOJE
    )


@pytest.fixture
def adapter(falso, dormir):
    return novo(falso, dormir)


def boleto(chave="b1", valor="35.45", codigo=BOLETO, **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": TipoPagamento.BOLETO,
        "valor": Decimal(valor),
        "data": HOJE,
        "codigo_barras": codigo,
        "beneficiario_documento": CNPJ_BENEF,
    }
    return Pagamento(**{**campos, **extra})


def guia(chave="g1", **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": TipoPagamento.GUIA,
        "valor": Decimal("28.73"),
        "data": HOJE,
        "codigo_barras": GUIA,
    }
    return Pagamento(**{**campos, **extra})


def pix(chave, destino, **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": TipoPagamento.PIX,
        "valor": Decimal("80.80"),
        "data": HOJE,
        "pix": destino,
    }
    return Pagamento(**{**campos, **extra})


def chave_email():
    return DestinoPix(tipo_chave=TipoChavePix.EMAIL, chave="a@example.com")


def qr(chave="q1", valor="10.00", **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": TipoPagamento.PIX_QRCODE,
        "valor": Decimal(valor),
        "data": HOJE,
        "brcode": brcode(valor),
    }
    return Pagamento(**{**campos, **extra})


def ted(chave="t1", conta=CREDITO, valor="100.99", **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": TipoPagamento.TRANSFERENCIA,
        "valor": Decimal(valor),
        "data": HOJE,
        "conta_credito": conta,
        "favorecido_documento": CNPJ_BENEF,
        "favorecido_nome": "FAVORECIDO TESTE",
    }
    return Pagamento(**{**campos, **extra})


def lote(*pagamentos, **extra):
    return LotePagamento(
        chave_idempotencia="l1",
        tipo=pagamentos[0].tipo,
        conta_debito=DEBITO,
        pagamentos=pagamentos,
        **extra,
    )


# -- contrato e configuracao ---------------------------------------------------


def test_contrato_e_capacidades():
    assert issubclass(SantanderPagamentoAdapter, AdapterPagamento)
    caps = SantanderPagamentoAdapter.capacidades_pagamento
    for cap in (
        "BOLETO",
        "GUIA",
        "PIX",
        "PIX_QRCODE",
        "TRANSFERENCIA",
        "RESERVA_NUMERO",
        "LOTE_LOCAL",
        "LIBERACAO",
    ):
        assert CapacidadePagamento[cap] in caps
    for cap in ("CANCELAMENTO", "WEBHOOK"):
        assert CapacidadePagamento[cap] not in caps
    assert SantanderPagamentoAdapter.validado_sandbox is False


def test_repr_da_config_sem_dado_pessoal_nem_segredo():
    texto = repr(config())
    assert "91300766000147" not in texto
    assert "KMEE" not in texto
    assert "segredo-xyz" not in texto


def test_sem_certificado_e_recusado_em_qualquer_ambiente():
    for ambiente in ("sandbox", "producao"):
        with pytest.raises(Validacao, match="mTLS"):
            SantanderPagamentoAdapter(config(ambiente=ambiente))


@pytest.mark.parametrize(
    ("kw", "erro"),
    [
        ({"ambiente": "homologacao"}, "ambiente"),
        ({"workspace_id": ""}, "workspace"),
        ({"pagador_documento": "123"}, "CPF"),
        ({"codigo_municipio": "35503"}, "IBGE"),
        ({"ted_tipo_destino": "STR0001"}, "ted_tipo_destino"),
    ],
)
def test_config_invalida(kw, erro):
    with pytest.raises(Validacao, match=erro):
        config(**kw)


def test_urls_oficiais():
    producao = config(ambiente="producao")
    assert producao.url_base == (
        "https://trust-open.api.santander.com.br/management_payments_partners/v1"
    )
    assert producao.url_token == (
        "https://trust-open.api.santander.com.br/auth/oauth/v2/token"
    )
    assert config().url_base.startswith("https://trust-sandbox.api.santander.com.br/")


async def test_token_e_cabecalhos(adapter, falso):
    await adapter.pagar_lote(lote(boleto()))
    assert falso.pedido_token == {
        "client_id": "cliente-xyz",
        "client_secret": "segredo-xyz",
        "grant_type": "client_credentials",
    }
    assert falso.cabecalhos[0]["x-application-key"] == "cliente-xyz"
    assert falso.cabecalhos[0]["authorization"] == "Bearer tok"


async def test_reserva_local_e_sem_consulta_de_lote(adapter, falso):
    um = await adapter.reservar_numero_lote()
    assert um != await adapter.reservar_numero_lote()
    with pytest.raises(NaoSuportado, match="LOTE_LOCAL"):
        await adapter.consultar_lote(um, TipoPagamento.BOLETO)
    assert falso.chamadas == []


# -- os dois passos ------------------------------------------------------------


async def test_boleto_post_nao_paga_e_patch_autoriza(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto(), numero="lote-1"))
    assert enviado.id_banco == "lote-1"
    assert enviado.status is StatusLote.PENDENTE_LIBERACAO
    linha = enviado.pagamentos[0]
    assert linha.status is StatusPagamento.ACEITO
    assert linha.valor == Decimal("35.45")
    post = falso.de("POST")[0]
    assert post["code"] == BOLETO
    assert post["paymentDate"] == "2026-10-30"
    assert post["tags"] == ["b1"]
    assert falso.de("PATCH") == []

    pago = await adapter.autorizar_lote(enviado, DEBITO)
    patch = falso.de("PATCH")[0]
    assert patch == {
        "paymentValue": "35.45",
        "debitAccount": {"branch": "0001", "number": "000130375431"},
        "finalPayer": {
            "name": "KMEE Ltda",
            "documentType": "CNPJ",
            "documentNumber": "91300766000147",
        },
        "status": "AUTHORIZED",
    }
    final = pago.pagamentos[0]
    assert final.status is StatusPagamento.PAGO
    assert final.chave_idempotencia == "b1"
    assert final.autenticacao == "VX00012209131051260932"
    assert final.valor == Decimal("35.45")
    assert final.data == HOJE
    assert pago.status is StatusLote.PROCESSADO


async def test_guia_futura_agenda_e_mantem_a_data(adapter, falso):
    falso.estado_patch = "SCHEDULED"
    futuro = HOJE + timedelta(days=5)
    enviado = await adapter.pagar_lote(lote(guia(data=futuro)))
    assert falso.de("POST")[0]["paymentDate"] == futuro.isoformat()
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    assert pago.pagamentos[0].status is StatusPagamento.AGENDADO
    assert "finalPayer" in falso.de("PATCH")[0]


@pytest.mark.parametrize(
    "fabrica",
    [
        lambda d: boleto(data=d),
        lambda d: pix("p1", chave_email(), data=d),
        lambda d: qr(data=d),
        lambda d: ted(data=d),
    ],
)
async def test_data_futura_fora_da_guia_nao_vai_ao_banco(adapter, falso, fabrica):
    enviado = await adapter.pagar_lote(lote(fabrica(HOJE + timedelta(days=1))))
    linha = enviado.pagamentos[0]
    assert linha.status is StatusPagamento.RECUSADO
    assert "agendamento" in linha.motivos[0]
    assert falso.chamadas == []


async def test_data_passada_nao_vai_ao_banco(adapter, falso):
    enviado = await adapter.pagar_lote(lote(guia(data=HOJE - timedelta(days=1))))
    assert enviado.pagamentos[0].status is StatusPagamento.RECUSADO
    assert falso.chamadas == []


async def test_pix_por_chave_monta_corpo(adapter, falso):
    p = pix("p1", chave_email(), descricao="NF 123")
    enviado = await adapter.pagar_lote(lote(p))
    post = falso.de("POST")[0]
    assert post["dictCodeType"] == "EMAIL"
    assert post["dictCode"] == "a@example.com"
    assert post["paymentValue"] == "80.80"
    assert post["remittanceInformation"] == "NF 123"
    assert "paymentDate" not in post
    await adapter.autorizar_lote(enviado, DEBITO)
    assert falso.de("PATCH")[0] == {
        "paymentValue": "80.80",
        "debitAccount": {"branch": "0001", "number": "000130375431"},
        "status": "AUTHORIZED",
    }


@pytest.mark.parametrize(
    ("tipo", "chave", "tipo_api", "chave_api"),
    [
        (TipoChavePix.DOCUMENTO, "529.982.247-25", "CPF", "52998224725"),
        (TipoChavePix.DOCUMENTO, CNPJ_BENEF, "CNPJ", CNPJ_BENEF),
        (TipoChavePix.TELEFONE, "(11) 91234-5678", "CELULAR", "+5511912345678"),
        (TipoChavePix.ALEATORIA, "abc-123", "EVP", "abc-123"),
    ],
)
async def test_tipos_de_chave(adapter, falso, tipo, chave, tipo_api, chave_api):
    p = pix("p1", DestinoPix(tipo_chave=tipo, chave=chave))
    await adapter.pagar_lote(lote(p))
    post = falso.de("POST")[0]
    assert (post["dictCodeType"], post["dictCode"]) == (tipo_api, chave_api)


async def test_pix_por_dados_bancarios_com_ispb(adapter, falso):
    destino = DestinoPix(
        conta=ContaBancaria(agencia="1", conta="4321", digito="9", ispb="60701190"),
        documento=CNPJ_BENEF,
        nome="FAVORECIDO",
    )
    await adapter.pagar_lote(lote(pix("p1", destino)))
    beneficiario = falso.de("POST")[0]["beneficiary"]
    assert beneficiario == {
        "branch": "0001",
        "number": "43219",
        "type": "CONTA_CORRENTE",
        "documentType": "CNPJ",
        "documentNumber": CNPJ_BENEF,
        "name": "FAVORECIDO",
        "ispb": "60701190",
    }


async def test_pix_por_dados_bancarios_com_compe_e_poupanca(adapter, falso):
    destino = DestinoPix(
        conta=ContaBancaria(
            agencia="1", conta="4321", digito="9", compe="341", poupanca=True
        ),
        documento=CNPJ_BENEF,
        nome="FAVORECIDO",
    )
    await adapter.pagar_lote(lote(pix("p1", destino)))
    beneficiario = falso.de("POST")[0]["beneficiary"]
    assert beneficiario["bankCode"] == "341"
    assert "ispb" not in beneficiario
    assert beneficiario["type"] == "CONTA_POUPANCA"


@pytest.mark.parametrize(
    ("kw", "erro"), [({"nome": None}, "nome"), ({"documento": None}, "documento")]
)
async def test_pix_por_dados_exige_nome_e_documento(adapter, falso, kw, erro):
    campos = {
        "conta": ContaBancaria(agencia="1", conta="1", digito="9", compe="341"),
        "documento": CNPJ_BENEF,
        "nome": "F",
    }
    enviado = await adapter.pagar_lote(lote(pix("p1", DestinoPix(**{**campos, **kw}))))
    assert erro in enviado.pagamentos[0].motivos[0]
    assert falso.chamadas == []


async def test_qrcode_monta_corpo_com_municipio(falso, dormir):
    adapter = novo(falso, dormir, codigo_municipio="3550308")
    p = qr()
    enviado = await adapter.pagar_lote(lote(p))
    post = falso.de("POST")[0]
    assert post["qrCode"] == p.brcode
    assert post["paymentValue"] == "10.00"
    assert post["paymentDate"] == "2026-10-30"
    assert post["ibgeTownCode"] == 3550308
    assert enviado.pagamentos[0].status is StatusPagamento.ACEITO


async def test_qrcode_com_encargos_autoriza_o_total_do_banco(adapter, falso):
    falso.total = "10.50"
    enviado = await adapter.pagar_lote(lote(qr(pagar_com_juros_multa=True)))
    assert enviado.pagamentos[0].valor == Decimal("10.50")
    await adapter.autorizar_lote(enviado, DEBITO)
    assert falso.de("PATCH")[0]["paymentValue"] == "10.50"


async def test_qrcode_com_encargos_sem_permissao_e_recusado(adapter, falso):
    falso.total = "10.50"
    enviado = await adapter.pagar_lote(lote(qr()))
    linha = enviado.pagamentos[0]
    assert linha.status is StatusPagamento.RECUSADO
    assert linha.motivos == ("VALOR_DIVERGENTE",)
    assert linha.id_banco
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    assert falso.de("PATCH") == []
    assert pago.pagamentos[0].status is StatusPagamento.RECUSADO


async def test_ted_monta_corpo_e_autoriza(adapter, falso):
    falso.estado_patch = "PENDING_CONFIRMATION"
    enviado = await adapter.pagar_lote(lote(ted()))
    post = falso.de("POST")[0]
    assert post["sourceAccount"] == {
        "branchCode": "0001",
        "accountNumber": "000130375431",
    }
    assert post["destinationAccount"] == {
        "bankCode": "341",
        "branchCode": "637",
        "accountNumber": "10391707",
        "typeAccount": "CC",
        "legalEntityIdentifier": "CNPJ",
        "documentIdentifierNumber": CNPJ_BENEF,
        "name": "FAVORECIDO TESTE",
        "purpose": "PAGAMENTO_DE_FORNECEDORES",
    }
    assert post["transferValue"] == 100.99
    assert post["destinationType"] == "STR0008"
    assert enviado.pagamentos[0].valor == Decimal("100.99")
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    assert falso.de("PATCH")[0] == {
        "sourceAccount": {"branchCode": "0001", "accountNumber": "000130375431"},
        "status": "AUTHORIZED",
    }
    assert pago.pagamentos[0].status is StatusPagamento.EM_PROCESSAMENTO


async def test_ted_acima_de_dez_milhoes_vai_como_texto(adapter, falso):
    await adapter.pagar_lote(lote(ted(valor="10000000.01")))
    assert falso.de("POST")[0]["transferValue"] == "10000000.01"


async def test_ted_conta_de_pagamento_leva_ispb(adapter, falso):
    conta = ContaBancaria(
        agencia="637", conta="1", digito="7", compe="341", ispb="60701190"
    )
    await adapter.pagar_lote(lote(ted(conta=conta)))
    destino = falso.de("POST")[0]["destinationAccount"]
    assert destino["typeAccount"] == "PG"
    assert destino["ispbCode"] == "60701190"


async def test_ted_sem_nome_nem_compe_nao_vai_ao_banco(falso, dormir):
    adapter = novo(falso, dormir)
    enviado = await adapter.pagar_lote(lote(ted(favorecido_nome=None)))
    assert "nome do favorecido" in enviado.pagamentos[0].motivos[0]
    adapter = novo(falso, dormir)
    sem_compe = ContaBancaria(agencia="1", conta="1", digito="1", ispb="60701190")
    enviado = await adapter.pagar_lote(lote(ted(conta=sem_compe)))
    assert "COMPE" in enviado.pagamentos[0].motivos[0]
    assert falso.chamadas == []


async def test_conta_de_debito_invalida_nao_vai_ao_banco(adapter, falso):
    grande = ContaBancaria(agencia="1", conta="1234567890123", digito="1")
    enviado = await adapter.pagar_lote(lote(boleto()))
    pago = await adapter.autorizar_lote(enviado, grande)
    # Erro local na montagem: a linha nao muda, e nada foi enviado.
    assert pago.pagamentos[0].status is StatusPagamento.ACEITO
    assert "12 digitos" in pago.pagamentos[0].motivos[0]
    assert falso.de("PATCH") == []


# -- idempotencia ---------------------------------------------------------------


async def test_id_e_estavel_e_tem_formato_v4(adapter, falso):
    await adapter.pagar_lote(lote(boleto()))
    primeiro = falso.de("POST")[0]["id"]
    assert primeiro[14] == "4"
    assert adapter._id_pagamento("b1") == primeiro
    assert adapter._id_pagamento("b2") != primeiro


async def test_mesma_chave_em_outro_workspace_tem_outro_id(falso, dormir):
    assert novo(falso, dormir)._id_pagamento("b1") != novo(
        falso, dormir, workspace_id="ws-2"
    )._id_pagamento("b1")


async def test_chave_longa_nao_vira_tag(adapter, falso):
    await adapter.pagar_lote(lote(boleto(chave="x" * 21)))
    assert "tags" not in falso.de("POST")[0]


async def test_reenvio_acha_o_pagamento_em_vez_de_criar_outro(adapter, falso):
    await adapter.pagar_lote(lote(boleto()))
    assert len(falso.pagamentos) == 1
    denovo = await adapter.pagar_lote(lote(boleto()))
    assert len(falso.pagamentos) == 1
    assert denovo.pagamentos[0].status is StatusPagamento.ACEITO
    assert denovo.pagamentos[0].valor == Decimal("35.45")


async def test_reenvio_de_pagamento_ja_pago_devolve_pago(adapter, falso):
    enviado = await adapter.pagar_lote(lote(pix("p1", chave_email())))
    await adapter.autorizar_lote(enviado, DEBITO)
    denovo = await adapter.pagar_lote(lote(pix("p1", chave_email())))
    assert denovo.pagamentos[0].status is StatusPagamento.PAGO
    assert falso.pagamentos[denovo.pagamentos[0].id_banco]["status"] == "PAYED"


async def test_ja_registrado_com_outro_id_vira_recusado(adapter, falso):
    falso.falhas["POST bank_slip_payments"] = [
        {"codigo": "30088", "mensagem": "Transaction already registered"}
    ]
    enviado = await adapter.pagar_lote(lote(boleto()))
    linha = enviado.pagamentos[0]
    assert linha.status is StatusPagamento.RECUSADO
    assert linha.motivos == ("30088: Transaction already registered",)


# -- POST: recusa e incerteza ----------------------------------------------------


async def test_post_com_erro_recusa_a_linha_e_segue(adapter, falso):
    falso.falhas["POST bank_slip_payments"] = [
        {"codigo": "30042", "mensagem": "Barcode with invalid content"}
    ]
    enviado = await adapter.pagar_lote(lote(boleto("a"), boleto("b")))
    assert [p.status for p in enviado.pagamentos] == [
        StatusPagamento.RECUSADO,
        StatusPagamento.ACEITO,
    ]
    assert enviado.pagamentos[0].motivos == ("30042: Barcode with invalid content",)
    assert enviado.status is StatusLote.ACEITO_PARCIAL


async def test_post_rejected_com_201_e_recusado(adapter, falso):
    falso.estado_post = "REJECTED"
    enviado = await adapter.pagar_lote(lote(boleto()))
    linha = enviado.pagamentos[0]
    assert linha.status is StatusPagamento.RECUSADO
    assert linha.motivos == ("30105", "Saldo")
    assert enviado.status is StatusLote.RECUSADO


@pytest.mark.parametrize("falha", ["timeout", 503, "html", "vazio"])
async def test_post_sem_resposta_legivel_e_indeterminado_e_para(adapter, falso, falha):
    falso.falhas["POST bank_slip_payments"] = [falha]
    enviado = await adapter.pagar_lote(lote(boleto("a"), boleto("b")))
    primeira, segunda = enviado.pagamentos
    assert primeira.status is StatusPagamento.INDETERMINADO
    assert primeira.id_banco == adapter._id_pagamento("a")
    assert primeira.motivos == ("SEM_RESPOSTA",)
    assert segunda.status is StatusPagamento.RECUSADO
    assert segunda.motivos == ("NAO_ENVIADO",)
    assert len(falso.de("POST")) == 1


async def test_post_com_429_nao_foi_processado(adapter, falso):
    falso.falhas["POST bank_slip_payments"] = [429]
    enviado = await adapter.pagar_lote(lote(boleto("a"), boleto("b")))
    assert enviado.pagamentos[0].status is StatusPagamento.RECUSADO
    assert enviado.pagamentos[0].motivos[0] == "NAO_ENVIADO"
    assert enviado.pagamentos[1].motivos == ("NAO_ENVIADO",)


async def test_post_com_estado_desconhecido_e_indeterminado(adapter, falso):
    falso.estado_post = "NOVIDADE"
    enviado = await adapter.pagar_lote(lote(boleto()))
    assert enviado.pagamentos[0].status is StatusPagamento.INDETERMINADO


async def test_documento_divergente_nao_e_autorizado(adapter, falso):
    falso.beneficiario = {**falso.beneficiario, "documentNumber": "99999999000199"}
    enviado = await adapter.pagar_lote(lote(boleto()))
    linha = enviado.pagamentos[0]
    assert linha.status is StatusPagamento.RECUSADO
    assert linha.motivos == ("DOCUMENTO_DIVERGENTE",)
    await adapter.autorizar_lote(enviado, DEBITO)
    assert falso.de("PATCH") == []


async def test_documento_com_zeros_a_esquerda_confere(adapter, falso):
    # O exemplo do OpenAPI traz CPF completado ate 14 ("00000261918141").
    falso.beneficiario = {"documentNumber": "00000261918141"}
    p = boleto(beneficiario_documento="26191814100")
    assert (await adapter.pagar_lote(lote(p))).pagamentos[0].status is (
        StatusPagamento.RECUSADO
    )
    p = boleto("b2", beneficiario_documento="00261918141")
    assert (await adapter.pagar_lote(lote(p))).pagamentos[0].status is (
        StatusPagamento.ACEITO
    )


async def test_estado_em_validacao_nao_e_autorizado(adapter, falso):
    falso.estado_post = "PENDING_VALIDATION"
    enviado = await adapter.pagar_lote(lote(boleto()))
    assert enviado.pagamentos[0].status is StatusPagamento.EM_PROCESSAMENTO
    await adapter.autorizar_lote(enviado, DEBITO)
    assert falso.de("PATCH") == []


# -- PATCH: dinheiro -------------------------------------------------------------


@pytest.mark.parametrize("falha", ["timeout", 500, 503, "html", "vazio"])
async def test_patch_sem_resposta_legivel_e_indeterminado_sem_retry(
    adapter, falso, falha
):
    enviado = await adapter.pagar_lote(lote(boleto("a"), boleto("b")))
    falso.falhas["PATCH bank_slip_payments"] = [falha]
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    primeira, segunda = pago.pagamentos
    assert primeira.status is StatusPagamento.INDETERMINADO
    assert primeira.id_banco == adapter._id_pagamento("a")
    assert primeira.motivos == ("SEM_RESPOSTA",)
    assert segunda.status is StatusPagamento.ACEITO
    assert segunda.motivos == ("NAO_ENVIADO",)
    assert len(falso.de("PATCH")) == 1
    assert pago.status is StatusLote.EM_PROCESSAMENTO


async def test_patch_com_429_deixa_a_linha_aceita(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto()))
    falso.falhas["PATCH bank_slip_payments"] = [429]
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    linha = pago.pagamentos[0]
    assert linha.status is StatusPagamento.ACEITO
    assert linha.motivos[0] == "NAO_ENVIADO"


async def test_patch_recusado_com_saldo_insuficiente_segue_aceito(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto("a"), boleto("b")))
    falso.falhas["PATCH bank_slip_payments"] = [
        {"codigo": "30105", "mensagem": "Insufficient balance"}
    ]
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    primeira, segunda = pago.pagamentos
    assert primeira.status is StatusPagamento.ACEITO
    assert primeira.motivos == ("30105: Insufficient balance",)
    assert primeira.valor == Decimal("35.45")
    assert segunda.status is StatusPagamento.PAGO


async def test_autorizar_de_novo_um_lote_velho_acha_o_pagamento_pago(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto()))
    await adapter.autorizar_lote(enviado, DEBITO)
    denovo = await adapter.autorizar_lote(enviado, DEBITO)
    assert denovo.pagamentos[0].status is StatusPagamento.PAGO
    assert denovo.pagamentos[0].motivos == ("40046: Pix is not ready to pay.",)
    assert falso.de("PATCH")[0] == falso.de("PATCH")[1]


async def test_patch_recusado_e_consulta_fora_mantem_a_linha(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto()))
    falso.falhas["PATCH bank_slip_payments"] = [
        {"codigo": "30105", "mensagem": "Insufficient balance"}
    ]
    falso.falhas["GET bank_slip_payments"] = [503, 503, 503, 503]
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    assert pago.pagamentos[0].status is StatusPagamento.ACEITO
    assert pago.pagamentos[0].motivos == ("30105: Insufficient balance",)


async def test_erro_de_montagem_nao_perde_o_que_ja_foi_pago(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto("a"), boleto("b")))
    primeira, segunda = enviado.pagamentos
    sem_valor = enviado.model_copy(
        update={"pagamentos": (primeira, segunda.model_copy(update={"valor": None}))}
    )
    pago = await adapter.autorizar_lote(sem_valor, DEBITO)
    assert pago.pagamentos[0].status is StatusPagamento.PAGO
    assert pago.pagamentos[1].status is StatusPagamento.ACEITO


async def test_autorizar_lote_tira_o_lote_da_memoria_de_liberar(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto()))
    await adapter.autorizar_lote(enviado, DEBITO)
    with pytest.raises(Validacao, match="autorizar_lote"):
        await adapter.liberar_lote(enviado.id_banco)
    assert len(falso.de("PATCH")) == 1


async def test_qrcode_mantem_o_total_quando_o_patch_e_recusado(adapter, falso):
    falso.total = "10.50"
    enviado = await adapter.pagar_lote(lote(qr(pagar_com_juros_multa=True)))
    falso.falhas["PATCH pix_payments"] = [
        {"codigo": "40032", "mensagem": "Daily limit"}
    ]
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    assert pago.pagamentos[0].status is StatusPagamento.ACEITO
    assert pago.pagamentos[0].valor == Decimal("10.50")


async def test_post_com_valor_ilegivel_e_indeterminado(adapter, falso):
    falso.total = "abc"
    enviado = await adapter.pagar_lote(lote(qr()))
    assert enviado.pagamentos[0].status is StatusPagamento.INDETERMINADO


async def test_patch_rejected_com_200_e_rejeitado(adapter, falso):
    falso.estado_patch = "REJECTED"
    enviado = await adapter.pagar_lote(lote(boleto()))
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    linha = pago.pagamentos[0]
    assert linha.status is StatusPagamento.REJEITADO
    assert linha.motivos == ("30105", "Saldo insuficiente")
    assert pago.status is StatusLote.PROCESSADO


@pytest.mark.parametrize("estado", ["NOVIDADE", "READY_TO_PAY"])
async def test_patch_com_estado_que_nao_diz_nada_e_indeterminado(
    adapter, falso, estado
):
    enviado = await adapter.pagar_lote(lote(boleto()))
    falso.estado_patch = estado
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    assert pago.pagamentos[0].status is StatusPagamento.INDETERMINADO


async def test_autorizar_nao_toca_linha_que_nao_esta_aceita(adapter, falso):
    falso.falhas["POST bank_slip_payments"] = [
        {"codigo": "30042", "mensagem": "Barcode with invalid content"}
    ]
    enviado = await adapter.pagar_lote(lote(boleto("a"), boleto("b")))
    pago = await adapter.autorizar_lote(enviado, DEBITO)
    assert len(falso.de("PATCH")) == 1
    assert pago.pagamentos[0].status is StatusPagamento.RECUSADO
    assert pago.pagamentos[1].status is StatusPagamento.PAGO


async def test_linha_sem_valor_nao_e_autorizada(adapter):
    enviado = await adapter.pagar_lote(lote(boleto()))
    sem_valor = enviado.model_copy(
        update={
            "pagamentos": (enviado.pagamentos[0].model_copy(update={"valor": None}),)
        }
    )
    pago = await adapter.autorizar_lote(sem_valor, DEBITO)
    assert pago.pagamentos[0].status is StatusPagamento.ACEITO
    assert "sem valor" in pago.pagamentos[0].motivos[0]


# -- liberar_lote ----------------------------------------------------------------


async def test_liberar_lote_na_mesma_instancia(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto()))
    assert await adapter.liberar_lote(enviado.id_banco) is None
    assert len(falso.de("PATCH")) == 1
    # O lote sai da memoria: liberar duas vezes nao chega ao banco.
    with pytest.raises(Validacao, match="autorizar_lote"):
        await adapter.liberar_lote(enviado.id_banco)
    assert len(falso.de("PATCH")) == 1


async def test_liberar_lote_de_outra_instancia_e_recusado(adapter, falso, dormir):
    enviado = await adapter.pagar_lote(lote(boleto()))
    with pytest.raises(Validacao, match="autorizar_lote"):
        await novo(falso, dormir).liberar_lote(enviado.id_banco)
    assert falso.de("PATCH") == []


async def test_liberar_lote_sem_resposta_levanta_e_nao_repete(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto()))
    falso.falhas["PATCH bank_slip_payments"] = ["timeout"]
    with pytest.raises(Indisponivel) as exc:
        await adapter.liberar_lote(enviado.id_banco)
    assert exc.value.retryable is False
    assert len(falso.de("PATCH")) == 1


# -- consulta ---------------------------------------------------------------------


async def test_consulta_le_o_estado_e_a_autenticacao(adapter, falso):
    enviado = await adapter.pagar_lote(lote(pix("p1", chave_email())))
    await adapter.autorizar_lote(enviado, DEBITO)
    lido = await adapter.consultar_pagamento(
        enviado.pagamentos[0].id_banco, TipoPagamento.PIX
    )
    assert lido.status is StatusPagamento.PAGO
    assert lido.autenticacao == "VX00012209131051260932"
    assert lido.valor == Decimal("80.80")
    assert lido.data == HOJE


async def test_consulta_repete_em_falha_de_leitura(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto()))
    falso.falhas["GET bank_slip_payments"] = [503]
    lido = await adapter.consultar_pagamento(
        enviado.pagamentos[0].id_banco, TipoPagamento.BOLETO
    )
    assert lido.status is StatusPagamento.ACEITO


@pytest.mark.parametrize("estado", list(ESTADOS))
async def test_todos_os_estados_mapeados(adapter, falso, estado):
    enviado = await adapter.pagar_lote(lote(boleto()))
    id_banco = enviado.pagamentos[0].id_banco
    falso.pagamentos[id_banco]["status"] = estado
    lido = await adapter.consultar_pagamento(id_banco, TipoPagamento.BOLETO)
    assert lido.status is ESTADOS[estado]


async def test_refunded_vira_devolvido_com_o_detalhe(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto()))
    id_banco = enviado.pagamentos[0].id_banco
    falso.pagamentos[id_banco].update(
        status="REFUNDED",
        statusDetail={"code": "REFUNDED", "detail": "Banco de destino recusou"},
    )
    lido = await adapter.consultar_pagamento(id_banco, TipoPagamento.BOLETO)
    assert lido.status is StatusPagamento.DEVOLVIDO
    assert lido.motivos == ("Banco de destino recusou",)


async def test_consulta_com_estado_desconhecido_levanta(adapter, falso):
    enviado = await adapter.pagar_lote(lote(boleto()))
    id_banco = enviado.pagamentos[0].id_banco
    falso.pagamentos[id_banco]["status"] = "NOVIDADE"
    with pytest.raises(Validacao, match="nao mapeado"):
        await adapter.consultar_pagamento(id_banco, TipoPagamento.BOLETO)


async def test_consulta_sem_id_e_nao_declarado(adapter, falso):
    with pytest.raises(Validacao, match="id do pagamento"):
        await adapter.consultar_pagamento("", TipoPagamento.BOLETO)
    assert not adapter.suporta_pagamento(CapacidadePagamento.CANCELAMENTO)
    with pytest.raises(NaoSuportado):
        await adapter.cancelar_pagamentos(DEBITO, ("x",))
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook_pagamento({})


async def test_consulta_devolve_os_tipos_de_rota(adapter, falso):
    enviado = await adapter.pagar_lote(lote(ted()))
    await adapter.consultar_pagamento(
        enviado.pagamentos[0].id_banco, TipoPagamento.TRANSFERENCIA
    )
    assert any("/transfer/" in caminho for _, caminho in falso.chamadas)


# -- PII ---------------------------------------------------------------------------


async def test_resposta_bruta_sem_dado_pessoal(adapter, falso):
    falso.pagamentos.clear()
    enviado = await adapter.pagar_lote(lote(boleto()))
    id_banco = enviado.pagamentos[0].id_banco
    falso.pagamentos[id_banco]["finalPayer"] = {"documentNumber": "91300766000147"}
    lido = await adapter.consultar_pagamento(id_banco, TipoPagamento.BOLETO)
    bruta = lido.resposta_bruta
    assert bruta["payer"] == MASCARA
    assert bruta["beneficiary"] == MASCARA
    assert bruta["finalPayer"] == MASCARA
    assert bruta["debitAccount"] == MASCARA
    assert bruta["code"] == MASCARA
    for texto in ("91300766000147", CNPJ_BENEF, BOLETO, "130375431"):
        assert texto not in str(bruta)
        assert texto not in repr(lido)
    # A autenticacao em `transaction.code` nao e o codigo de barras: fica.
    assert bruta["transaction"] == {"value": None, "code": None, "date": None}


async def test_resposta_bruta_de_ted_e_pix_sem_dado_pessoal(adapter, falso):
    enviado = await adapter.pagar_lote(lote(ted()))
    lido = await adapter.consultar_pagamento(
        enviado.pagamentos[0].id_banco, TipoPagamento.TRANSFERENCIA
    )
    assert lido.resposta_bruta["destinationAccount"] == MASCARA
    assert CNPJ_BENEF not in str(lido.resposta_bruta)
    enviado = await adapter.pagar_lote(lote(pix("p1", chave_email())))
    lido = await adapter.consultar_pagamento(
        enviado.pagamentos[0].id_banco, TipoPagamento.PIX
    )
    assert "a@example.com" not in str(lido.resposta_bruta)
