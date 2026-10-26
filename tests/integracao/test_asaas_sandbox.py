"""Ciclo real contra o sandbox do Asaas (cobranca, Pix, assinatura, checkout).

Opcional: roda so com `-m sandbox` e a variavel abaixo definida; sem ela,
pula. A chave e a de uma conta criada em sandbox.asaas.com (`$aact_hmlg_...`)
e nunca entra no codigo:

    BRBANK_ASAAS_SANDBOX_API_KEY=... uv run pytest -m sandbox tests/integracao

Ao contrario do sandbox do Sicoob, o do Asaas guarda estado: aqui se prova a
transicao de estado (registro, confirmacao por `/sandbox/payment/{id}/confirm`,
baixa, ciclos da assinatura pelo carne). O que o sandbox nao exercita (juros,
multa, desconto; QR do boleto sem chave Pix) esta em docs/asaas-duvidas.md.
"""

import os
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from brbank import (
    AlteracaoAssinatura,
    Assinatura,
    Ciclo,
    Duplicado,
    Instrucao,
    MeioCobranca,
    StatusAssinatura,
    StatusCanonico,
    StatusCheckout,
    TipoInstrucao,
    Validacao,
)
from brbank.adapters.asaas import URL_SANDBOX, USER_AGENT, AsaasAdapter, AsaasConfig
from tests.conftest import fazer_titulo, pagador

CHAVE = os.environ.get("BRBANK_ASAAS_SANDBOX_API_KEY")
HOJE = date.today()

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(not CHAVE, reason="defina BRBANK_ASAAS_SANDBOX_API_KEY"),
]


def _chave(prefixo: str) -> str:
    # Uma chave por execucao: o sandbox guarda as cobrancas anteriores.
    return f"{prefixo}-{HOJE.isoformat()}-{os.getpid()}"


def _titulo(prefixo: str):
    return fazer_titulo(_chave(prefixo)).model_copy(
        update={"vencimento": HOJE + timedelta(days=30)}
    )


async def _sandbox(caminho: str) -> int:
    """Acoes que so o sandbox tem (confirmar pagamento)."""
    async with httpx.AsyncClient(timeout=30) as cliente:
        resp = await cliente.post(
            f"{URL_SANDBOX}/{caminho}",
            json={},
            headers={"access_token": CHAVE, "User-Agent": USER_AGENT},
        )
    return resp.status_code


@pytest.fixture
async def boleto():
    adapter = AsaasAdapter(
        AsaasConfig(conta="sandbox", api_key=CHAVE, ambiente="sandbox")
    )
    yield adapter
    await adapter.aclose()


@pytest.fixture
async def pix():
    adapter = AsaasAdapter(
        AsaasConfig(
            conta="sandbox", api_key=CHAVE, ambiente="sandbox", billing_type="PIX"
        )
    )
    yield adapter
    await adapter.aclose()


async def test_boleto_ciclo_completo(boleto):
    titulo = _titulo("sbx-bol")
    registrado = await boleto.registrar_titulo(titulo)
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.id_banco.startswith("pay_")
    assert registrado.nosso_numero
    assert registrado.linha_digitavel
    assert registrado.codigo_barras
    assert registrado.seu_numero == titulo.seu_numero
    assert registrado.resposta_bruta.get("bankSlipUrl")
    id_banco = registrado.id_banco

    with pytest.raises(Duplicado) as duplicado:
        await boleto.registrar_titulo(titulo)
    assert duplicado.value.id_banco == id_banco

    prorrogado = await boleto.enviar_instrucao(
        id_banco,
        Instrucao(
            chave_idempotencia="p",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=HOJE + timedelta(days=60),
        ),
    )
    assert (
        prorrogado.resposta_bruta["dueDate"] == (HOJE + timedelta(days=60)).isoformat()
    )
    abatido = await boleto.enviar_instrucao(
        id_banco,
        Instrucao(chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor="10"),
    )
    assert Decimal(str(abatido.resposta_bruta["value"])) == Decimal("140.00")

    assert (await boleto.obter_pdf(id_banco)).startswith(b"%PDF")

    assert await _sandbox(f"sandbox/payment/{id_banco}/confirm") == 200
    pago = await boleto.consultar_titulo(id_banco)
    assert pago.status is StatusCanonico.LIQUIDADO
    assert pago.valor_pago == Decimal("140.00")
    assert pago.data_pagamento == HOJE

    with pytest.raises(Validacao) as recusa:
        await boleto.baixar_titulo(id_banco)
    assert recusa.value.status_http == 400


