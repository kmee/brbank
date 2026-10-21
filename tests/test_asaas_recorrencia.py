from datetime import date, datetime
from decimal import Decimal

import pytest

from brbank import (
    AdapterRecorrencia,
    AlteracaoAssinatura,
    Assinatura,
    Capacidade,
    Ciclo,
    Duplicado,
    Encargos,
    MeioCobranca,
    NaoSuportado,
    StatusAssinatura,
    StatusCanonico,
    StatusCheckout,
    TipoEventoRecorrencia,
    Validacao,
)
from brbank.adapters.asaas import IMAGEM_PADRAO_BASE64, AsaasAdapter, AsaasConfig
from tests import asaas_falso
from tests.asaas_falso import CHAVE_API, AsaasFalso
from tests.conftest import pagador


def fazer_assinatura(chave: str = "contrato-1", **extra) -> Assinatura:
    base = {
        "chave_idempotencia": chave,
        "seu_numero": f"CT-{chave}",
        "pagador": pagador(),
        "valor": "99.90",
        "ciclo": Ciclo.MENSAL,
        "primeiro_vencimento": date(2027, 1, 10),
        "meio": MeioCobranca.BOLETO,
    }
    return Assinatura(**{**base, **extra})


@pytest.fixture
def falso():
    return AsaasFalso()


@pytest.fixture
def adapter(falso, dormir):
    config = AsaasConfig(conta="c1", api_key=CHAVE_API, ambiente="sandbox")
    return AsaasAdapter(config, client=falso.cliente(), dormir=dormir)


def _chamadas(falso, metodo, sufixo):
    return [c for c in falso.chamadas if c[0] == metodo and c[1].endswith(sufixo)]


def test_protocolo_e_capacidades(adapter):
    assert isinstance(adapter, AdapterRecorrencia)
    assert adapter.suporta(Capacidade.RECORRENCIA)
    assert adapter.suporta(Capacidade.CHECKOUT)


async def test_criar_assinatura_payload(adapter, falso):
    assinatura = fazer_assinatura(
        data_fim=date(2027, 12, 31),
        maximo_cobrancas=12,
        encargos=Encargos(multa_percentual="2", juros_mensal_percentual="1"),
        mensagens=("Plano Pro",),
    )
    registrada = await adapter.criar_assinatura(assinatura)
    corpo = falso.ultimo_corpo
    assert corpo["billingType"] == "BOLETO"
    assert corpo["cycle"] == "MONTHLY"
    assert corpo["value"] == 99.9
    assert corpo["nextDueDate"] == "2027-01-10"
    assert corpo["endDate"] == "2027-12-31"
    assert corpo["maxPayments"] == 12
    assert corpo["externalReference"] == "contrato-1"
    assert corpo["description"] == "CT-contrato-1\nPlano Pro"
    assert corpo["fine"] == {"value": 2.0, "type": "PERCENTAGE"}
    assert corpo["interest"] == {"value": 1.0}
    assert corpo["customer"] in falso.clientes
    assert registrada.id_banco.startswith("sub_")
    assert registrada.status is StatusAssinatura.ATIVA
    assert registrada.seu_numero == "CT-contrato-1"
    assert registrada.valor == Decimal("99.9")
    assert registrada.ciclo is Ciclo.MENSAL
    assert registrada.meio is MeioCobranca.BOLETO
    assert registrada.proximo_vencimento == date(2027, 1, 10)
    assert registrada.data_fim == date(2027, 12, 31)
    assert registrada.id_checkout is None


@pytest.mark.parametrize(
    ("ciclo", "esperado"),
    [
        (Ciclo.SEMANAL, "WEEKLY"),
        (Ciclo.QUINZENAL, "BIWEEKLY"),
        (Ciclo.BIMESTRAL, "BIMONTHLY"),
        (Ciclo.TRIMESTRAL, "QUARTERLY"),
        (Ciclo.SEMESTRAL, "SEMIANNUALLY"),
        (Ciclo.ANUAL, "YEARLY"),
    ],
)
async def test_mapa_de_ciclos(adapter, falso, ciclo, esperado):
    registrada = await adapter.criar_assinatura(fazer_assinatura(ciclo=ciclo))
    assert falso.ultimo_corpo["cycle"] == esperado
    assert registrada.ciclo is ciclo


