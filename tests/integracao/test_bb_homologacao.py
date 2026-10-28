"""Ciclo real contra a homologacao do Banco do Brasil (Cobranca v2, Pix v2 e
Pagamentos em Lote v1).

Opcional: roda so com `-m sandbox` e a credencial de uma aplicacao "Em teste" do
Portal Developers BB. Aponte `BRBANK_BB_HM_CREDENCIAIS` para o arquivo que o portal
baixa (linhas `appKey=`, `clientID=`, `clientSecret=`); o arquivo nunca entra no
repositorio e o teste nao imprime nenhum valor.

    BRBANK_BB_HM_CREDENCIAIS=~/Downloads/credenciais-aplNNNNNN.txt \
        uv run pytest -m sandbox tests/integracao/test_bb_homologacao.py

A homologacao e compartilhada entre todas as aplicacoes e guarda estado: o
convenio, a carteira e a chave Pix abaixo sao os de teste do BB. Instrucao (baixa,
prorrogacao) so e aceita 30 minutos depois do registro; aqui se confere que a
recusa chega como `Indisponivel`. Detalhes em docs/bb-duvidas.md.
"""

import os
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from brbank import (
    ContaBancaria,
    DestinoPix,
    Indisponivel,
    LotePagamento,
    Pagamento,
    StatusCanonico,
    StatusLote,
    StatusPagamento,
    TipoChavePix,
    TipoPagamento,
)
from brbank.adapters.bb import BancoDoBrasilAdapter, BBConfig
from brbank.adapters.bb_pagamento import (
    BancoDoBrasilPagamentoAdapter,
    BBPagamentoConfig,
)
from brbank.adapters.pix_bacen import PixBacenAdapter, PixBacenConfig
from tests.conftest import fazer_titulo

CONVENIO_TESTE = "3128557"
CARTEIRA_TESTE = 17
VARIACAO_TESTE = 35
CHAVE_PIX_TESTE = "9e881f18-cc66-4fc7-8f2c-a795dbb2bfc1"


def _credenciais() -> dict[str, str]:
    caminho = os.environ.get("BRBANK_BB_HM_CREDENCIAIS")
    if not caminho or not Path(caminho).expanduser().is_file():
        return {}
    dados = {}
    for linha in Path(caminho).expanduser().read_text().splitlines():
        if "=" in linha:
            chave, valor = linha.split("=", 1)
            dados[chave.strip()] = valor.strip()
    return dados


CRED = _credenciais()

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        not {"appKey", "clientID", "clientSecret"} <= CRED.keys(),
        reason="defina BRBANK_BB_HM_CREDENCIAIS com o arquivo de credenciais do portal",
    ),
]


def _titulo(prefixo: str, **extra):
    seu = str(time.time_ns())[-10:]
    return fazer_titulo(f"{prefixo}-{seu}").model_copy(
        update={
            "vencimento": date.today() + timedelta(days=30),
            "seu_numero": seu,
            "valor": Decimal("123.45"),
            "mensagens": [],
            **extra,
        }
    )


@pytest.fixture
async def boleto():
    adapter = BancoDoBrasilAdapter(
        BBConfig(
            conta="hm",
            client_id=CRED.get("clientID", ""),
            client_secret=CRED.get("clientSecret", ""),
            chave_desenvolvedor=CRED.get("appKey", ""),
            convenio=CONVENIO_TESTE,
            carteira=CARTEIRA_TESTE,
            variacao_carteira=VARIACAO_TESTE,
            ambiente="sandbox",
            indicador_pix=True,
        )
    )
    yield adapter
    await adapter.aclose()


async def test_boleto_registra_e_consulta(boleto):
    registrado = await boleto.registrar_titulo(_titulo("bb"))
    assert registrado.status is StatusCanonico.REGISTRADO
    assert len(registrado.id_banco) == 20
    assert registrado.id_banco.startswith("000" + CONVENIO_TESTE)
    assert registrado.linha_digitavel and registrado.codigo_barras
    assert registrado.pix_copia_cola and registrado.txid

    consultado = await boleto.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.codigo_barras == registrado.codigo_barras


async def test_instrucao_logo_apos_registro_e_indisponivel(boleto):
    registrado = await boleto.registrar_titulo(_titulo("bb-cedo"))
    with pytest.raises(Indisponivel) as erro:
        await boleto.baixar_titulo(registrado.id_banco)
    assert erro.value.retryable


