import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from brbank import (
    Capacidade,
    Encargos,
    Instrucao,
    NaoSuportado,
    StatusCanonico,
    TipoInstrucao,
    Validacao,
    obter_classe,
)
from brbank.adapters.btg import BtgAdapter, BtgConfig
from tests.btg_falso import CLIENT_ID, CLIENT_SECRET, REFRESH_INICIAL, BtgFalso
from tests.conftest import CNPJ_VALIDO, fazer_titulo

CNPJ = "11222333000181"


def _config(**extra) -> BtgConfig:
    base = {
        "conta": "c1",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_INICIAL,
        "cnpj_empresa": CNPJ_VALIDO,
        "agencia": "50",
        "conta_corrente": "0030818500",
        "dias_expiracao": 30,
    }
    return BtgConfig(**{**base, **extra})


@pytest.fixture
def falso():
    return BtgFalso()


@pytest.fixture
def adapter(falso, dormir):
    return BtgAdapter(_config(), client=falso.cliente(), dormir=dormir)


def _webhook(evento: str, dados: dict) -> bytes:
    return json.dumps({"webhookId": "w-1", "event": evento, "data": dados}).encode()


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("btg") is BtgAdapter
    assert BtgAdapter.validado_sandbox is False
    for capacidade in (
        Capacidade.HIBRIDO,
        Capacidade.ALTERACAO,
        Capacidade.BAIXA,
        Capacidade.PDF,
        Capacidade.WEBHOOK,
        Capacidade.EMISSAO_ASSINCRONA,
    ):
        assert capacidade in BtgAdapter.capacidades
    assert Capacidade.SO_HIBRIDO not in BtgAdapter.capacidades
    assert Capacidade.PIX_COB not in BtgAdapter.capacidades


