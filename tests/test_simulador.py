# ruff: noqa: ASYNC210
# (chamadas bloqueantes ao admin do simulador local sao intencionais)
"""O simulador de verdade numa porta livre, com o `BtgAdapter` como oraculo."""

import json
import socket
import subprocess
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from brbank import StatusDda, TipoEventoDda, Validacao, normalizar_linha_digitavel
from brbank.adapters.btg import BtgAdapter, gerar_pkce, trocar_codigo, url_autorizacao
from brbank.documento import cnpj_valido
from brbank.simulador import Simulador
from brbank.simulador.dados import (
    BENEFICIARIOS,
    CNPJ_DEMO,
    SEMENTE_DEMO,
    cnpj_com_dv,
    vencimento_da_semente,
)
from tests.dda_util import Receptor, admin, config_para

HOJE = date(2026, 10, 26)
OUTRO_CNPJ = cnpj_com_dv("450000990001")  # sem DDA no simulador


@pytest.fixture
def sim():
    with Simulador(hoje=HOJE, semente="demo") as servidor:
        yield servidor


@pytest.fixture
def vazio():
    with Simulador(hoje=HOJE) as servidor:
        yield servidor


@pytest.fixture
async def adapter(sim):
    adapter = BtgAdapter(config_para(sim))
    yield adapter
    await adapter.aclose()


async def _todos(adapter: BtgAdapter, cnpj: str = CNPJ_DEMO, **filtros):
    itens, cursor = [], None
    while True:
        pagina = await adapter.listar_debitos(cnpj, cursor=cursor, **filtros)
        itens += pagina.itens
        cursor = pagina.cursor_proximo
        if cursor is None:
            return itens


def test_semente_tem_doze_debitos_validos():
    assert len(SEMENTE_DEMO) == 12
    assert [i.ordem for i in SEMENTE_DEMO] == list(range(1, 13))
    for quem in BENEFICIARIOS.values():
        assert cnpj_valido(quem.cnpj)
    assert cnpj_valido(CNPJ_DEMO)


def test_docs_do_simulador_listam_a_semente():
    """O modulo Odoo de demo casa faturas com estes dados: a doc nao pode divergir."""
    docs = Path(__file__).parent.parent / "docs" / "simulador.md"
    texto = docs.read_text()
    for item in SEMENTE_DEMO:
        quem = BENEFICIARIOS[item.beneficiario]
        assert quem.cnpj in texto
        assert quem.razao_social in texto
        assert f"{item.valor:.2f}" in texto
    assert CNPJ_DEMO in texto


async def test_semente_demo_pelo_adapter(adapter, sim):
    adapter.tamanho_pagina_dda = 5
    itens = await _todos(adapter)
    assert len(itens) == 12
    assert len({d.id_banco for d in itens}) == 12
    assert len({d.linha_digitavel for d in itens}) == 12
    for d in itens:
        assert normalizar_linha_digitavel(d.linha_digitavel) == d.linha_digitavel
        assert d.codigo_barras and len(d.codigo_barras) == 44
        assert cnpj_valido(d.beneficiario_documento)
        assert d.cnpj_pagador == CNPJ_DEMO
        assert d.status is StatusDda.ABERTO
    por_valor = {(d.beneficiario_documento, d.valor, d.vencimento) for d in itens}
    for item in SEMENTE_DEMO:
        quem = BENEFICIARIOS[item.beneficiario]
        vencimento = vencimento_da_semente(item, HOJE)
        assert (quem.cnpj, item.valor, vencimento) in por_valor
    # o simulador marca o vencido como OVERDUE e o adapter o mantem aberto
    vencido = next(d for d in itens if d.vencimento < HOJE)
    assert vencido.resposta_bruta["status"] == "OVERDUE"
    assert vencido.status is StatusDda.ABERTO


