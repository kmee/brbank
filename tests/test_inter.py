from datetime import date
from decimal import Decimal

import httpx
import pytest

from brbank import (
    Capacidade,
    Encargos,
    Indisponivel,
    Instrucao,
    NaoSuportado,
    StatusCanonico,
    TipoInstrucao,
    Validacao,
    obter_classe,
)
from brbank.adapters.inter import InterAdapter, InterConfig
from tests.conftest import fazer_titulo
from tests.inter_falso import InterFalso


@pytest.fixture
def falso():
    return InterFalso()


@pytest.fixture
def adapter(falso, dormir):
    config = InterConfig(
        conta="c1", client_id="id", client_secret="s", conta_corrente="999"
    )
    return InterAdapter(config, client=falso.cliente(), dormir=dormir)


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
        }
    )
    await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["valorNominal"] == 150.01
    assert corpo["dataVencimento"] == "2026-12-10"
    assert corpo["pagador"]["cpfCnpj"] == "52998224725"
    assert corpo["pagador"]["tipoPessoa"] == "FISICA"
    assert corpo["beneficiarioFinal"]["tipoPessoa"] == "JURIDICA"
    assert corpo["multa"] == {"codigo": "PERCENTUAL", "taxa": 2.0}
    assert corpo["mora"] == {"codigo": "TAXAMENSAL", "taxa": 1.0}
    assert corpo["desconto"]["quantidadeDias"] == 3
    assert corpo["mensagem"] == {"linha1": "linha um", "linha2": "linha dois"}
    assert falso.ultimo_cabecalho["x-conta-corrente"] == "999"
    assert falso.ultimo_cabecalho["authorization"] == "Bearer tok-1"


async def test_valor_minimo_nao_chama_a_rede(adapter, falso):
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(fazer_titulo(valor="2.49"))
    assert falso.chamadas == []


async def test_token_por_escopo_e_reaproveitado(adapter, falso):
    r = await adapter.registrar_titulo(fazer_titulo("a"))
    await adapter.consultar_titulo(r.id_banco)
    await adapter.consultar_titulo(r.id_banco)
    # escrita e leitura: dois escopos, dois tokens, nenhum a mais
    assert falso.tokens_emitidos == 2


async def test_consulta_traz_pix_e_boleto(adapter, falso):
    r = await adapter.registrar_titulo(fazer_titulo())
    c = await adapter.consultar_titulo(r.id_banco)
    assert c.nosso_numero == "0012345"
    assert c.txid == "tx123"
    assert c.valor_pago is None


async def test_resposta_bruta_nao_guarda_pii(adapter, falso):
    r = await adapter.registrar_titulo(fazer_titulo())
    c = await adapter.consultar_titulo(r.id_banco)
    assert "000201" not in str(c.resposta_bruta)
    assert "Maria" not in repr(fazer_titulo())


async def test_status_desconhecido_nao_vira_pago(adapter, falso):
    r = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[r.id_banco]["situacao"] = "INVENTADO"
    with pytest.raises(Validacao):
        await adapter.consultar_titulo(r.id_banco)


async def test_instrucao_nao_suportada(adapter):
    assert not adapter.suporta(Capacidade.ALTERACAO)
    with pytest.raises(NaoSuportado):
        await adapter.enviar_instrucao(
            "x",
            Instrucao(
                chave_idempotencia="i1",
                tipo=TipoInstrucao.PRORROGACAO,
                nova_data_vencimento=date(2027, 1, 10),
            ),
        )


async def test_5xx_e_retentado_ate_esgotar(falso, dormir, dormidas):
    def tratar(request):
        if request.url.path == "/oauth/v2/token":
            return falso.tratar(request)
        return httpx.Response(503, text="fora do ar")

    config = InterConfig(conta="c1", client_id="id", client_secret="s")
    adapter = InterAdapter(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(tratar)),
        dormir=dormir,
    )
    with pytest.raises(Indisponivel) as erro:
        await adapter.consultar_titulo("x")
    assert erro.value.retryable
    assert len(dormidas) == 3


def test_parse_webhook_lista_e_dedup():
    adapter = InterAdapter(InterConfig(conta="c", client_id="i", client_secret="s"))
    corpo = (
        b'[{"codigoSolicitacao":"a","situacao":"PAGO",'
        b'"valorTotalRecebido":10.5,"dataHoraSituacao":"2026-11-10T10:00:00",'
        b'"nome":"Fulano"},'
        b'{"codigoSolicitacao":"b","situacao":"CANCELADO"}]'
    )
    a, b = adapter.parse_webhook(corpo)
    assert a.tipo is StatusCanonico.LIQUIDADO
    assert a.valor_pago == Decimal("10.5")
    assert a.resposta_bruta["nome"] == "***"
    assert b.tipo is StatusCanonico.BAIXADO
    with pytest.raises(Validacao):
        adapter.parse_webhook(b"nao e json")
    with pytest.raises(Validacao):
        adapter.parse_webhook([{"situacao": "PAGO"}])


async def test_pdf_binario_e_invalido(falso, dormir):
    def tratar(request):
        if request.url.path == "/oauth/v2/token":
            return falso.tratar(request)
        if request.url.path.endswith("/bin/pdf"):
            return httpx.Response(200, content=b"%PDF-1.7 binario")
        return httpx.Response(200, json={"pdf": "bm9fcGRm"})

    adapter = InterAdapter(
        InterConfig(conta="c", client_id="i", client_secret="s"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(tratar)),
        dormir=dormir,
    )
    assert (await adapter.obter_pdf("bin")).startswith(b"%PDF")
    with pytest.raises(Validacao):
        await adapter.obter_pdf("outro")


def test_entry_point_registrado():
    assert obter_classe("inter") is InterAdapter