async def test_meio_indefinido_e_pix(adapter, falso):
    await adapter.criar_assinatura(fazer_assinatura("a", meio=MeioCobranca.INDEFINIDO))
    assert falso.ultimo_corpo["billingType"] == "UNDEFINED"
    await adapter.criar_assinatura(fazer_assinatura("b", meio=MeioCobranca.PIX))
    assert falso.ultimo_corpo["billingType"] == "PIX"


async def test_cartao_so_pelo_checkout(adapter, falso):
    with pytest.raises(NaoSuportado):
        await adapter.criar_assinatura(fazer_assinatura(meio=MeioCobranca.CARTAO))
    assert not falso.chamadas


async def test_chave_repetida_vira_duplicado(adapter, falso):
    primeira = await adapter.criar_assinatura(fazer_assinatura())
    with pytest.raises(Duplicado) as erro:
        await adapter.criar_assinatura(fazer_assinatura())
    assert erro.value.id_banco == primeira.id_banco
    assert len(_chamadas(falso, "POST", "/v3/subscriptions")) == 1


async def test_consultar_status(adapter, falso):
    registrada = await adapter.criar_assinatura(fazer_assinatura())
    falso.assinaturas[registrada.id_banco]["status"] = "EXPIRED"
    assert (
        await adapter.consultar_assinatura(registrada.id_banco)
    ).status is StatusAssinatura.ENCERRADA
    falso.assinaturas[registrada.id_banco]["status"] = "INVENTADO"
    with pytest.raises(Validacao):
        await adapter.consultar_assinatura(registrada.id_banco)
    with pytest.raises(Validacao) as erro:
        await adapter.consultar_assinatura("sub_nao_existe")
    assert erro.value.status_http == 404


async def test_alterar_envia_so_o_que_mudou_e_reconsulta(adapter, falso):
    registrada = await adapter.criar_assinatura(fazer_assinatura())
    depois = await adapter.alterar_assinatura(
        registrada.id_banco,
        AlteracaoAssinatura(
            valor="120", ciclo=Ciclo.TRIMESTRAL, data_fim=date(2028, 1, 1)
        ),
    )
    assert falso.ultimo_corpo == {
        "value": 120.0,
        "cycle": "QUARTERLY",
        "endDate": "2028-01-01",
    }
    assert depois.valor == Decimal("120")
    assert depois.ciclo is Ciclo.TRIMESTRAL
    assert depois.data_fim == date(2028, 1, 1)


async def test_alterar_nas_pendentes_e_mensagens(adapter, falso):
    registrada = await adapter.criar_assinatura(fazer_assinatura())
    cobrancas = falso.gerar_ciclos(registrada.id_banco, 2)
    await adapter.alterar_assinatura(
        registrada.id_banco,
        AlteracaoAssinatura(
            valor="150", mensagens=("Reajuste anual",), aplicar_nas_pendentes=True
        ),
    )
    corpo = falso.ultimo_corpo
    assert corpo["updatePendingPayments"] is True
    assert corpo["description"] == "CT-contrato-1\nReajuste anual"
    assert all(falso.cobrancas[c]["value"] == 150.0 for c in cobrancas)


async def test_alterar_vazia_ou_para_cartao_e_recusada(adapter, falso):
    registrada = await adapter.criar_assinatura(fazer_assinatura())
    antes = len(falso.chamadas)
    with pytest.raises(Validacao):
        await adapter.alterar_assinatura(registrada.id_banco, AlteracaoAssinatura())
    with pytest.raises(NaoSuportado):
        await adapter.alterar_assinatura(
            registrada.id_banco, AlteracaoAssinatura(meio=MeioCobranca.CARTAO)
        )
    assert len(falso.chamadas) == antes


async def test_suspender_e_reativar(adapter, falso):
    registrada = await adapter.criar_assinatura(fazer_assinatura())
    suspensa = await adapter.suspender_assinatura(registrada.id_banco)
    assert suspensa.status is StatusAssinatura.SUSPENSA
    assert falso.gerar_ciclos(registrada.id_banco, 1) == []
    ativa = await adapter.reativar_assinatura(registrada.id_banco, date(2027, 3, 10))
    assert ativa.status is StatusAssinatura.ATIVA
    assert ativa.proximo_vencimento == date(2027, 3, 10)
    assert falso.ultimo_corpo == {"status": "ACTIVE", "nextDueDate": "2027-03-10"}