async def test_token_por_refresh_token_com_basic(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_form == {
        "grant_type": ["refresh_token"],
        "refresh_token": [REFRESH_INICIAL],
    }
    assert falso.ultimo_cabecalho_token["authorization"].startswith("Basic ")
    assert falso.ultimo_cabecalho["authorization"] == "Bearer tok-1"
    assert "x-response" not in falso.ultimo_cabecalho
    assert falso.chamadas[-1] == ("POST", f"/{CNPJ}/banking/collections")


async def test_refresh_token_rotacionado_vai_para_o_callback(falso, dormir):
    recebidos: list[str] = []
    adapter = BtgAdapter(
        _config(ao_renovar_refresh_token=recebidos.append),
        client=falso.cliente(),
        dormir=dormir,
    )
    await adapter.registrar_titulo(fazer_titulo())
    assert recebidos == ["rt-1"]
    # Token expirado no banco: o 401 forca renovar com o refresh_token NOVO.
    falso.tokens_emitidos += 1
    await adapter.consultar_titulo("col-1")
    assert falso.ultimo_form["refresh_token"] == ["rt-1"]
    assert recebidos == ["rt-1", "rt-3"]


async def test_callback_assincrono_e_aguardado(falso, dormir):
    recebidos: list[str] = []

    async def guardar(novo: str) -> None:
        recebidos.append(novo)

    adapter = BtgAdapter(
        _config(ao_renovar_refresh_token=guardar), client=falso.cliente(), dormir=dormir
    )
    await adapter.registrar_titulo(fazer_titulo())
    assert recebidos == ["rt-1"]


async def test_refresh_sem_rotacao_nao_chama_callback(dormir):
    falso = BtgFalso(rotacionar_refresh=False)
    recebidos: list[str] = []
    adapter = BtgAdapter(
        _config(ao_renovar_refresh_token=recebidos.append),
        client=falso.cliente(),
        dormir=dormir,
    )
    await adapter.registrar_titulo(fazer_titulo())
    assert recebidos == []


async def test_refresh_recusado_e_validacao(falso, dormir):
    adapter = BtgAdapter(
        _config(refresh_token="vencido"), client=falso.cliente(), dormir=dormir
    )
    with pytest.raises(Validacao, match="credenciais"):
        await adapter.registrar_titulo(fazer_titulo())


async def test_sandbox_usa_hosts_e_header_do_wiremock(falso, dormir):
    adapter = BtgAdapter(
        _config(ambiente="sandbox"), client=falso.cliente(), dormir=dormir
    )
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_cabecalho["x-response"] == "success"
    assert adapter._config.url_base == "https://api.sandbox.empresas.btgpactual.com"
    assert adapter._config.url_token == "https://id.sandbox.btgpactual.com/oauth2/token"


async def test_payload_com_encargos(adapter, falso):
    titulo = fazer_titulo(valor="150.005").model_copy(
        update={
            "encargos": Encargos(
                multa_percentual="2",
                juros_mensal_percentual="1",
                desconto_percentual="5",
                desconto_dias_antes=3,
                abatimento="1.50",
            ),
            "mensagens": ("linha um", "linha dois"),
        }
    )
    await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["type"] == "BANKSLIP_QRCODE"
    assert corpo["amount"] == 150.01
    assert corpo["dueDate"] == "2026-12-10"
    assert corpo["overDueDate"] == "2027-01-09"
    assert corpo["description"] == "linha um linha dois"
    assert corpo["account"] == {"number": "0030818500", "branch": "50"}
    assert corpo["detail"] == {"documentNumber": "FAT-chave-1"}
    assert corpo["payer"] == {
        "name": "Maria da Silva",
        "taxId": "52998224725",
        "personType": "F",
        "email": "maria@example.com",
        "address": {
            "street": "Rua das Flores",
            "number": "10",
            "neighborhood": "Centro",
            "city": "Belo Horizonte",
            "state": "MG",
            "zipCode": "30110000",
        },
    }
    assert corpo["interest"] == {
        "type": "PERCENTAGE_PER_MONTH",
        "value": 1.0,
        "startDate": "2026-12-11",
    }
    assert corpo["fine"] == {
        "type": "PERCENTAGE",
        "value": 2.0,
        "startDate": "2026-12-11",
    }
    assert corpo["discounts"] == [
        {"type": "PERCENTAGE", "value": 5.0, "limitDate": "2026-12-07"}
    ]
    assert corpo["rebate"] == {"type": "FIXED_VALUE", "value": 1.5}


async def test_sem_encargos_e_boleto_simples(falso, dormir):
    adapter = BtgAdapter(_config(hibrido=False), client=falso.cliente(), dormir=dormir)
    await adapter.registrar_titulo(fazer_titulo())
    corpo = falso.ultimo_corpo
    assert corpo["type"] == "BANKSLIP"
    assert corpo["interest"] == {"type": "NOT_APPLICABLE", "value": 0}
    assert corpo["fine"] == {"type": "NOT_APPLICABLE", "value": 0}
    assert "discounts" not in corpo
    assert "rebate" not in corpo
    assert "description" not in corpo
    consultado = await adapter.consultar_titulo("col-1")
    assert consultado.pix_copia_cola is None


async def test_pagador_juridico_endereco_cortado_e_sem_endereco(adapter, falso):
    titulo = fazer_titulo()
    endereco = titulo.pagador.endereco.model_copy(
        update={"bairro": "Jardim Paulista Novo Horizonte", "complemento": "Sala 2"}
    )
    pj = titulo.pagador.model_copy(
        update={"documento": CNPJ, "endereco": endereco, "email": None}
    )
    await adapter.registrar_titulo(titulo.model_copy(update={"pagador": pj}))
    pagador = falso.ultimo_corpo["payer"]
    assert pagador["personType"] == "J"
    assert "email" not in pagador
    assert pagador["address"]["neighborhood"] == "Jardim Paulista Novo"
    assert pagador["address"]["complement"] == "Sala 2"

    sem = titulo.pagador.model_copy(update={"endereco": None})
    await adapter.registrar_titulo(
        fazer_titulo("c2").model_copy(update={"pagador": sem})
    )
    assert "address" not in falso.ultimo_corpo["payer"]


async def test_dias_baixa_automatica_define_expiracao(adapter, falso):
    titulo = fazer_titulo().model_copy(update={"dias_baixa_automatica": 5})
    await adapter.registrar_titulo(titulo)
    assert falso.ultimo_corpo["overDueDate"] == "2026-12-15"


async def test_recusas_antes_de_chamar_o_banco(falso, dormir):
    adapter = BtgAdapter(
        _config(dias_expiracao=None), client=falso.cliente(), dormir=dormir
    )
    with pytest.raises(Validacao, match="overDueDate"):
        await adapter.registrar_titulo(fazer_titulo())
    longo = fazer_titulo().model_copy(
        update={"seu_numero": "X" * 16, "dias_baixa_automatica": 1}
    )
    with pytest.raises(Validacao, match="documentNumber"):
        await adapter.registrar_titulo(longo)
    verboso = fazer_titulo().model_copy(
        update={"mensagens": ("a" * 200, "b" * 200), "dias_baixa_automatica": 1}
    )
    with pytest.raises(Validacao, match="300"):
        await adapter.registrar_titulo(verboso)
    assert falso.chamadas == []


async def test_registro_assincrono_e_consulta(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.id_banco == "col-1"
    assert registrado.status is StatusCanonico.PENDENTE_REGISTRO
    assert registrado.seu_numero == "FAT-chave-1"
    assert registrado.linha_digitavel is None

    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert falso.chamadas[-1] == ("GET", f"/{CNPJ}/banking/collections/col-1")
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.nosso_numero == "00001"
    assert consultado.linha_digitavel
    assert consultado.codigo_barras
    assert consultado.pix_copia_cola
    assert consultado.valor_pago is None


async def test_resposta_bruta_sem_pii(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    texto = json.dumps(consultado.resposta_bruta, default=str)
    assert "Maria" not in texto
    assert "52998224725" not in texto
    assert "Flores" not in texto
    assert "maria@example.com" not in texto


async def test_liquidacao_le_valor_e_data(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco, valor=151.37)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("151.37")
    assert consultado.data_pagamento == date(2026, 12, 9)


@pytest.mark.parametrize(
    ("situacao", "esperado"),
    [
        ("OVERDUE", StatusCanonico.REGISTRADO),
        ("FAILED", StatusCanonico.REJEITADO),
        ("PROCESSING", StatusCanonico.PENDENTE_REGISTRO),
    ],
)
async def test_tabela_de_status(adapter, falso, situacao, esperado):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[registrado.id_banco]["status"] = situacao
    assert (await adapter.consultar_titulo(registrado.id_banco)).status is esperado


@pytest.mark.parametrize("situacao", ["EXPIRED", "CANCELING", "RETURNED", None])
async def test_status_fora_da_tabela_vira_validacao(adapter, falso, situacao):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[registrado.id_banco]["status"] = situacao
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_prorrogacao_reenvia_o_titulo_inteiro(adapter, falso):
    titulo = fazer_titulo().model_copy(
        update={"encargos": Encargos(multa_percentual="2", abatimento="3")}
    )
    registrado = await adapter.registrar_titulo(titulo)
    resultado = await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(
            chave_idempotencia="i1",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=date(2027, 1, 15),
        ),
    )
    assert falso.chamadas[-1] == ("PUT", f"/{CNPJ}/banking/collections/col-1")
    corpo = falso.ultimo_corpo
    assert corpo["dueDate"] == "2027-01-15"
    assert corpo["overDueDate"] == "2027-02-14"
    assert corpo["amount"] == 150.0
    assert corpo["fine"]["startDate"] == "2027-01-16"
    assert corpo["interest"] == {"type": "NOT_APPLICABLE", "value": 0}
    assert corpo["discounts"] == [{"type": "NOT_APPLICABLE", "value": 0}]
    assert resultado.status is StatusCanonico.REGISTRADO
    assert falso.cobrancas["col-1"]["dueDate"] == "2027-01-15"


async def test_abatimento_protesto_e_baixa(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    id_banco = registrado.id_banco
    await adapter.enviar_instrucao(
        id_banco,
        Instrucao(chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor="2.5"),
    )
    assert falso.ultimo_corpo["rebate"] == {"type": "FIXED_VALUE", "value": 2.5}
    for tipo in (TipoInstrucao.PROTESTO, TipoInstrucao.NEGATIVACAO):
        with pytest.raises(NaoSuportado):
            await adapter.enviar_instrucao(
                id_banco, Instrucao(chave_idempotencia="p", tipo=tipo)
            )
    baixado = await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="b", tipo=TipoInstrucao.BAIXA)
    )
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.chamadas[-1] == ("DELETE", f"/{CNPJ}/banking/collections/col-1")


async def test_instrucoes_sem_dado_obrigatorio(adapter):
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.PRORROGACAO)
        )
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.ABATIMENTO)
        )


