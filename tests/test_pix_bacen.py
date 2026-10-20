from dataclasses import replace
from datetime import date
from decimal import Decimal

import httpx
import pytest

from brbank import (
    Capacidade,
    Encargos,
    NaoSuportado,
    Orquestrador,
    StatusCanonico,
    Validacao,
    criar_adapter,
)
from brbank.adapters.pix_bacen import (
    PERFIS,
    PixBacenAdapter,
    PixBacenConfig,
    gerar_txid,
    obter_perfil,
)
from tests.conftest import fazer_titulo
from tests.pix_falso import PixFalso


def montar(nome="inter", ambiente="producao", dormir=None, **extra):
    falso = PixFalso(PERFIS[nome], ambiente)
    dados = {
        "perfil": nome,
        "conta": "c1",
        "chave_pix": "chave@kmee.com.br",
        "client_id": "id",
        "client_secret": "segredo",
        "ambiente": ambiente,
    }
    dados.update(extra)
    adapter = PixBacenAdapter(
        PixBacenConfig(**dados), client=falso.cliente(), dormir=dormir
    )
    return adapter, falso


def test_so_o_perfil_sicoob_validado_em_sandbox():
    assert set(PERFIS) == {
        "inter",
        "bb",
        "sicoob",
        "sicredi",
        "itau",
        "bradesco",
        "santander",
        "efi",
    }
    assert {n for n, p in PERFIS.items() if p.validado_sandbox} == {"sicoob"}


def test_bnb_sem_perfil():
    with pytest.raises(Validacao, match="BNB"):
        obter_perfil("bnb")


def test_registro_por_entry_point():
    adapter = criar_adapter(
        "pix_bacen",
        PixBacenConfig(
            perfil="inter",
            conta="c",
            chave_pix="k",
            client_id="i",
            client_secret="s",
            certificado_pem=None,
        ),
        client=httpx.AsyncClient(),
    )
    assert adapter.banco == "pix_inter"
    assert adapter.suporta(Capacidade.PIX_COBV)
    assert not adapter.suporta(Capacidade.PDF)


def test_perfil_exige_mtls_sem_certificado():
    with pytest.raises(Validacao, match="mTLS"):
        PixBacenAdapter(
            PixBacenConfig(
                perfil="inter",
                conta="c",
                chave_pix="k",
                client_id="i",
                client_secret="s",
            )
        )


def test_config_sem_segredo_no_repr():
    config = PixBacenConfig(
        perfil="inter", conta="c", chave_pix="minha@chave", client_secret="XYZ"
    )
    assert "XYZ" not in repr(config)
    assert "minha@chave" not in repr(config)


def test_txid_deterministico_e_valido():
    txid = gerar_txid("c1", "chave-1")
    assert txid == gerar_txid("c1", "chave-1")
    assert txid != gerar_txid("c2", "chave-1")
    assert txid.isalnum() and 26 <= len(txid) <= 35


async def test_registro_cobv_com_encargos_e_resultado_pix():
    adapter, falso = montar()
    titulo = fazer_titulo().model_copy(
        update={
            "encargos": Encargos(
                multa_percentual=Decimal("2"),
                juros_mensal_percentual=Decimal("1"),
                desconto_percentual=Decimal("5"),
                desconto_dias_antes=3,
                abatimento=Decimal("1.50"),
            ),
            "dias_baixa_automatica": 30,
            "mensagens": ("pague em dia",),
        }
    )
    registrado = await adapter.registrar_titulo(titulo)
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.txid == registrado.id_banco == gerar_txid("c1", "chave-1")
    assert registrado.pix_copia_cola.startswith("000201")
    assert registrado.seu_numero == "FAT-chave-1"
    corpo = falso.ultimo_corpo
    assert corpo["chave"] == "chave@kmee.com.br"
    assert corpo["calendario"] == {
        "dataDeVencimento": "2026-12-10",
        "validadeAposVencimento": 30,
    }
    assert corpo["valor"]["original"] == "150.00"
    assert corpo["valor"]["multa"] == {"modalidade": 2, "valorPerc": "2.00"}
    assert corpo["valor"]["juros"] == {"modalidade": 3, "valorPerc": "1.00"}
    assert corpo["valor"]["abatimento"] == {"modalidade": 1, "valorPerc": "1.50"}
    assert corpo["valor"]["desconto"] == {
        "modalidade": 2,
        "descontoDataFixa": [{"data": "2026-12-07", "valorPerc": "5.00"}],
    }
    assert corpo["devedor"]["cpf"] == "52998224725"
    assert corpo["devedor"]["cep"] == "30110000"
    assert {"nome": "Mensagem 1", "valor": "pague em dia"} in corpo["infoAdicionais"]
    # Copia-e-cola e dados do pagador nao vazam na resposta bruta guardada.
    assert registrado.resposta_bruta["pixCopiaECola"] == "***"
    assert "chave@kmee" not in repr(registrado.resposta_bruta)