async def test_paginacao_cobre_tudo_sem_repetir(adapter):
    for tamanho in (1, 5, 12, 50):
        adapter.tamanho_pagina_dda = tamanho
        itens = await _todos(adapter)
        assert len(itens) == len({d.id_banco for d in itens}) == 12
    adapter.tamanho_pagina_dda = 5
    primeira = await adapter.listar_debitos(CNPJ_DEMO)
    assert len(primeira.itens) == 5 and primeira.cursor_proximo == "2"
    ultima = await adapter.listar_debitos(CNPJ_DEMO, cursor="3")
    assert len(ultima.itens) == 2 and ultima.cursor_proximo is None


async def test_filtro_desde(adapter):
    corte = vencimento_da_semente(SEMENTE_DEMO[0], HOJE)
    itens = await _todos(adapter, desde=corte)
    assert itens and all(d.vencimento >= corte for d in itens)
    assert len(itens) < 12


async def test_resumo_bate_com_a_semente(adapter):
    resumo = await adapter.resumo_debitos(CNPJ_DEMO)
    assert resumo.quantidade == 12
    assert resumo.valor_total == sum((i.valor for i in SEMENTE_DEMO), Decimal(0))
    assert resumo.por_status == {StatusDda.ABERTO: 12}


async def test_ativar_primeiro_depois_listar(vazio):
    adapter = BtgAdapter(config_para(vazio))
    try:
        with pytest.raises(Validacao) as info:
            await adapter.listar_debitos(OUTRO_CNPJ)
        assert info.value.status_http == 403
        await adapter.ativar_dda(OUTRO_CNPJ)
        assert (await adapter.listar_debitos(OUTRO_CNPJ)).itens == ()
        await adapter.desativar_dda(OUTRO_CNPJ)
        with pytest.raises(Validacao):
            await adapter.resumo_debitos(OUTRO_CNPJ)
    finally:
        await adapter.aclose()


async def test_ocultar_e_mostrar(adapter):
    [primeiro, *_] = await _todos(adapter)
    oculto = await adapter.alterar_debito(CNPJ_DEMO, primeiro.id_banco, oculto=True)
    assert oculto.resposta_bruta["hidden"] is True
    assert primeiro.id_banco not in {d.id_banco for d in await _todos(adapter)}
    assert (await adapter.resumo_debitos(CNPJ_DEMO)).quantidade == 11
    await adapter.alterar_debito(CNPJ_DEMO, primeiro.id_banco, oculto=False)
    assert len(await _todos(adapter)) == 12
    with pytest.raises(Validacao):
        await adapter.alterar_debito(CNPJ_DEMO, "nao-existe", oculto=True)


async def test_admin_cria_debito_com_linha_valida(vazio):
    adapter = BtgAdapter(config_para(vazio))
    try:
        await adapter.ativar_dda(CNPJ_DEMO)
        criado = admin(
            vazio,
            "POST",
            "/debitos",
            {"cnpj_pagador": CNPJ_DEMO, "valor": "321.45", "vencimento": "2026-11-10"},
        )
        assert criado["webhook"] == {"enviado": False}
        [debito] = await _todos(adapter)
        assert debito.id_banco == criado["id"]
        assert debito.valor == Decimal("321.45")
        assert debito.vencimento == date(2026, 11, 10)
        assert cnpj_valido(debito.beneficiario_documento)
        assert normalizar_linha_digitavel(debito.linha_digitavel)
    finally:
        await adapter.aclose()