async def test_encerrar_remove_as_cobrancas_abertas(adapter, falso):
    registrada = await adapter.criar_assinatura(fazer_assinatura())
    aberta, paga = falso.gerar_ciclos(registrada.id_banco, 2)
    falso.confirmar(paga)
    encerrada = await adapter.encerrar_assinatura(registrada.id_banco)
    assert encerrada.status is StatusAssinatura.ENCERRADA
    assert (
        await adapter.consultar_assinatura(registrada.id_banco)
    ).status is StatusAssinatura.ENCERRADA
    assert falso.cobrancas[aberta]["deleted"] is True
    assert falso.cobrancas[paga]["deleted"] is False
    assert (await adapter.consultar_titulo(aberta)).status is StatusCanonico.BAIXADO


async def test_listar_cobrancas_sem_detalhar(adapter, falso):
    registrada = await adapter.criar_assinatura(fazer_assinatura())
    ids = falso.gerar_ciclos(registrada.id_banco, 3)
    falso.confirmar(ids[0])
    antes = len(falso.chamadas)
    titulos = await adapter.listar_cobrancas(registrada.id_banco)
    assert [t.id_banco for t in titulos] == ids
    assert titulos[0].status is StatusCanonico.LIQUIDADO
    assert titulos[0].valor_pago == Decimal("99.9")
    assert titulos[1].status is StatusCanonico.REGISTRADO
    assert titulos[1].seu_numero == "CT-contrato-1"
    assert titulos[1].nosso_numero
    # Uma chamada so: a listagem nao busca linha digitavel nem QR.
    assert len(falso.chamadas) - antes == 1
    assert not _chamadas(falso, "GET", "/identificationField")


async def test_carne_do_sandbox_antecipa_os_ciclos(adapter, falso):
    registrada = await adapter.criar_assinatura(fazer_assinatura())
    resp = await falso.cliente().get(
        f"https://api-sandbox.asaas.com/v3/subscriptions/{registrada.id_banco}"
        "/paymentBook?month=4&year=2027",
        headers={"access_token": CHAVE_API, "User-Agent": "teste"},
    )
    assert resp.content.startswith(b"%PDF")
    titulos = await adapter.listar_cobrancas(registrada.id_banco)
    assert len(titulos) == 4
    assert all(t.resposta_bruta["subscription"] == registrada.id_banco for t in titulos)


async def test_criar_checkout_payload_e_link(adapter, falso):
    assinatura = fazer_assinatura(
        meio=MeioCobranca.CARTAO,
        data_fim=date(2027, 12, 31),
        mensagens=("Plano Pro", "12 meses"),
    )
    checkout = await adapter.criar_checkout(
        assinatura,
        url_sucesso="https://erp.exemplo/ok",
        url_cancelamento="https://erp.exemplo/cancelou",
        url_expiracao="https://erp.exemplo/expirou",
        minutos_validade=120,
    )
    corpo = falso.ultimo_corpo
    assert corpo["billingTypes"] == ["CREDIT_CARD"]
    assert corpo["chargeTypes"] == ["RECURRENT"]
    assert corpo["minutesToExpire"] == 120
    assert corpo["externalReference"] == "contrato-1"
    assert corpo["callback"] == {
        "successUrl": "https://erp.exemplo/ok",
        "cancelUrl": "https://erp.exemplo/cancelou",
        "expiredUrl": "https://erp.exemplo/expirou",
    }
    assert corpo["items"] == [
        {
            "name": "CT-contrato-1",
            "description": "Plano Pro\n12 meses",
            "quantity": 1,
            "value": 99.9,
            "imageBase64": IMAGEM_PADRAO_BASE64,
        }
    ]
    assert corpo["customerData"]["cpfCnpj"] == "52998224725"
    assert corpo["customerData"]["postalCode"] == "30110000"
    assert corpo["subscription"] == {
        "cycle": "MONTHLY",
        "nextDueDate": "2027-01-10 00:00:00",
        "endDate": "2027-12-31 00:00:00",
    }
    assert checkout.status is StatusCheckout.ATIVO
    assert checkout.url.endswith(checkout.id_banco)
    assert "customerData" not in repr(checkout)


async def test_checkout_recusa_boleto_e_validade_fora_da_faixa(adapter, falso):
    assinatura = fazer_assinatura(meio=MeioCobranca.CARTAO)
    with pytest.raises(Validacao):
        await adapter.criar_checkout(
            assinatura,
            url_sucesso="u",
            url_cancelamento="c",
            meios=(MeioCobranca.BOLETO,),
        )
    with pytest.raises(Validacao):
        await adapter.criar_checkout(
            assinatura, url_sucesso="u", url_cancelamento="c", minutos_validade=5
        )
    assert not falso.chamadas


