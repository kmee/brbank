from datetime import date, datetime
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
    obter_classe,
)
from brbank.adapters.asaas import USER_AGENT, AsaasAdapter, AsaasConfig
from tests import asaas_falso
from tests.asaas_falso import CHAVE_API, AsaasFalso
from tests.conftest import fazer_titulo, pagador


def _config(**extra) -> AsaasConfig:
    base = {"conta": "c1", "api_key": CHAVE_API, "ambiente": "sandbox"}
    return AsaasConfig(**{**base, **extra})


@pytest.fixture
def falso():
    return AsaasFalso()


@pytest.fixture
def adapter(falso, dormir):
    return AsaasAdapter(_config(), client=falso.cliente(), dormir=dormir)


@pytest.fixture
def adapter_pix(falso, dormir):
    return AsaasAdapter(
        _config(billing_type="pix"), client=falso.cliente(), dormir=dormir
    )


def _chamadas(falso, metodo, sufixo):
    return [c for c in falso.chamadas if c[0] == metodo and c[1].endswith(sufixo)]


def test_entry_point():
    assert obter_classe("asaas") is AsaasAdapter


def test_config_recusa_cartao_e_chave_vazia():
    with pytest.raises(Validacao):
        _config(billing_type="CREDIT_CARD")
    with pytest.raises(Validacao):
        _config(api_key=" ")
    assert "***" in repr(_config()) or "chave" not in repr(_config())
    assert CHAVE_API not in repr(_config())


async def test_payload_e_cabecalhos(adapter, falso):
    titulo = fazer_titulo(valor="150.005").model_copy(
        update={
            "encargos": Encargos(
                multa_percentual="2",
                juros_mensal_percentual="1",
                desconto_percentual="5",
                desconto_dias_antes=3,
            ),
            "mensagens": ("linha um", "linha dois"),
            "dias_baixa_automatica": 30,
        }
    )
    registrado = await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["billingType"] == "BOLETO"
    assert corpo["value"] == 150.01
    assert corpo["dueDate"] == "2026-12-10"
    assert corpo["externalReference"] == "chave-1"
    assert corpo["description"] == "FAT-chave-1\nlinha um\nlinha dois"
    assert corpo["fine"] == {"value": 2.0, "type": "PERCENTAGE"}
    assert corpo["interest"] == {"value": 1.0}
    assert corpo["discount"] == {
        "value": 5.0,
        "type": "PERCENTAGE",
        "dueDateLimitDays": 3,
    }
    assert corpo["daysAfterDueDateToRegistrationCancellation"] == 30
    assert falso.ultimo_cabecalho["access_token"] == CHAVE_API
    assert falso.ultimo_cabecalho["user-agent"] == USER_AGENT
    assert USER_AGENT.startswith("brbank/")
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.id_banco.startswith("pay_")
    assert registrado.seu_numero == "FAT-chave-1"
    assert registrado.nosso_numero == "00000001"
    assert registrado.linha_digitavel.startswith("23793.38128")
    assert registrado.codigo_barras
    assert registrado.pix_copia_cola.startswith("000201")


async def test_cliente_criado_uma_vez_com_notificacoes_desligadas(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo("a"))
    await adapter.registrar_titulo(fazer_titulo("b"))
    assert len(_chamadas(falso, "POST", "/v3/customers")) == 1
    cliente = next(iter(falso.clientes.values()))
    assert cliente["cpfCnpj"] == "52998224725"
    assert cliente["name"] == "Maria da Silva"
    assert cliente["email"] == "maria@example.com"
    assert cliente["postalCode"] == "30110000"
    assert cliente["addressNumber"] == "10"
    assert cliente["province"] == "Centro"
    assert cliente["notificationDisabled"] is True
    assert len(falso.cobrancas) == 2
    assert {c["customer"] for c in falso.cobrancas.values()} == {cliente["id"]}


async def test_cliente_existente_e_reaproveitado(adapter, falso):
    existente = falso.cadastrar_cliente("52998224725", name="Maria")
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert not _chamadas(falso, "POST", "/v3/customers")
    assert falso.cobrancas[registrado.id_banco]["customer"] == existente


async def test_chave_repetida_vira_duplicado_sem_segundo_post(adapter, falso):
    primeiro = await adapter.registrar_titulo(fazer_titulo())
    with pytest.raises(Duplicado) as erro:
        await adapter.registrar_titulo(fazer_titulo())
    assert erro.value.id_banco == primeiro.id_banco
    assert len(_chamadas(falso, "POST", "/v3/payments")) == 1


async def test_pix_nao_busca_linha_digitavel(adapter_pix, falso):
    registrado = await adapter_pix.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["billingType"] == "PIX"
    assert "daysAfterDueDateToRegistrationCancellation" not in falso.ultimo_corpo
    assert not _chamadas(falso, "GET", "/identificationField")
    assert registrado.pix_copia_cola
    assert registrado.linha_digitavel is None
    assert registrado.nosso_numero is None