async def test_webhooks_ponta_a_ponta(vazio):
    receptor = Receptor()
    adapter = BtgAdapter(config_para(vazio, webhook_secret="s3gredo"))
    try:
        admin(vazio, "POST", "/webhook", {"url": receptor.url, "segredo": "s3gredo"})
        await adapter.ativar_dda(CNPJ_DEMO)  # dispara `enabled`
        criado = admin(
            vazio,
            "POST",
            "/debitos",
            {
                "cnpj_pagador": CNPJ_DEMO,
                "valor": "700.00",
                "vencimento": "2026-11-20",
                "beneficiario_nome": "FORNECEDOR TESTE LTDA",
            },
        )
        assert criado["webhook"]["status_http"] == 200
        admin(vazio, "POST", f"/debitos/{criado['id']}/pagar")
        admin(vazio, "POST", f"/debitos/{criado['id']}/reenviar")
        await adapter.desativar_dda(CNPJ_DEMO)

        assert len(receptor.recebidos) == 5
        eventos = []
        for cabecalhos, corpo in receptor.recebidos:
            assert cabecalhos["Authorization"] == "Bearer s3gredo"
            eventos += adapter.parse_webhook_dda(corpo, cabecalhos)
            assert adapter.parse_webhook(corpo, cabecalhos) == []
        tipos = [e.tipo for e in eventos]
        assert tipos == [
            TipoEventoDda.DDA_ATIVADO,
            TipoEventoDda.DEBITO_NOVO,
            TipoEventoDda.DEBITO_PAGO,
            TipoEventoDda.DEBITO_PAGO,
            TipoEventoDda.DDA_DESATIVADO,
        ]
        novo, pago = eventos[1], eventos[2]
        assert novo.cnpj_pagador == CNPJ_DEMO
        assert novo.debitos[0].id_banco == criado["id"]
        assert novo.debitos[0].beneficiario_nome == "FORNECEDOR TESTE LTDA"
        assert novo.debitos[0].valor == Decimal("700.00")
        assert novo.debitos[0].vencimento == date(2026, 11, 20)
        # o pago traz a data sentinela: vencimento desconhecido, nunca 0001-01-01
        assert pago.debitos[0].status is StatusDda.PAGO
        assert pago.debitos[0].vencimento is None
        # reenvio identico: mesma chave de dedup
        assert eventos[2].chave_dedup == eventos[3].chave_dedup
        assert eventos[1].chave_dedup != eventos[2].chave_dedup
        estado = admin(vazio, "GET", "/estado")
        assert [e["status_http"] for e in estado["eventos"]] == [200] * 5
    finally:
        await adapter.aclose()
        receptor.parar()


async def test_admin_desativa_e_reativa_o_dda_com_webhook(sim):
    receptor = Receptor()
    adapter = BtgAdapter(config_para(sim))
    try:
        admin(sim, "POST", "/webhook", {"url": receptor.url})
        resposta = admin(sim, "POST", f"/dda/{CNPJ_DEMO}/desativar")
        assert resposta["status"] == "DISABLED"
        assert resposta["webhook"]["status_http"] == 200
        # O banco desligou: a listagem recusa, como no BTG real.
        with pytest.raises(Exception):  # noqa: B017
            await _todos(adapter)
        estado = admin(sim, "GET", "/estado")
        assert estado["dda"][CNPJ_DEMO]["status"] == "DISABLED"
        resposta = admin(sim, "POST", f"/dda/{CNPJ_DEMO}/ativar")
        assert resposta["status"] == "ENABLED"
        assert len(await _todos(adapter)) == 12
        eventos = []
        for cabecalhos, corpo in receptor.recebidos:
            eventos += adapter.parse_webhook_dda(corpo, cabecalhos)
        assert [e.tipo for e in eventos] == [
            TipoEventoDda.DDA_DESATIVADO,
            TipoEventoDda.DDA_ATIVADO,
        ]
        assert {e.cnpj_pagador for e in eventos} == {CNPJ_DEMO}
        # CNPJ invalido e acao desconhecida
        for caminho in ("/dda/123/desativar", f"/dda/{CNPJ_DEMO}/explodir"):
            resp = httpx.post(f"{sim.url}/_sim{caminho}", timeout=10)
            assert resp.status_code in (400, 404)
    finally:
        await adapter.aclose()
        receptor.parar()