async def test_checkout_pago_vira_assinatura_ligada(adapter, falso):
    checkout = await adapter.criar_checkout(
        fazer_assinatura(meio=MeioCobranca.CARTAO),
        url_sucesso="u",
        url_cancelamento="c",
    )
    assinatura_id = falso.pagar_checkout(checkout.id_banco)
    registrada = await adapter.consultar_assinatura(assinatura_id)
    assert registrada.meio is MeioCobranca.CARTAO
    assert registrada.id_checkout == checkout.id_banco
    assert registrada.status is StatusAssinatura.ATIVA


def test_webhook_de_assinatura(adapter):
    esperado = {
        "SUBSCRIPTION_CREATED": TipoEventoRecorrencia.CRIADA,
        "SUBSCRIPTION_UPDATED": TipoEventoRecorrencia.ALTERADA,
        "SUBSCRIPTION_INACTIVATED": TipoEventoRecorrencia.SUSPENSA,
        "SUBSCRIPTION_DELETED": TipoEventoRecorrencia.ENCERRADA,
    }
    for evento, tipo in esperado.items():
        eventos = adapter.parse_webhook_recorrencia(
            asaas_falso.webhook_assinatura("sub_1", evento, checkout="chk-1")
        )
        assert len(eventos) == 1, evento
        assert eventos[0].chave_dedup == ("asaas", "sub_1", tipo)
        assert eventos[0].id_checkout == "chk-1"
        assert eventos[0].ocorrido_em == datetime(2026, 12, 1, 9, 0, 0)
    assert (
        adapter.parse_webhook_recorrencia(
            asaas_falso.webhook_assinatura("sub_1", "SUBSCRIPTION_SPLIT_DISABLED")
        )
        == []
    )


def test_webhook_de_checkout(adapter):
    esperado = {
        "CHECKOUT_PAID": TipoEventoRecorrencia.CHECKOUT_PAGO,
        "CHECKOUT_CANCELED": TipoEventoRecorrencia.CHECKOUT_CANCELADO,
        "CHECKOUT_EXPIRED": TipoEventoRecorrencia.CHECKOUT_EXPIRADO,
    }
    for evento, tipo in esperado.items():
        eventos = adapter.parse_webhook_recorrencia(
            asaas_falso.webhook_checkout("chk-9", evento)
        )
        assert [e.tipo for e in eventos] == [tipo], evento
        assert eventos[0].referencia == "chk-9"
        assert eventos[0].id_checkout == "chk-9"
    assert (
        adapter.parse_webhook_recorrencia(
            asaas_falso.webhook_checkout("chk-9", "CHECKOUT_CREATED")
        )
        == []
    )


def test_webhooks_de_categorias_cruzadas_sao_ignorados(adapter):
    # O webhook de cobranca ignora assinatura e checkout, e vice-versa.
    assert adapter.parse_webhook(asaas_falso.webhook_assinatura("sub_1")) == []
    assert adapter.parse_webhook(asaas_falso.webhook_checkout("chk-1")) == []
    assert adapter.parse_webhook_recorrencia(asaas_falso.webhook("pay_1")) == []


def test_cobranca_de_assinatura_traz_id_recorrencia(adapter):
    corpo = asaas_falso.webhook("pay_1", "PAGO").replace(
        b'"customer": "cus_000001"',
        b'"customer": "cus_000001", "subscription": "sub_77"',
    )
    evento = adapter.parse_webhook(corpo)[0]
    assert evento.id_recorrencia == "sub_77"
    assert adapter.parse_webhook(asaas_falso.webhook("pay_2"))[0].id_recorrencia is None


def test_webhook_de_recorrencia_exige_token_quando_configurado(falso, dormir):
    adapter = AsaasAdapter(
        AsaasConfig(
            conta="c1",
            api_key=CHAVE_API,
            webhook_auth_token="t0ken-do-webhook-com-32-caracteres!",
        ),
        client=falso.cliente(),
        dormir=dormir,
    )
    corpo = asaas_falso.webhook_assinatura("sub_1")
    with pytest.raises(Validacao):
        adapter.parse_webhook_recorrencia(corpo)
    assert (
        len(
            adapter.parse_webhook_recorrencia(
                corpo, {"asaas-access-token": "t0ken-do-webhook-com-32-caracteres!"}
            )
        )
        == 1
    )
    with pytest.raises(Validacao):
        adapter.parse_webhook_recorrencia(b"nao e json")