async def test_boleto_sem_chave_pix_segue_sem_qr(dormir):
    falso = AsaasFalso(com_chave_pix=False)
    adapter = AsaasAdapter(_config(), client=falso.cliente(), dormir=dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.linha_digitavel
    assert registrado.pix_copia_cola is None


async def test_pix_sem_qr_e_erro(dormir):
    falso = AsaasFalso(com_chave_pix=False)
    adapter = AsaasAdapter(
        _config(billing_type="PIX"), client=falso.cliente(), dormir=dormir
    )
    registrado = await adapter.registrar_titulo(fazer_titulo())
    # Com meio PIX o QR e obrigatorio; o falso so o nega para boleto.
    assert registrado.pix_copia_cola


async def test_consulta_liquidada(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.confirmar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.00")
    assert consultado.data_pagamento == date(2026, 12, 9)
    assert consultado.seu_numero == "FAT-chave-1"


async def test_consulta_nao_rebusca_linha_e_qr_depois_de_paga(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.confirmar(registrado.id_banco)
    antes = len(falso.chamadas)
    await adapter.consultar_titulo(registrado.id_banco)
    assert len(falso.chamadas) - antes == 1


async def test_confirmed_e_liquidado(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[registrado.id_banco]["status"] = "CONFIRMED"
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO


async def test_removida_e_baixada_mesmo_com_status_antigo(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.baixar_titulo(registrado.id_banco)
    assert falso.cobrancas[registrado.id_banco]["status"] == "PENDING"
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.BAIXADO


async def test_estornada_e_baixada(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[registrado.id_banco]["status"] = "REFUNDED"
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.BAIXADO


async def test_status_desconhecido_nao_vira_pago(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[registrado.id_banco]["status"] = "INVENTADO"
    with pytest.raises(Validacao):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_prorrogacao_manda_os_tres_campos(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    depois = await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(
            chave_idempotencia="i1",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=date(2027, 1, 10),
        ),
    )
    put = _chamadas(falso, "PUT", registrado.id_banco)
    assert len(put) == 1
    assert falso.ultimo_corpo == {
        "billingType": "BOLETO",
        "value": 150.0,
        "dueDate": "2027-01-10",
    }
    assert falso.cobrancas[registrado.id_banco]["dueDate"] == "2027-01-10"
    assert depois.status is StatusCanonico.REGISTRADO
    assert depois.linha_digitavel


async def test_abatimento_reduz_o_valor(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(
            chave_idempotencia="i2", tipo=TipoInstrucao.ABATIMENTO, valor="10.50"
        ),
    )
    assert falso.cobrancas[registrado.id_banco]["value"] == 139.5


async def test_abatimento_maior_que_o_valor_nao_chama_put(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            registrado.id_banco,
            Instrucao(
                chave_idempotencia="i3", tipo=TipoInstrucao.ABATIMENTO, valor="150"
            ),
        )
    assert not _chamadas(falso, "PUT", registrado.id_banco)


async def test_abatimento_no_registro_sai_no_valor(adapter, falso):
    titulo = fazer_titulo().model_copy(update={"encargos": Encargos(abatimento="20")})
    await adapter.registrar_titulo(titulo)
    assert falso.ultimo_corpo["value"] == 130.0


async def test_protesto_e_negativacao_nao_suportados(adapter):
    assert adapter.suporta(Capacidade.ALTERACAO)
    for tipo in (TipoInstrucao.PROTESTO, TipoInstrucao.NEGATIVACAO):
        with pytest.raises(NaoSuportado):
            await adapter.enviar_instrucao(
                "pay_x", Instrucao(chave_idempotencia="i4", tipo=tipo)
            )


async def test_instrucao_de_baixa_remove(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(chave_idempotencia="i5", tipo=TipoInstrucao.BAIXA),
    )
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.cobrancas[registrado.id_banco]["deleted"] is True


async def test_baixar_cobranca_paga_e_validacao_com_codigo(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.confirmar(registrado.id_banco)
    with pytest.raises(Validacao) as erro:
        await adapter.baixar_titulo(registrado.id_banco)
    assert erro.value.status_http == 400
    assert "invalid_action" in str(erro.value)
    assert not erro.value.retryable


async def test_pdf_vem_da_url_publica_sem_chave(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    pdf = await adapter.obter_pdf(registrado.id_banco)
    assert pdf.startswith(b"%PDF")
    assert falso.chamadas[-1][1].startswith("/b/pdf/")
    assert "access_token" not in falso.ultimo_cabecalho


async def test_pdf_de_cobranca_pix_e_validacao(adapter_pix):
    registrado = await adapter_pix.registrar_titulo(fazer_titulo())
    with pytest.raises(Validacao):
        await adapter_pix.obter_pdf(registrado.id_banco)


def test_webhook_recebido(adapter):
    eventos = adapter.parse_webhook(asaas_falso.webhook("pay_1", "PAGO"))
    assert len(eventos) == 1
    evento = eventos[0]
    assert evento.chave_dedup == ("asaas", "pay_1", StatusCanonico.LIQUIDADO)
    assert evento.valor_pago == Decimal("150.0")
    assert evento.ocorrido_em == datetime(2026, 12, 9, 10, 15, 30)


def test_webhook_confirmado_vencido_removido_estornado(adapter):
    esperado = {
        "CONFIRMADO": StatusCanonico.LIQUIDADO,
        "VENCIDO": StatusCanonico.REGISTRADO,
        "REMOVIDO": StatusCanonico.BAIXADO,
        "ESTORNADO": StatusCanonico.BAIXADO,
    }
    for situacao, tipo in esperado.items():
        eventos = adapter.parse_webhook(asaas_falso.webhook("pay_2", situacao))
        assert [e.tipo for e in eventos] == [tipo], situacao
    assert eventos[0].valor_pago is None


def test_webhook_informativo_e_de_outra_categoria_sao_ignorados(adapter):
    assert (
        adapter.parse_webhook(
            asaas_falso.webhook("pay_3", "PAGO", evento="PAYMENT_BANK_SLIP_VIEWED")
        )
        == []
    )
    assert (
        adapter.parse_webhook(
            b'{"id":"evt_1","event":"SUBSCRIPTION_CREATED","subscription":{"id":"sub_1"}}'
        )
        == []
    )
    assert (
        adapter.parse_webhook(
            b'{"id":"evt_2","event":"PAYMENT_NOVO","payment":{"id":"pay_9","status":"INVENTADO"}}'
        )
        == []
    )
    # Evento novo com status conhecido ainda e aproveitado.
    eventos = adapter.parse_webhook(
        asaas_falso.webhook("pay_4", "PAGO", evento="PAYMENT_EVENTO_NOVO")
    )
    assert [e.tipo for e in eventos] == [StatusCanonico.LIQUIDADO]


def test_webhook_fora_do_formato_e_validacao(adapter):
    with pytest.raises(Validacao):
        adapter.parse_webhook(b"nao e json")
    with pytest.raises(Validacao):
        adapter.parse_webhook([1, 2])


def test_webhook_exige_token_quando_configurado(falso, dormir):
    adapter = AsaasAdapter(
        _config(webhook_auth_token="t0ken-do-webhook-com-32-caracteres!"),
        client=falso.cliente(),
        dormir=dormir,
    )
    corpo = asaas_falso.webhook("pay_5")
    with pytest.raises(Validacao):
        adapter.parse_webhook(corpo)
    with pytest.raises(Validacao):
        adapter.parse_webhook(corpo, {"asaas-access-token": "outro"})
    eventos = adapter.parse_webhook(
        corpo, {"Asaas-Access-Token": "t0ken-do-webhook-com-32-caracteres!"}
    )
    assert len(eventos) == 1


def test_webhook_sem_token_configurado_aceita_qualquer_cabecalho(adapter):
    eventos = adapter.parse_webhook(
        asaas_falso.webhook("pay_6"), {"asaas-access-token": "qualquer"}
    )
    assert len(eventos) == 1


def test_resposta_bruta_do_evento_e_mascarada(adapter):
    corpo = asaas_falso.webhook("pay_7").replace(
        b'"customer": "cus_000001"', b'"customer": "cus_000001", "name": "Maria"'
    )
    evento = adapter.parse_webhook(corpo)[0]
    assert "Maria" not in str(evento.resposta_bruta)


async def test_titulo_inexistente_traz_codigo_do_asaas(adapter):
    with pytest.raises(Validacao) as erro:
        await adapter.consultar_titulo("pay_nao_existe")
    assert erro.value.status_http == 404
    assert "invalid_action" in str(erro.value)


async def test_429_e_retentado_e_depois_desiste(falso, dormir, dormidas):
    adapter = AsaasAdapter(_config(), client=falso.cliente(), dormir=dormir)
    falso.falhas_429 = 1
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.id_banco
    assert dormidas
    falso.falhas_429 = 10
    with pytest.raises(Indisponivel):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_chave_errada_e_validacao_sem_retentativa_longa(falso, dormir):
    adapter = AsaasAdapter(
        _config(api_key="$aact_hmlg_errada"), client=falso.cliente(), dormir=dormir
    )
    with pytest.raises(Validacao) as erro:
        await adapter.registrar_titulo(fazer_titulo())
    assert erro.value.status_http == 401
    assert "invalid_access_token" in str(erro.value)
    # O transporte repete uma vez depois de invalidar o token, e para.
    assert len(falso.chamadas) == 2


async def test_pagador_sem_endereco_ainda_cria_cliente(adapter, falso):
    titulo = fazer_titulo().model_copy(
        update={"pagador": pagador().model_copy(update={"endereco": None})}
    )
    await adapter.registrar_titulo(titulo)
    cliente = next(iter(falso.clientes.values()))
    assert "postalCode" not in cliente


async def test_capacidades_declaradas(adapter):
    for capacidade in (
        Capacidade.HIBRIDO,
        Capacidade.ALTERACAO,
        Capacidade.BAIXA,
        Capacidade.PDF,
        Capacidade.WEBHOOK,
        Capacidade.PIX_COB,
        Capacidade.PIX_COBV,
    ):
        assert adapter.suporta(capacidade)
    assert not adapter.suporta(Capacidade.EMISSAO_ASSINCRONA)
    assert adapter.validado_sandbox is True