@pytest.mark.parametrize("modalidade", ["cob", "cobv"])
async def test_pix_registra_consulta_e_remove(modalidade):
    adapter = PixBacenAdapter(
        PixBacenConfig(
            perfil="bb",
            conta="hm",
            chave_pix=CHAVE_PIX_TESTE,
            client_id=CRED["clientID"],
            client_secret=CRED["clientSecret"],
            app_key=CRED["appKey"],
            ambiente="sandbox",
            modalidade=modalidade,
        )
    )
    try:
        titulo = _titulo("pix-" + modalidade, valor=Decimal("10.50"))
        registrado = await adapter.registrar_titulo(titulo)
        assert registrado.status is StatusCanonico.REGISTRADO
        assert registrado.pix_copia_cola

        consultado = await adapter.consultar_titulo(registrado.id_banco)
        assert consultado.status is StatusCanonico.REGISTRADO

        baixado = await adapter.baixar_titulo(registrado.id_banco)
        assert baixado.status is StatusCanonico.BAIXADO
    finally:
        await adapter.aclose()


# --- Pagamentos em Lote ------------------------------------------------------
# Conta pagadora, contrato e destinos da massa de homologacao do BB (CNPJs
# inexistentes na Receita). O boleto e a guia de teste sao compartilhados com
# todas as aplicacoes: a linha e aceita, mas o pagamento pode sair REJEITADO
# (motivo 295) porque outra aplicacao ja o pagou.
CONTA_PAGADORA = ContaBancaria(agencia="1607", conta="99738672", digito="X")


def _pagamentos(tipo: TipoPagamento) -> tuple[Pagamento, ...]:
    marca = str(time.time_ns())[-8:]
    comum = {"data": date.today(), "descricao": "brbank hm"}
    if tipo is TipoPagamento.BOLETO:
        return (
            Pagamento(
                chave_idempotencia="bol-" + marca,
                tipo=tipo,
                valor=Decimal("15.00"),
                codigo_barras="00195313800000015000000003051284190526000117",
                beneficiario_documento="95752207000129",
                **comum,
            ),
        )
    if tipo is TipoPagamento.GUIA:
        return (
            Pagamento(
                chave_idempotencia="gui-" + marca,
                tipo=tipo,
                valor=Decimal("64.14"),
                codigo_barras="83630000000641400052836100812355200812351310",
                **comum,
            ),
        )
    if tipo is TipoPagamento.PIX:
        conta_pix = ContaBancaria(agencia="551", conta="43814", digito="6", compe="001")
        return (
            Pagamento(
                chave_idempotencia="pixe-" + marca,
                tipo=tipo,
                valor=Decimal("1.11"),
                pix=DestinoPix(
                    tipo_chave=TipoChavePix.EMAIL, chave="hmtestes2@bb.com.br"
                ),
                **comum,
            ),
            Pagamento(
                chave_idempotencia="pixc-" + marca,
                tipo=tipo,
                valor=Decimal("2.22"),
                pix=DestinoPix(conta=conta_pix, documento="95127446000198"),
                **comum,
            ),
        )
    return (
        Pagamento(
            chave_idempotencia="ted-" + marca,
            tipo=tipo,
            valor=Decimal("4.44"),
            conta_credito=ContaBancaria(
                agencia="551", conta="60840", digito="8", compe="001"
            ),
            favorecido_documento="84526081000158",
            **comum,
        ),
    )


@pytest.mark.parametrize("tipo", list(TipoPagamento))
async def test_pagamento_em_lote_aceita_libera_e_consulta(tipo):
    adapter = BancoDoBrasilPagamentoAdapter(
        BBPagamentoConfig(
            conta="hm",
            client_id=CRED["clientID"],
            client_secret=CRED["clientSecret"],
            chave_desenvolvedor=CRED["appKey"],
            contrato="731030",
            ambiente="sandbox",
            tipo_pagamento=128,
        )
    )
    try:
        numero = await adapter.reservar_numero_lote()
        lote = LotePagamento(
            chave_idempotencia=f"{tipo.value}-{numero}",
            tipo=tipo,
            conta_debito=CONTA_PAGADORA,
            pagamentos=_pagamentos(tipo),
            numero=numero,
        )
        registrado = await adapter.pagar_lote(lote)
        assert registrado.id_banco == numero
        assert registrado.status is StatusLote.ACEITO
        assert len(registrado.aceitos) == len(lote.pagamentos)

        consultado = await adapter.consultar_lote(numero, tipo)
        assert [p.id_banco for p in consultado.pagamentos] == [
            p.id_banco for p in registrado.pagamentos
        ]

        await adapter.liberar_lote(numero)
        pagamento = await adapter.consultar_pagamento(
            registrado.aceitos[0].id_banco, tipo
        )
        assert pagamento.status in (
            StatusPagamento.ACEITO,
            StatusPagamento.EM_PROCESSAMENTO,
            StatusPagamento.PAGO,
            StatusPagamento.REJEITADO,
        )
        assert pagamento.valor == lote.pagamentos[0].valor
    finally:
        await adapter.aclose()
