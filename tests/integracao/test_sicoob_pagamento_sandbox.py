"""Pagamentos Pix do Sicoob contra o sandbox publico (mesma credencial do boleto).

Opcional: roda so com `-m sandbox` e as variaveis do `test_sicoob_sandbox.py`
(`BRBANK_SICOOB_SANDBOX_CLIENT_ID` e `BRBANK_SICOOB_SANDBOX_TOKEN`).

O que o sandbox de Pagamentos Pix e (29/10/2026, docs/sicoob-pagamento-duvidas.md):
um mock que valida o corpo contra o swagger e devolve o exemplo do swagger. Na
iniciacao, confirmacao, consulta e webhook o exemplo e de sucesso (com valores
"string" e estado NAO_REALIZADO); na rota do QR o unico exemplo e o erro 400,
que vem para qualquer corpo. Por isso o QR fica sem prova aqui.
"""

import os
from datetime import date
from decimal import Decimal

import httpx
import pytest

from brbank import (
    ContaBancaria,
    DestinoPix,
    LotePagamento,
    Pagamento,
    StatusPagamento,
    TipoChavePix,
    TipoPagamento,
)
from brbank.adapters.sicoob_pagamento import (
    URL_SANDBOX,
    SicoobPagamentoAdapter,
    SicoobPagamentoConfig,
)
from tests.sicoob_pagamento_falso import brcode

CLIENT_ID = os.environ.get("BRBANK_SICOOB_SANDBOX_CLIENT_ID")
TOKEN = os.environ.get("BRBANK_SICOOB_SANDBOX_TOKEN")
DEBITO = ContaBancaria(agencia="4027", conta="12345", digito="6")
E2E = "E02038232202610291200abcdefghijk"

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        not (CLIENT_ID and TOKEN),
        reason="defina BRBANK_SICOOB_SANDBOX_CLIENT_ID e BRBANK_SICOOB_SANDBOX_TOKEN",
    ),
]


@pytest.fixture
async def adapter():
    adapter = SicoobPagamentoAdapter(
        SicoobPagamentoConfig(
            conta="sandbox",
            client_id=CLIENT_ID,
            pagador_documento="91300766000147",
            pagador_nome="KMEE Teste",
            ambiente="sandbox",
            token_sandbox=TOKEN,
        )
    )
    yield adapter
    await adapter.aclose()


def _lote(pagamento: Pagamento) -> LotePagamento:
    return LotePagamento(
        chave_idempotencia="sbx-lote",
        tipo=pagamento.tipo,
        conta_debito=DEBITO,
        pagamentos=(pagamento,),
    )


async def test_pix_por_chave_inicia_confirma_e_consulta(adapter):
    pagamento = Pagamento(
        chave_idempotencia="sbx-chave",
        tipo=TipoPagamento.PIX,
        valor=Decimal("1.99"),
        data=date.today(),
        descricao="teste brbank",
        pix=DestinoPix(tipo_chave=TipoChavePix.EMAIL, chave="fulano@example.com"),
    )
    registrado = await adapter.pagar_lote(_lote(pagamento))
    (linha,) = registrado.pagamentos
    # Iniciacao e confirmacao responderam 200 (corpo aceito pelo swagger); o
    # exemplo fixo traz estado NAO_REALIZADO e detalheRejeicao "string".
    assert linha.status is StatusPagamento.REJEITADO
    assert linha.id_banco
    assert linha.valor == Decimal("1.99")
    # O mock devolve endToEndId "string" na confirmacao; a consulta exige 32
    # posicoes (outro tamanho vira 400 no gateway), entao consulta um e2e no formato.
    consultado = await adapter.consultar_pagamento(E2E, TipoPagamento.PIX)
    assert consultado.status is StatusPagamento.REJEITADO


async def test_pix_por_dados_bancarios(adapter):
    conta = ContaBancaria(agencia="1", conta="1234567", digito="8", ispb="18236120")
    pagamento = Pagamento(
        chave_idempotencia="sbx-manual",
        tipo=TipoPagamento.PIX,
        valor=Decimal("1.99"),
        data=date.today(),
        pix=DestinoPix(conta=conta, documento="28133847044"),
    )
    (linha,) = (await adapter.pagar_lote(_lote(pagamento))).pagamentos
    assert linha.status is StatusPagamento.REJEITADO


async def test_qrcode_responde_sempre_o_exemplo_de_erro(adapter):
    pagamento = Pagamento(
        chave_idempotencia="sbx-qr",
        tipo=TipoPagamento.PIX_QRCODE,
        valor=Decimal("10.00"),
        data=date.today(),
        brcode=brcode("10.00"),
    )
    (linha,) = (await adapter.pagar_lote(_lote(pagamento))).pagamentos
    assert linha.status is StatusPagamento.RECUSADO
    assert linha.motivos[0] == "RequisicaoInvalida"


async def test_confirmacao_com_valor_em_ponto_e_400():
    """Guarda o formato que o adapter usa na confirmacao: virgula, nao ponto."""
    corpo = {
        "endToEndId": E2E,
        "meioIniciacao": "CHAVE",
        "repeticao": False,
    }
    cabecalhos = {"Authorization": f"Bearer {TOKEN}", "client_id": CLIENT_ID}
    async with httpx.AsyncClient(timeout=30) as cliente:
        ponto = await cliente.post(
            f"{URL_SANDBOX}/pagamentos/confirmacao",
            json={**corpo, "valor": "1.99"},
            headers=cabecalhos,
        )
        virgula = await cliente.post(
            f"{URL_SANDBOX}/pagamentos/confirmacao",
            json={**corpo, "valor": "1,99"},
            headers=cabecalhos,
        )
    assert ponto.status_code == 400
    assert virgula.status_code == 200


async def test_webhook_configurar_consultar_remover(adapter):
    await adapter.configurar_webhook_pagamento("https://example.com/brbank/pix-pag")
    assert await adapter.consultar_webhook_pagamento()
    await adapter.remover_webhook_pagamento()