async def test_cob_imediata_recusa_encargos_e_usa_expiracao():
    adapter, falso = montar(modalidade="cob", expiracao_s=600)
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["calendario"] == {"expiracao": 600}
    # Sandbox do Sicoob (2026-10-20): cob com endereco ou e-mail do devedor e 400.
    assert falso.ultimo_corpo["devedor"] == {
        "cpf": "52998224725",
        "nome": "Maria da Silva",
    }
    assert ("cob", gerar_txid("c1", "chave-1")) in falso.cobrancas
    titulo = fazer_titulo("c2").model_copy(
        update={"encargos": Encargos(multa_percentual=Decimal("2"))}
    )
    with pytest.raises(Validacao, match="cobv"):
        await adapter.registrar_titulo(titulo)


async def test_falso_recusa_cob_com_devedor_completo():
    _, falso = montar(modalidade="cob")
    async with falso.cliente() as cliente:
        resp = await cliente.put(
            f"https://x{falso.base}/cob/{gerar_txid('c1', 'k')}",
            json={
                "calendario": {"expiracao": 60},
                "valor": {"original": "1.00"},
                "chave": "k",
                "devedor": {"cpf": "52998224725", "nome": "M", "email": "m@x.com"},
            },
            headers={"authorization": "Bearer t"},
        )
    assert resp.status_code == 400


async def test_id_e_o_txid_pedido_mesmo_se_o_psp_devolver_outro():
    adapter, falso = montar("sicoob")
    original = falso._resposta

    def outro_txid(cob, status=200):
        return original({**cob, "txid": "OutroTxidAleatorioDoSandbox01"}, status)

    falso._resposta = outro_txid
    registrado = await adapter.registrar_titulo(fazer_titulo())
    esperado = gerar_txid("c1", "chave-1")
    assert registrado.id_banco == registrado.txid == esperado
    consultado = await adapter.consultar_titulo(esperado)
    assert consultado.id_banco == esperado


async def test_registro_repetido_vira_consulta():
    adapter, falso = montar()
    primeiro = await adapter.registrar_titulo(fazer_titulo())
    segundo = await adapter.registrar_titulo(fazer_titulo())
    assert segundo.id_banco == primeiro.id_banco
    assert len(falso.cobrancas) == 1


async def test_consulta_acha_titulo_da_outra_modalidade():
    emissor, falso = montar(modalidade="cob")
    registrado = await emissor.registrar_titulo(fazer_titulo())
    consultor = PixBacenAdapter(
        PixBacenConfig(
            perfil="inter",
            conta="c1",
            chave_pix="k",
            client_id="i",
            client_secret="s",
        ),
        client=falso.cliente(),
    )
    assert (await consultor.consultar_titulo(registrado.id_banco)).status is (
        StatusCanonico.REGISTRADO
    )


async def test_liquidacao_soma_pix_e_data():
    adapter, falso = montar()
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.00")
    assert consultado.data_pagamento == date(2026, 11, 10)