async def test_webhook_com_destino_fora_do_ar_nao_derruba_o_simulador(vazio):
    receptor = Receptor(status=500)
    admin(vazio, "POST", "/webhook", {"url": receptor.url})
    adapter = BtgAdapter(config_para(vazio))
    try:
        await adapter.ativar_dda(CNPJ_DEMO)
        criado = admin(vazio, "POST", "/debitos", {"cnpj_pagador": CNPJ_DEMO})
        assert criado["webhook"] == {"enviado": True, "status_http": 500}
        receptor.parar()
        criado = admin(vazio, "POST", "/debitos", {"cnpj_pagador": CNPJ_DEMO})
        assert criado["webhook"]["enviado"] is False
        assert "erro" in criado["webhook"]
        assert len(await _todos(adapter)) == 2
    finally:
        await adapter.aclose()


async def test_cancelar_some_da_listagem(adapter, sim):
    [primeiro, *_] = await _todos(adapter)
    assert admin(sim, "POST", f"/debitos/{primeiro.id_banco}/cancelar")["cancelado"]
    assert primeiro.id_banco not in {d.id_banco for d in await _todos(adapter)}
    estado = admin(sim, "GET", "/estado")
    cancelados = [d for d in estado["debitos"] if d["cancelado"]]
    assert [d["id"] for d in cancelados] == [primeiro.id_banco]


async def test_reset_volta_a_semente(adapter, sim):
    admin(sim, "POST", "/debitos", {"cnpj_pagador": CNPJ_DEMO})
    assert len(await _todos(adapter)) == 13
    admin(sim, "POST", "/reset")
    assert len(await _todos(adapter)) == 12
    admin(sim, "POST", "/reset", {"vazio": True})
    estado = admin(sim, "GET", "/estado")
    assert estado["debitos"] == [] and estado["dda"] == {}
    admin(sim, "POST", "/reset")
    assert len(await _todos(adapter)) == 12


async def test_token_revogado_renova_sozinho(adapter, sim):
    await adapter.resumo_debitos(CNPJ_DEMO)
    assert admin(sim, "GET", "/estado")["renovacoes_token"] == 1
    admin(sim, "POST", "/revogar-tokens")
    assert (await adapter.resumo_debitos(CNPJ_DEMO)).quantidade == 12
    assert admin(sim, "GET", "/estado")["renovacoes_token"] == 2


async def test_sem_token_e_401():
    with Simulador(hoje=HOJE, semente="demo") as sim:
        resposta = httpx.get(f"{sim.url}/{CNPJ_DEMO}/banking/direct-debit/debits")
        assert resposta.status_code == 401
        assert resposta.json()["errors"][0]["code"] == "unauthorized"


async def test_x_response_error_no_sandbox(sim):
    adapter = BtgAdapter(config_para(sim, ambiente="sandbox", resposta_sandbox="error"))
    try:
        with pytest.raises(Validacao) as info:
            await adapter.resumo_debitos(CNPJ_DEMO)
        assert info.value.status_http == 400
    finally:
        await adapter.aclose()


async def test_rota_desconhecida_e_json_invalido(sim):
    assert httpx.get(f"{sim.url}/nada").status_code == 404
    resposta = httpx.post(f"{sim.url}/_sim/debitos", content=b"{nao json", timeout=10)
    assert resposta.status_code == 400
    assert httpx.post(f"{sim.url}/_sim/debitos", json={}).status_code == 400
    assert httpx.post(f"{sim.url}/_sim/debitos/x/pagar").status_code == 404


# --- OAuth ----------------------------------------------------------------------


