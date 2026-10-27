"""DDA do BtgAdapter: webhooks com os exemplos da ficha tecnica do BTG e as rotas
com MockTransport (a conversa com o servidor completo esta em test_simulador)."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from brbank import CapacidadeDda, NaoSuportado, StatusDda, TipoEventoDda, Validacao
from brbank.adapters.btg import BtgAdapter, BtgConfig
from brbank.dda import AdapterDda
from brbank.simulador.dados import montar_linha_digitavel
from tests.conftest import CNPJ_VALIDO

CNPJ = "11222333000181"
LINHA = montar_linha_digitavel(
    banco="208", valor=Decimal("24193.61"), vencimento=date(2026, 12, 1), semente="x"
)


def _config(**extra) -> BtgConfig:
    base = {
        "conta": "c1",
        "client_id": "app",
        "client_secret": "seg",
        "refresh_token": "rt-0",
        "cnpj_empresa": CNPJ_VALIDO,
        "agencia": "50",
        "conta_corrente": "0030818500",
    }
    return BtgAdapter(BtgConfig(**{**base, **extra}), client=_cliente_mudo())


def _cliente_mudo() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))
    )


def _envelope(evento: str, dados: dict, **extra) -> bytes:
    return json.dumps(
        {"webhookId": "w-1", "event": evento, "data": dados, **extra}
    ).encode()


# Exemplos da ficha tecnica "Banking - DDA" (com a linha digitavel trocada por uma
# valida: a da ficha tem DV errado, e o adapter nao exige DV do banco).
ADDED = {
    "directDebitId": "00000000-7779-4f25-bec0-ebb3391589be",
    "owner": {"taxId": CNPJ, "name": "NOME EMPRESA"},
    "status": "ENABLED",
    "debits": [
        {
            "id": "00000000-0000-0000-0000-c35ba9199d96",
            "amount": 24193.61,
            "dueDate": "2023-03-16",
            "expirationDate": "2024-03-16",
            "digitableLine": LINHA,
            "status": "CREATED",
            "payee": {
                "taxId": "30306294000145",
                "socialName": "Emiss_o Boleto BTG",
                "fantasyName": "Emiss_o Boleto BTG",
                "bank": {"code": "208", "name": "BANCO BTG PACTUAL S.A."},
            },
        },
        {
            "id": "00000000-0000-0000-0000-c35ba9199d97",
            "amount": 24193.61,
            "dueDate": "2023-03-16",
            "expirationDate": "2024-03-16",
            "digitableLine": LINHA,
            "status": "CREATED",
            "payee": {
                "taxId": "30306294000145",
                "socialName": "Emiss_o Boleto BTG",
                "fantasyName": "Emiss_o Boleto BTG",
                "bank": {"code": "208", "name": "BANCO BTG PACTUAL S.A."},
            },
        },
    ],
}
PAID = {
    "directDebitId": "00000000-0000-0000-0000-ebb3391589be",
    "owner": {"taxId": "00000000000001", "name": "NOME EMPRESA"},
    "status": "ENABLED",
    "debits": [
        {
            "id": "00000000-0000-0000-0000-c35ba9199d97",
            "amount": 4948.1,
            "dueDate": "0001-01-01",
            "expirationDate": "0001-01-01",
            # a linha da ficha (47 digitos, DV errado) tem de passar
            "digitableLine": "20800000000000000000000000000000000000000004800",
            "status": "PAID",
            "payee": {
                "taxId": "00000000000001",
                "socialName": "SOCIAL NAME",
                "fantasyName": "NAME",
                "bank": {"code": "208", "name": "BANCO BTG PACTUAL S.A."},
            },
        }
    ],
}
DISABLED = {
    "directDebitId": "00000000-8bcf-488b-85f9-8b3a041e3eef",
    "owner": {"taxId": "30306294000145", "name": "NOME EMPRESA"},
    "status": "DISABLED",
}
ENABLED = {
    "accountId": "37297902000141-208-50-003886850",
    "date": "2022-03-02T22:01:55.274Z",
    "creditDebitIndicator": "DEBIT",
    "amount": 30900,
    "currency": "BRL",
    "transactionId": "33449743",
}


def test_capacidades_e_contrato():
    assert isinstance(_config(), AdapterDda)
    for cap in CapacidadeDda:
        if cap is not CapacidadeDda.NENHUMA and cap.name:
            assert BtgAdapter.capacidades_dda & cap
    assert _config().suporta_dda(CapacidadeDda.WEBHOOK)
    assert BtgAdapter.validado_sandbox is False


def test_debit_added_traz_os_boletos_sem_reconsulta():
    adapter = _config()
    [evento] = adapter.parse_webhook_dda(
        _envelope("authorized-direct-debits.debit-added", ADDED)
    )
    assert evento.tipo is TipoEventoDda.DEBITO_NOVO
    assert evento.cnpj_pagador == CNPJ
    assert len(evento.debitos) == 2
    primeiro = evento.debitos[0]
    assert primeiro.id_banco.endswith("c35ba9199d96")
    assert primeiro.valor == Decimal("24193.61")
    assert primeiro.vencimento == date(2023, 3, 16)
    assert primeiro.data_limite == date(2024, 3, 16)
    assert primeiro.status is StatusDda.ABERTO
    assert primeiro.linha_digitavel == LINHA
    assert primeiro.codigo_barras is not None and len(primeiro.codigo_barras) == 44
    assert primeiro.beneficiario_documento == "30306294000145"
    assert primeiro.beneficiario_nome == "Emiss_o Boleto BTG"
    assert primeiro.banco_emissor == "208"
    assert primeiro.cnpj_pagador == CNPJ
    # id do envelope ausente: hash estavel do corpo, igual no reenvio
    [de_novo] = adapter.parse_webhook_dda(
        _envelope("authorized-direct-debits.debit-added", ADDED)
    )
    assert de_novo.chave_dedup == evento.chave_dedup
    assert evento.chave_dedup[0] == "btg"


def test_eventos_diferentes_tem_ids_diferentes():
    adapter = _config()
    [um] = adapter.parse_webhook_dda(
        _envelope("authorized-direct-debits.debit-added", ADDED)
    )
    [dois] = adapter.parse_webhook_dda(
        _envelope("authorized-direct-debits.debit-paid", ADDED)
    )
    assert um.id_evento != dois.id_evento


def test_id_do_envelope_vira_chave_de_dedup_quando_existe():
    [evento] = _config().parse_webhook_dda(
        _envelope("authorized-direct-debits.debit-added", ADDED, eventId="ev-77")
    )
    assert evento.chave_dedup == ("btg", "ev-77")


def test_debit_paid_com_data_sentinela_vira_none_e_status_pago():
    [evento] = _config().parse_webhook_dda(
        _envelope("authorized-direct-debits.debit-paid", PAID)
    )
    assert evento.tipo is TipoEventoDda.DEBITO_PAGO
    [debito] = evento.debitos
    assert debito.vencimento is None
    assert debito.data_limite is None
    assert debito.status is StatusDda.PAGO
    assert debito.valor == Decimal("4948.10")
    # a linha com DV errado da ficha nao derruba o evento, mas nao vira codigo de barras
    assert debito.codigo_barras is None


def test_enabled_e_disabled():
    adapter = _config()
    [ligado] = adapter.parse_webhook_dda(
        _envelope("authorized-direct-debits.enabled", ENABLED)
    )
    assert ligado.tipo is TipoEventoDda.DDA_ATIVADO
    assert ligado.debitos == ()
    # sem owner: o CNPJ vem do prefixo do accountId
    assert ligado.cnpj_pagador == "37297902000141"
    assert ligado.ocorrido_em == datetime(
        2022, 3, 2, 22, 1, 55, 274000, tzinfo=timezone.utc
    )
    [desligado] = adapter.parse_webhook_dda(
        _envelope("authorized-direct-debits.disabled", DISABLED)
    )
    assert desligado.tipo is TipoEventoDda.DDA_DESATIVADO
    assert desligado.cnpj_pagador == "30306294000145"


def test_aceita_o_prefixo_completo_e_lista():
    corpo = json.dumps(
        [
            {
                "event": "empresas.btgpactual.com/authorized-direct-debits.debit-added",
                "data": ADDED,
            },
            {"event": "collections.paid", "data": {"collectionId": "c"}},
            {"event": "authorized-direct-debits.algo-novo", "data": {}},
            {"event": "authorized-direct-debits.disabled", "data": DISABLED},
        ]
    )
    eventos = _config().parse_webhook_dda(corpo)
    assert [e.tipo for e in eventos] == [
        TipoEventoDda.DEBITO_NOVO,
        TipoEventoDda.DDA_DESATIVADO,
    ]


def test_eventos_de_cobranca_e_outros_viram_lista_vazia():
    adapter = _config()
    assert (
        adapter.parse_webhook_dda(_envelope("collections.paid", {"collectionId": "c"}))
        == []
    )
    assert adapter.parse_webhook_dda(_envelope("transactions.x", {})) == []
    assert adapter.parse_webhook_dda([]) == []


def test_resposta_bruta_sem_pii_e_repr_limpo():
    [evento] = _config().parse_webhook_dda(
        _envelope("authorized-direct-debits.debit-added", ADDED)
    )
    texto = json.dumps(evento.resposta_bruta, default=str) + repr(evento)
    for pii in ("NOME EMPRESA", "Emiss_o", "30306294000145"):
        assert pii not in texto
    # a linha digitavel nao e dado pessoal (fica na bruta), mas sai do repr
    assert LINHA not in repr(evento)
    assert evento.resposta_bruta["data"]["owner"] == {"taxId": "***", "name": "***"}


def test_webhook_dda_confere_o_segredo():
    adapter = _config(webhook_secret="s3gredo")
    corpo = _envelope("authorized-direct-debits.disabled", DISABLED)
    [_] = adapter.parse_webhook_dda(corpo, {"Authorization": "Bearer s3gredo"})
    for cab in (None, {"Authorization": "Bearer outro"}):
        with pytest.raises(Validacao, match="segredo"):
            adapter.parse_webhook_dda(corpo, cab)


@pytest.mark.parametrize(
    "corpo, trecho",
    [
        (b"nao e json", "JSON"),
        (_envelope("authorized-direct-debits.debit-added", None), "sem data"),
        (
            _envelope(
                "authorized-direct-debits.debit-added",
                {**ADDED, "debits": [{**ADDED["debits"][0], "status": "WAT"}]},
            ),
            "desconhecido",
        ),
        (
            _envelope(
                "authorized-direct-debits.debit-added",
                {**ADDED, "debits": [{**ADDED["debits"][0], "digitableLine": "123"}]},
            ),
            "ilegivel",
        ),
        (
            _envelope(
                "authorized-direct-debits.debit-added",
                {**ADDED, "debits": [{**ADDED["debits"][0], "amount": 0}]},
            ),
            "ilegivel",
        ),
        (
            _envelope("authorized-direct-debits.debit-added", {**ADDED, "owner": {}}),
            "CNPJ do pagador",
        ),
        (
            _envelope("authorized-direct-debits.enabled", {"date": "ontem"}),
            "data do evento",
        ),
    ],
)
def test_webhook_dda_recusas(corpo, trecho):
    with pytest.raises(Validacao, match=trecho) as info:
        _config().parse_webhook_dda(corpo)
    # a mensagem nunca ecoa dado do boleto
    assert LINHA not in str(info.value)


# --- rotas (MockTransport) ----------------------------------------------------


class _Banco:
    def __init__(self, *respostas: httpx.Response) -> None:
        self.respostas = list(respostas)
        self.chamadas: list[httpx.Request] = []

    def tratar(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        self.chamadas.append(request)
        return self.respostas.pop(0)


def _adapter(banco: _Banco, **extra) -> BtgAdapter:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(banco.tratar))
    config = BtgConfig(
        conta="c1",
        client_id="a",
        client_secret="b",
        refresh_token="r",
        cnpj_empresa=CNPJ_VALIDO,
        agencia="1",
        conta_corrente="1",
        url_base_override="http://banco.test",
        url_token_override="http://banco.test/oauth2/token",
        **extra,
    )
    return BtgAdapter(config, client=cliente)


def _item(identificador: str, **extra) -> dict:
    return {
        "id": identificador,
        "amount": 100.5,
        "dueDate": "2026-12-01T00:00:00.000Z",
        "expirationDate": "2027-01-01T00:00:00.000Z",
        "digitableLine": LINHA,
        "payee": {
            "document": "45000001000156",
            "fantasyName": "ALFA",
            "socialName": "ALFA LTDA",
            "bankCode": "341",
            "bankName": "ITAU",
        },
        "hidden": False,
        "status": "OVERDUE",
        **extra,
    }


async def test_ativar_e_desativar_usam_a_rota_e_conferem_o_status():
    ativo = {"data": {"id": "d", "status": "ENABLED"}}
    inativo = {"data": {"id": "d", "status": "DISABLED"}}
    banco = _Banco(httpx.Response(200, json=ativo), httpx.Response(200, json=inativo))
    adapter = _adapter(banco)
    await adapter.ativar_dda("11.222.333/0001-81")
    await adapter.desativar_dda(CNPJ)
    assert [(c.method, c.url.path) for c in banco.chamadas] == [
        ("POST", f"/{CNPJ}/banking/direct-debit"),
        ("DELETE", f"/{CNPJ}/banking/direct-debit"),
    ]
    assert banco.chamadas[0].headers["authorization"] == "Bearer t"
    banco = _Banco(httpx.Response(200, json=inativo))
    with pytest.raises(Validacao, match="DISABLED"):
        await _adapter(banco).ativar_dda(CNPJ)


async def test_ativar_rejeita_cnpj_invalido_sem_chamar_o_banco():
    banco = _Banco()
    with pytest.raises(Validacao, match="CNPJ"):
        await _adapter(banco).ativar_dda("123")
    assert banco.chamadas == []


async def test_listar_pagina_curta_e_a_ultima_e_cheia_com_link_continua():
    banco = _Banco(
        httpx.Response(
            200,
            json={"data": [_item("a"), _item("b")], "_links": {"next": {"href": "x"}}},
        ),
        httpx.Response(
            200, json={"data": [_item("c")], "_links": {"next": {"href": "x"}}}
        ),
    )
    adapter = _adapter(banco)
    adapter.tamanho_pagina_dda = 2
    um = await adapter.listar_debitos(CNPJ, desde=date(2026, 11, 1))
    assert [d.id_banco for d in um.itens] == ["a", "b"]
    assert um.cursor_proximo == "2"
    dois = await adapter.listar_debitos(CNPJ, cursor=um.cursor_proximo)
    assert [d.id_banco for d in dois.itens] == ["c"]
    assert dois.cursor_proximo is None
    consulta = dict(banco.chamadas[0].url.params)
    assert consulta == {"pageNumber": "1", "pageSize": "2", "minDueDate": "2026-11-01"}
    assert banco.chamadas[1].url.params["pageNumber"] == "2"
    primeiro = um.itens[0]
    assert primeiro.valor == Decimal("100.50")
    assert primeiro.vencimento == date(2026, 12, 1)
    assert primeiro.status is StatusDda.ABERTO
    assert primeiro.resposta_bruta["status"] == "OVERDUE"


async def test_listar_cursor_invalido_e_resposta_ruim():
    adapter = _adapter(_Banco(httpx.Response(200, json={"x": 1})))
    for cursor in ("abc", "0", "-3"):
        with pytest.raises(Validacao, match="cursor"):
            await adapter.listar_debitos(CNPJ, cursor=cursor)
    with pytest.raises(Validacao, match="sem data"):
        await adapter.listar_debitos(CNPJ)


async def test_resumo_mapeia_contagens():
    bloco = lambda n, v: {"count": n, "amount": v}  # noqa: E731
    banco = _Banco(
        httpx.Response(
            200,
            json={
                "data": {
                    "total": bloco(5, 1000.25),
                    "totalOverdue": bloco(1, 100),
                    "totalCreated": bloco(3, 700.25),
                    "totalPendingApproval": bloco(1, 200),
                    "totalScheduled": bloco(0, 0),
                }
            },
        )
    )
    resumo = await _adapter(banco).resumo_debitos(CNPJ)
    assert resumo.quantidade == 5
    assert resumo.valor_total == Decimal("1000.25")
    assert resumo.por_status == {StatusDda.ABERTO: 5}
    assert banco.chamadas[0].url.path == f"/{CNPJ}/banking/direct-debit/debits/summary"


async def test_alterar_debito_so_oculta_e_mostra():
    banco = _Banco(httpx.Response(200, json={"data": _item("a", hidden=True)}))
    adapter = _adapter(banco)
    debito = await adapter.alterar_debito(CNPJ, "a", oculto=True)
    assert debito.resposta_bruta["hidden"] is True
    chamada = banco.chamadas[0]
    assert chamada.method == "PATCH"
    assert json.loads(chamada.content) == {"items": [{"id": "a", "hidden": True}]}
    with pytest.raises(NaoSuportado, match="oculto"):
        await adapter.alterar_debito(CNPJ, "a", vencimento=date.today())
    with pytest.raises(Validacao, match="oculto"):
        await adapter.alterar_debito(CNPJ, "a")


async def test_sandbox_manda_x_response():
    banco = _Banco(httpx.Response(200, json={"data": {"status": "ENABLED"}}))
    await _adapter(banco, ambiente="sandbox").ativar_dda(CNPJ)
    assert banco.chamadas[0].headers["x-response"] == "success"


async def test_erros_do_banco_viram_erros_canonicos():
    adapter = _adapter(
        _Banco(httpx.Response(403, json={"errors": [{"detail": "sem DDA"}]}))
    )
    with pytest.raises(Validacao) as info:
        await adapter.listar_debitos(CNPJ)
    assert info.value.status_http == 403