async def test_alteracao_sem_dados_na_consulta_e_validacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    del falso.cobrancas[registrado.id_banco]["overDueDate"]
    with pytest.raises(Validacao, match="overDueDate"):
        await adapter.enviar_instrucao(
            registrado.id_banco,
            Instrucao(chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor="1"),
        )


async def test_id_banco_vai_escapado_na_url(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    with pytest.raises(Validacao):
        await adapter.consultar_titulo("../outra")
    assert falso.chamadas[-1][1].endswith("/collections/..%2Foutra")


async def test_pdf(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert await adapter.obter_pdf(registrado.id_banco) == b"%PDF-1.4 falso"
    assert falso.ultimo_cabecalho["accept"] == "application/pdf"
    falso.pdf = b"<html>erro</html>"
    with pytest.raises(Validacao, match="PDF"):
        await adapter.obter_pdf(registrado.id_banco)


def test_webhook_pago(adapter):
    corpo = _webhook(
        "collections.paid",
        {
            "collectionId": "col-9",
            "status": "PAID",
            "amountPaid": 9200,
            "paidAt": "2025-08-08",
            "updatedAt": "2025-08-08T18:34:32.000Z",
            "payer": {"name": "NOME", "taxId": "01324124321464"},
        },
    )
    [evento] = adapter.parse_webhook(corpo)
    assert evento.chave_dedup == ("btg", "col-9", StatusCanonico.LIQUIDADO)
    assert evento.valor_pago == Decimal("9200")
    assert evento.ocorrido_em == datetime(2025, 8, 8, 18, 34, 32, tzinfo=timezone.utc)
    assert "01324124321464" not in json.dumps(evento.resposta_bruta, default=str)


def test_webhook_cancelado_nas_duas_grafias(adapter):
    for evento in ("collections.cancelled", "colections.cancelled"):
        [item] = adapter.parse_webhook(_webhook(evento, {"collectionId": "c"}))
        assert item.tipo is StatusCanonico.BAIXADO
        assert item.valor_pago is None


def test_webhook_recusas(adapter):
    with pytest.raises(Validacao, match="desconhecido"):
        adapter.parse_webhook(_webhook("collections.expired", {"collectionId": "c"}))
    with pytest.raises(Validacao, match="desconhecido"):
        adapter.parse_webhook(_webhook("transactions.debit", {"collectionId": "c"}))
    with pytest.raises(Validacao, match="collectionId"):
        adapter.parse_webhook(_webhook("collections.paid", {}))
    with pytest.raises(Validacao, match="JSON"):
        adapter.parse_webhook(b"nao e json")
    with pytest.raises(Validacao, match="data do evento"):
        adapter.parse_webhook(
            _webhook("collections.paid", {"collectionId": "c", "updatedAt": "ontem"})
        )


def test_webhook_confere_segredo(falso):
    adapter = BtgAdapter(_config(webhook_secret="s3gredo"), client=falso.cliente())
    corpo = _webhook("collections.created", {"collectionId": "c"})
    [evento] = adapter.parse_webhook(corpo, {"Authorization": "Bearer s3gredo"})
    assert evento.tipo is StatusCanonico.REGISTRADO
    with pytest.raises(Validacao, match="segredo"):
        adapter.parse_webhook(corpo, {"Authorization": "Bearer outro"})
    with pytest.raises(Validacao, match="segredo"):
        adapter.parse_webhook(corpo)


def test_config_invalida():
    with pytest.raises(Validacao, match="ambiente"):
        _config(ambiente="homologacao")
    with pytest.raises(Validacao, match="CNPJ"):
        _config(cnpj_empresa="11.222.333/0001-00")
    with pytest.raises(Validacao, match="negativo"):
        _config(dias_expiracao=-1)
    assert _config(cnpj_empresa="11.222.333/0001-81").cnpj_empresa == CNPJ


def test_repr_nao_vaza_segredos():
    texto = repr(_config(webhook_secret="wh-segredo"))
    for segredo in (CLIENT_SECRET, REFRESH_INICIAL, "wh-segredo"):
        assert segredo not in texto