async def test_boleto_baixado_vira_baixado_na_consulta(boleto):
    registrado = await boleto.registrar_titulo(_titulo("sbx-bol2"))
    assert (await boleto.baixar_titulo(registrado.id_banco)).status is (
        StatusCanonico.BAIXADO
    )
    consultado = await boleto.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.BAIXADO
    assert consultado.resposta_bruta["deleted"] is True


async def test_titulo_inexistente_e_404(boleto):
    with pytest.raises(Validacao) as erro:
        await boleto.consultar_titulo("pay_naoexiste")
    assert erro.value.status_http == 404


async def test_pix_registra_com_qr(pix):
    registrado = await pix.registrar_titulo(_titulo("sbx-pix"))
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.pix_copia_cola
    assert registrado.linha_digitavel is None
    assert registrado.nosso_numero is None


async def test_assinatura_ciclo_completo(boleto):
    assinatura = Assinatura(
        chave_idempotencia=_chave("sbx-ass"),
        seu_numero="CT-SBX",
        pagador=pagador(),
        valor=Decimal("12.34"),
        ciclo=Ciclo.MENSAL,
        primeiro_vencimento=HOJE + timedelta(days=15),
        meio=MeioCobranca.BOLETO,
        mensagens=("Plano teste",),
    )
    criada = await boleto.criar_assinatura(assinatura)
    assert criada.id_banco.startswith("sub_")
    assert criada.status is StatusAssinatura.ATIVA
    assert criada.valor == Decimal("12.34")
    assert criada.ciclo is Ciclo.MENSAL
    assert criada.meio is MeioCobranca.BOLETO
    ref = criada.id_banco

    # A primeira cobranca nasce com a assinatura (vencimento dentro da
    # antecedencia da conta) e o proximo vencimento ja avanca um ciclo.
    cobrancas = await boleto.listar_cobrancas(ref)
    assert len(cobrancas) == 1
    assert cobrancas[0].status is StatusCanonico.REGISTRADO
    assert cobrancas[0].resposta_bruta["subscription"] == ref
    assert cobrancas[0].seu_numero == "CT-SBX"

    with pytest.raises(Duplicado) as duplicado:
        await boleto.criar_assinatura(assinatura)
    assert duplicado.value.id_banco == ref

    alterada = await boleto.alterar_assinatura(
        ref, AlteracaoAssinatura(valor="20.00", aplicar_nas_pendentes=True)
    )
    assert alterada.valor == Decimal("20.00")
    assert all(
        Decimal(str(c.resposta_bruta["value"])) == Decimal("20.00")
        for c in await boleto.listar_cobrancas(ref)
    )

    assert (await boleto.suspender_assinatura(ref)).status is StatusAssinatura.SUSPENSA
    assert (
        await boleto.reativar_assinatura(ref, HOJE + timedelta(days=45))
    ).status is StatusAssinatura.ATIVA

    encerrada = await boleto.encerrar_assinatura(ref)
    assert encerrada.status is StatusAssinatura.ENCERRADA
    assert (await boleto.consultar_assinatura(ref)).status is StatusAssinatura.ENCERRADA
    # O DELETE remove as cobrancas abertas.
    assert await boleto.listar_cobrancas(ref) == []


async def test_checkout_recorrente_de_cartao(boleto):
    assinatura = Assinatura(
        chave_idempotencia=_chave("sbx-chk"),
        seu_numero="CT-SBX-CHK",
        pagador=pagador().model_copy(update={"telefone": "31999998888"}),
        valor=Decimal("12.34"),
        ciclo=Ciclo.MENSAL,
        primeiro_vencimento=HOJE + timedelta(days=15),
        meio=MeioCobranca.CARTAO,
    )
    checkout = await boleto.criar_checkout(
        assinatura,
        url_sucesso="https://example.com/ok",
        url_cancelamento="https://example.com/cancel",
        minutos_validade=30,
    )
    assert checkout.status is StatusCheckout.ATIVO
    assert checkout.url.startswith("https://")
    assert checkout.id_banco in checkout.url
    assert await _sandbox(f"checkouts/{checkout.id_banco}/cancel") == 200