async def test_concluida_sem_pix_nao_liquida():
    adapter, falso = montar()
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[("cobv", registrado.id_banco)]["status"] = "CONCLUIDA"
    with pytest.raises(Validacao, match="sem Pix"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_status_desconhecido_nao_vira_pago():
    adapter, falso = montar()
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[("cobv", registrado.id_banco)]["status"] = "NOVO_STATUS"
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_baixar_manda_patch_e_devolve_baixado():
    adapter, falso = montar()
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.baixar_titulo(registrado.id_banco)
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.requisicoes[-1].method == "PATCH"


async def test_instrucao_nao_suportada():
    adapter, _ = montar()
    with pytest.raises(NaoSuportado):
        await adapter.enviar_instrucao("x", None)


# --- autenticacao e quirks por perfil --------------------------------------


async def test_inter_form_com_escopos():
    adapter, falso = montar("inter")
    await adapter.registrar_titulo(fazer_titulo())
    corpo = falso.corpo_token()
    assert corpo["client_id"] == "id" and corpo["client_secret"] == "segredo"
    assert "cobv.write" in corpo["scope"] and "webhook.read" in corpo["scope"]
    assert falso.basic() is None


async def test_bb_basic_e_app_key_na_query():
    adapter, falso = montar("bb", app_key="k1", id_parceiro="11222333000181")
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.basic() == "id:segredo"
    assert "client_secret" not in falso.corpo_token()
    req = falso.requisicoes[-1]
    assert req.url.params["gw-dev-app-key"] == "k1"
    assert req.headers["x-bb-portal-devx-cnpj-parceiro"] == "11222333000181"


async def test_sicoob_so_client_id_header_e_alias_brcode():
    adapter, falso = montar("sicoob")
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert "client_secret" not in falso.corpo_token()
    assert falso.requisicoes[-1].headers["client_id"] == "id"
    assert registrado.pix_copia_cola.startswith("000201")


async def test_sicoob_sandbox_usa_token_estatico():
    adapter, falso = montar("sicoob", "sandbox", token_estatico="portal-tok")
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.pedidos_token == []
    assert falso.requisicoes[-1].headers["authorization"] == "Bearer portal-tok"
    with pytest.raises(Validacao, match="token_estatico"):
        montar("sicoob", "sandbox")


async def test_sicredi_basic_com_escopo():
    adapter, falso = montar("sicredi")
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.basic() == "id:segredo"
    assert "cob.write" in falso.corpo_token()["scope"]


async def test_itau_sem_escopo_e_correlacao_unica():
    adapter, falso = montar("itau")
    await adapter.registrar_titulo(fazer_titulo())
    await adapter.consultar_titulo(gerar_txid("c1", "chave-1"))
    assert "scope" not in falso.corpo_token()
    ids = {r.headers["x-correlationID"] for r in falso.requisicoes[1:]}
    assert len(ids) == 2


async def test_bradesco_e_efi_http1_basic():
    for nome in ("bradesco", "efi"):
        adapter, falso = montar(nome)
        await adapter.registrar_titulo(fazer_titulo())
        assert PERFIS[nome].http1
        assert falso.basic() == "id:segredo"
        assert "scope" not in falso.corpo_token()
    assert falso.corpo_token() == {"grant_type": "client_credentials"}  # efi: JSON


async def test_efi_header_de_parceiro():
    adapter, falso = montar("efi", id_parceiro="parceiro-1")
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.requisicoes[-1].headers["x-partner-token"] == "parceiro-1"


async def test_santander_sandbox_cria_por_post_e_baixa_em_v2_na_producao():
    adapter, falso = montar("santander", "sandbox")
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert falso.requisicoes[-1].method == "POST"
    assert "scope" not in falso.corpo_token()
    await adapter.baixar_titulo(registrado.id_banco)
    assert "/api/v1/sandbox/" in str(falso.requisicoes[-1].url)

    perfil = replace(PERFIS["santander"], url_producao="https://x.test/api/v1")
    falso = PixFalso(perfil)
    prod = PixBacenAdapter(
        PixBacenConfig(
            perfil=perfil, conta="c", chave_pix="k", client_id="i", client_secret="s"
        ),
        client=falso.cliente(),
    )
    await prod.registrar_titulo(fazer_titulo())
    assert falso.requisicoes[-1].method == "PUT"
    assert PERFIS["santander"].url_revisao.endswith("/api/v2")


async def test_token_fica_em_cache():
    adapter, falso = montar()
    await adapter.registrar_titulo(fazer_titulo())
    await adapter.consultar_titulo(gerar_txid("c1", "chave-1"))
    assert falso.tokens_emitidos == 1  # cache por (conta, escopo)


# --- webhook ----------------------------------------------------------------


def test_parse_webhook_gera_evento_por_txid_e_dedup():
    adapter, _ = montar()
    corpo = {
        "pix": [
            {
                "endToEndId": "E1",
                "txid": "abc",
                "valor": "10.00",
                "horario": "2026-11-10T12:30:00.000Z",
                "infoPagador": "Maria",
            },
            {"endToEndId": "E1", "txid": "abc", "valor": "10.00"},
            {"endToEndId": "E2", "valor": "5.00"},
        ]
    }
    eventos = adapter.parse_webhook(corpo)
    assert len(eventos) == 1
    evento = eventos[0]
    assert evento.chave_dedup == ("pix_inter", "abc", StatusCanonico.LIQUIDADO)
    assert evento.valor_pago == Decimal("10.00")
    assert evento.ocorrido_em.year == 2026
    assert "Maria" not in repr(evento.resposta_bruta)


@pytest.mark.parametrize(
    "corpo", [b"nao-json", {"outro": 1}, {"pix": [{"txid": "a"}]}, [1]]
)
def test_parse_webhook_malformado(corpo):
    adapter, _ = montar()
    with pytest.raises(Validacao):
        adapter.parse_webhook(corpo)


async def test_webhook_configurar_consultar_remover():
    adapter, falso = montar()
    assert await adapter.consultar_webhook() is None
    await adapter.configurar_webhook("https://erp.kmee.com.br/pix")
    assert falso.webhooks == {"chave@kmee.com.br": "https://erp.kmee.com.br/pix"}
    assert await adapter.consultar_webhook() == "https://erp.kmee.com.br/pix"
    await adapter.remover_webhook()
    assert falso.webhooks == {}
    with pytest.raises(Validacao, match="HTTPS"):
        await adapter.configurar_webhook("http://inseguro")


# --- devolucao e orquestrador -------------------------------------------------


async def test_devolucao():
    adapter, falso = montar()
    dev = await adapter.devolver_pix("E123", "dev1", Decimal("10"), "estorno")
    assert dev.valor == Decimal("10.00") and dev.status == "EM_PROCESSAMENTO"
    assert (await adapter.consultar_devolucao("E123", "dev1")).rtr_id == "D1"
    assert falso.devolucoes["dev1"]["valor"] == "10.00"


async def test_orquestrador_emite_com_o_adapter():
    adapter, falso = montar()
    orq = Orquestrador(adapter)
    a = await orq.emitir(fazer_titulo())
    b = await orq.emitir(fazer_titulo())
    assert a == b and len(falso.cobrancas) == 1