async def test_oauth_authorization_code_com_pkce(vazio):
    config = config_para(vazio)
    verificador, desafio = gerar_pkce()
    url = url_autorizacao(
        config, "https://odoo.test/cb", "estado-1", desafio, config.escopos_dda
    )
    assert url.startswith(f"{vazio.url}/oauth2/authorize?")
    resposta = httpx.get(url, follow_redirects=False)
    assert resposta.status_code == 302
    destino = urlsplit(resposta.headers["location"])
    consulta = parse_qs(destino.query)
    assert (
        f"{destino.scheme}://{destino.netloc}{destino.path}" == "https://odoo.test/cb"
    )
    assert consulta["state"] == ["estado-1"]

    tokens = await trocar_codigo(
        config, consulta["code"][0], "https://odoo.test/cb", verificador
    )
    assert set(tokens) == {"refresh_token", "access_token", "expires_in"}
    assert tokens["expires_in"] == 3600
    # o codigo vale uma vez so
    with pytest.raises(Validacao):
        await trocar_codigo(
            config, consulta["code"][0], "https://odoo.test/cb", verificador
        )

    # o refresh token novo move a biblioteca
    adapter = BtgAdapter(
        config_para(vazio, refresh_token=tokens["refresh_token"]),
    )
    try:
        await adapter.ativar_dda(CNPJ_DEMO)
        assert (await adapter.listar_debitos(CNPJ_DEMO)).itens == ()
    finally:
        await adapter.aclose()


async def test_oauth_pkce_errado_e_pedido_sem_pkce(vazio):
    config = config_para(vazio)
    _, desafio = gerar_pkce()
    url = url_autorizacao(config, "https://odoo.test/cb", "s", desafio, "x")
    codigo = parse_qs(
        urlsplit(httpx.get(url, follow_redirects=False).headers["location"]).query
    )["code"][0]
    with pytest.raises(Validacao) as info:
        await trocar_codigo(
            config, codigo, "https://odoo.test/cb", "verificador-errado"
        )
    assert info.value.status_http == 400
    sem_pkce = (
        f"{vazio.url}/oauth2/authorize?response_type=code"
        f"&client_id={config.client_id}&redirect_uri=x&state=s"
    )
    assert httpx.get(sem_pkce, follow_redirects=False).status_code == 400
    outro = f"{vazio.url}/oauth2/authorize?client_id=outro&response_type=code"
    assert httpx.get(outro, follow_redirects=False).status_code == 400


async def test_oauth_cliente_errado_e_refresh_rotacionado():
    with Simulador(hoje=HOJE, semente="demo", rotacionar_refresh=True) as sim:
        ruim = config_para(sim, client_secret="errado")
        with pytest.raises(Validacao):
            await trocar_codigo(ruim, "c", "https://x", "v")
        recebidos: list[str] = []
        adapter = BtgAdapter(
            config_para(sim, ao_renovar_refresh_token=recebidos.append)
        )
        try:
            await adapter.resumo_debitos(CNPJ_DEMO)
            admin(sim, "POST", "/revogar-tokens")
            await adapter.resumo_debitos(CNPJ_DEMO)
        finally:
            await adapter.aclose()
        assert recebidos == ["sim-refresh-1", "sim-refresh-2"]
        # o refresh token inicial morreu na primeira rotacao
        antigo = BtgAdapter(config_para(sim))
        try:
            with pytest.raises(Validacao):
                await antigo.resumo_debitos(CNPJ_DEMO)
        finally:
            await antigo.aclose()


# --- linha de comando -------------------------------------------------------------


def test_python_m_brbank_simulador():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    processo = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "brbank.simulador",
            "--porta",
            str(porta),
            "--semente",
            "demo",
            "--hoje",
            "2026-10-26",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        estado = None
        for _ in range(100):
            try:
                estado = httpx.get(
                    f"http://127.0.0.1:{porta}/_sim/estado", timeout=1
                ).json()
                break
            except httpx.TransportError:
                time.sleep(0.1)
        assert estado is not None
        assert len(estado["debitos"]) == 12
        assert estado["hoje"] == "2026-10-26"
        assert json.dumps(estado)  # serializavel
    finally:
        processo.terminate()
        processo.wait(timeout=10)
