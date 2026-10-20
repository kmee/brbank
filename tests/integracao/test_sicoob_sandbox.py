"""Ciclo real contra o sandbox publico do Sicoob (boleto v3 e Pix).

Opcional: roda so com `-m sandbox` e as variaveis abaixo definidas; sem elas,
pula. Os valores sao o Client ID e o Access token (Bearer) fixos que o Sicoob
mostra na pagina Sandbox do portal developers.sicoob.com.br (iguais para todos
e so de teste). Nunca coloque os valores no codigo.

    BRBANK_SICOOB_SANDBOX_CLIENT_ID=... BRBANK_SICOOB_SANDBOX_TOKEN=... \
        uv run pytest -m sandbox tests/integracao

O sandbox de boleto valida o corpo contra o swagger da v3 e responde sempre o
mesmo exemplo; o de Pix valida o corpo e responde dados aleatorios no formato
do BACEN. Nenhum dos dois guarda estado, entao aqui se confere o contrato
(rota, metodo, cabecalhos, corpo, codigo HTTP e leitura da resposta), nao a
transicao de estado. Detalhes em docs/sicoob-duvidas.md e docs/pix-duvidas.md.
"""

import os
from datetime import date, timedelta

import httpx
import pytest

from brbank import Instrucao, StatusCanonico, TipoInstrucao, Validacao
from brbank.adapters.pix_bacen import (
    PERFIS,
    PixBacenAdapter,
    PixBacenConfig,
    gerar_txid,
)
from brbank.adapters.sicoob import URL_SANDBOX, SicoobAdapter, SicoobConfig
from tests.conftest import fazer_titulo

CLIENT_ID = os.environ.get("BRBANK_SICOOB_SANDBOX_CLIENT_ID")
TOKEN = os.environ.get("BRBANK_SICOOB_SANDBOX_TOKEN")
# Numero de cliente do exemplo do proprio sandbox.
NUMERO_CLIENTE = 25546454
CHAVE_PIX_TESTE = "00000000-0000-0000-0000-000000000000"

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        not (CLIENT_ID and TOKEN),
        reason="defina BRBANK_SICOOB_SANDBOX_CLIENT_ID e BRBANK_SICOOB_SANDBOX_TOKEN",
    ),
]


def _titulo(chave: str):
    return fazer_titulo(chave).model_copy(
        update={"vencimento": date.today() + timedelta(days=30)}
    )


@pytest.fixture
async def boleto():
    adapter = SicoobAdapter(
        SicoobConfig(
            conta="sandbox",
            client_id=CLIENT_ID,
            numero_cliente=NUMERO_CLIENTE,
            numero_conta_corrente="0",
            codigo_especie_documento="DM",
            ambiente="sandbox",
            token_sandbox=TOKEN,
        )
    )
    yield adapter
    await adapter.aclose()


async def test_boleto_ciclo_completo(boleto):
    registrado = await boleto.registrar_titulo(_titulo("sbx-boleto"))
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.nosso_numero
    assert registrado.linha_digitavel
    assert registrado.codigo_barras
    assert registrado.pix_copia_cola.startswith("000201")
    id_banco = registrado.id_banco

    consultado = await boleto.consultar_titulo(id_banco)
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.linha_digitavel

    for instrucao in (
        Instrucao(
            chave_idempotencia="sbx-p",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=date.today() + timedelta(days=60),
        ),
        Instrucao(chave_idempotencia="sbx-a", tipo=TipoInstrucao.ABATIMENTO, valor="1"),
        Instrucao(chave_idempotencia="sbx-t", tipo=TipoInstrucao.PROTESTO),
    ):
        apos = await boleto.enviar_instrucao(id_banco, instrucao)
        assert apos.status is StatusCanonico.REGISTRADO

    assert (await boleto.obter_pdf(id_banco)).startswith(b"%PDF")
    baixado = await boleto.baixar_titulo(id_banco)
    assert baixado.status is StatusCanonico.BAIXADO


async def test_boleto_corpo_fora_do_swagger_e_400():
    """Guarda o comportamento que motivou a correcao da especie (sigla)."""
    async with httpx.AsyncClient(timeout=30) as cliente:
        resp = await cliente.post(
            f"{URL_SANDBOX}/boletos",
            json=[{"numeroCliente": NUMERO_CLIENTE}],
            headers={"Authorization": f"Bearer {TOKEN}", "client_id": CLIENT_ID},
        )
    assert resp.status_code == 400
    assert "mensagens" in resp.json()


async def test_boleto_sem_client_id_e_401():
    async with httpx.AsyncClient(timeout=30) as cliente:
        resp = await cliente.get(
            f"{URL_SANDBOX}/boletos",
            params={"numeroCliente": NUMERO_CLIENTE, "codigoModalidade": 1},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert resp.status_code == 401


def _pix(modalidade: str) -> PixBacenAdapter:
    return PixBacenAdapter(
        PixBacenConfig(
            perfil="sicoob",
            conta="sandbox",
            chave_pix=CHAVE_PIX_TESTE,
            client_id=CLIENT_ID,
            ambiente="sandbox",
            modalidade=modalidade,
            token_estatico=TOKEN,
        )
    )


async def _tolerando_dado_aleatorio(chamada):
    """Executa a chamada; aceita so erro de LEITURA de dado aleatorio do mock.

    Erro com `status_http` e recusa do banco (corpo, rota ou cabecalho errados)
    e falha o teste. Sem `status_http`, e o adapter recusando um valor
    aleatorio do mock (ex.: CONCLUIDA sem Pix), o comportamento certo.
    """
    try:
        return await chamada
    except Validacao as erro:
        assert erro.status_http is None, erro
        return None


@pytest.mark.parametrize("modalidade", ["cob", "cobv"])
async def test_pix_ciclo_completo(modalidade):
    adapter = _pix(modalidade)
    try:
        titulo = _titulo(f"sbx-pix-{modalidade}")
        txid = gerar_txid("sandbox", titulo.chave_idempotencia)
        registrado = await _tolerando_dado_aleatorio(adapter.registrar_titulo(titulo))
        if registrado is not None:
            assert registrado.id_banco == registrado.txid == txid
            assert registrado.pix_copia_cola
        consultado = await _tolerando_dado_aleatorio(adapter.consultar_titulo(txid))
        if consultado is not None:
            assert consultado.id_banco == txid
        baixado = await _tolerando_dado_aleatorio(adapter.baixar_titulo(txid))
        if baixado is not None:
            assert baixado.id_banco == txid
    finally:
        await adapter.aclose()


async def test_pix_webhook_configurar_consultar_remover():
    adapter = _pix("cobv")
    try:
        await adapter.configurar_webhook("https://example.com/brbank/webhook")
        assert await adapter.consultar_webhook()
        await adapter.remover_webhook()
    finally:
        await adapter.aclose()


async def test_pix_cob_com_endereco_do_devedor_e_400():
    """Guarda o comportamento que motivou a correcao do devedor da cob."""
    url = PERFIS["sicoob"].url_sandbox
    async with httpx.AsyncClient(timeout=30) as cliente:
        resp = await cliente.put(
            f"{url}/cob/{gerar_txid('sandbox', 'sbx-devedor')}",
            json={
                "calendario": {"expiracao": 3600},
                "valor": {"original": "1.00"},
                "chave": CHAVE_PIX_TESTE,
                "devedor": {"cpf": "52998224725", "nome": "Teste", "cep": "30110000"},
            },
            headers={"Authorization": f"Bearer {TOKEN}", "client_id": CLIENT_ID},
        )
    assert resp.status_code == 400
