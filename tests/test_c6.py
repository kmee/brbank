import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
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
from brbank.adapters.c6 import C6Adapter, C6Config, referencia_externa
from tests.c6_falso import PDF, C6Falso
from tests.conftest import fazer_titulo

CHAVE_PIX = "123e4567-e89b-12d3-a456-426614174000"


def _config(**extra) -> C6Config:
    base = {
        "conta": "c1",
        "client_id": "cid",
        "client_secret": "segredo",
        "chave_pix": CHAVE_PIX,
    }
    return C6Config(**{**base, **extra})


@pytest.fixture
def falso():
    return C6Falso()


@pytest.fixture
def adapter(falso, dormir):
    return C6Adapter(_config(), client=falso.cliente(), dormir=dormir)


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("c6") is C6Adapter
    assert C6Adapter.validado_sandbox is False
    assert Capacidade.HIBRIDO in C6Adapter.capacidades
    assert Capacidade.SO_HIBRIDO not in C6Adapter.capacidades
    assert Capacidade.PDF in C6Adapter.capacidades
    assert Capacidade.WEBHOOK in C6Adapter.capacidades
    assert Capacidade.ALTERACAO in C6Adapter.capacidades


def test_referencia_externa_deterministica_e_no_formato():
    ref = referencia_externa("chave-1")
    assert ref == referencia_externa("chave-1")
    assert ref != referencia_externa("chave-2")
    assert len(ref) == 26
    assert ref.isalnum()
    assert ref.upper() == ref
    ulid = "01KP640RNSYXH9G41GR27RTAWP"
    assert referencia_externa(ulid) == ulid


def test_config_recusa_chave_pix_que_nao_e_evp():
    with pytest.raises(Validacao, match="EVP"):
        _config(chave_pix="maria@example.com")
    with pytest.raises(Validacao, match="origem"):
        _config(origem="x" * 21)


def test_carteira_por_ambiente():
    assert _config().carteira_efetiva == "15"
    assert _config(ambiente="sandbox").carteira_efetiva == "21"
    assert _config(carteira="99").carteira_efetiva == "99"
    assert "sandbox" in _config(ambiente="sandbox").url_base


async def test_token_client_credentials_sem_escopo(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    form = falso.ultimo_form
    assert form == {
        "client_id": ["cid"],
        "client_secret": ["segredo"],
        "grant_type": ["client_credentials"],
    }
    assert ("POST", "/v1/auth/") in falso.chamadas
    assert falso.ultimo_cabecalho["authorization"] == "Bearer tok-1"


async def test_cabecalhos_de_software_parceiro(falso, dormir):
    adapter = C6Adapter(
        _config(software_nome="Odoo", software_versao="16.0", origem="ERP"),
        client=falso.cliente(),
        dormir=dormir,
    )
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_cabecalho["partner-software-name"] == "Odoo"
    assert falso.ultimo_cabecalho["partner-software-version"] == "16.0"
    assert falso.ultimo_corpo["origin"] == "ERP"
    await adapter.aclose()


async def test_payload_bolepix(adapter, falso):
    titulo = fazer_titulo(valor="150.005").model_copy(
        update={
            "encargos": Encargos(
                multa_percentual="2",
                juros_mensal_percentual="1",
                desconto_percentual="5",
                desconto_dias_antes=3,
            ),
            "mensagens": ("um", "dois", "tres", "quatro", "cinco"),
            "dias_baixa_automatica": 30,
        }
    )
    registrado = await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["external_reference_id"] == referencia_externa("chave-1")
    assert registrado.id_banco == corpo["external_reference_id"]
    assert corpo["amount"] == 150.01
    assert corpo["due_date"] == "2026-12-10"
    assert corpo["description"] == "FAT-chave-1"
    assert corpo["days_after_due_date"] == 30
    assert corpo["payer"] == {
        "name": "Maria da Silva",
        "tax_id": "52998224725",
        "email": "maria@example.com",
        "address": {
            "address": "Rua das Flores, 10",
            "neighborhood": "Centro",
            "city": "Belo Horizonte",
            "state": "MG",
            "zip_code": "30110000",
        },
    }
    assert corpo["fees"] == {
        "fine_type": "PERCENTAGE",
        "fine_value": 2.0,
        "fine_deadline": 0,
        "interest_type": "MONTHLY_PERCENTAGE",
        "interest_value": 1.0,
        "interest_deadline": 0,
        "discount_type": "MONTHLY_PERCENTAGE",
        "first_discount_value": 5.0,
        "first_discount_deadline": 3,
    }
    boleto = corpo["payment_method"]["bank_slip"]
    assert boleto["billing_scheme"] == "15"
    assert boleto["instructions"] == ["um", "dois", "tres", "quatro"]
    assert "your_number" not in boleto  # seu_numero nao e so digitos
    assert corpo["payment_method"]["pix"] == {"key": CHAVE_PIX, "type": "EVP"}
    assert "beneficiarioFinal" not in corpo
    assert "origin" not in corpo


async def test_registro_devolve_hibrido_sem_pii(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.pix_copia_cola.startswith("000201")
    assert registrado.linha_digitavel
    assert registrado.codigo_barras
    assert registrado.nosso_numero == "10198966"
    assert registrado.seu_numero == "FAT-chave-1"
    bruta = json.dumps(registrado.resposta_bruta)
    assert "Maria" not in bruta
    assert "52998224725" not in bruta
    assert "Rua das Flores" not in bruta


async def test_seu_numero_numerico_vai_como_your_number(adapter, falso):
    titulo = fazer_titulo().model_copy(update={"seu_numero": "0000003048"})
    await adapter.registrar_titulo(titulo)
    bloco = falso.ultimo_corpo["payment_method"]["bank_slip"]
    assert bloco["your_number"] == "0000003048"


async def test_registro_repetido_devolve_a_mesma_cobranca(adapter, falso):
    primeiro = await adapter.registrar_titulo(fazer_titulo())
    segundo = await adapter.registrar_titulo(fazer_titulo())
    assert primeiro.id_banco == segundo.id_banco
    assert len(falso.cobrancas) == 1


async def test_sem_pix_na_resposta_registra_e_avisa(adapter, falso, caplog):
    falso.sem_pix = True
    with caplog.at_level(logging.WARNING, logger="brbank.adapters.c6"):
        registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.pix_copia_cola is None
    assert "sem Pix" in caplog.text
    assert "Maria" not in caplog.text


async def test_abatimento_e_valor_maximo_sao_recusados(adapter):
    titulo = fazer_titulo()
    with pytest.raises(Validacao, match="abatimento"):
        await adapter.registrar_titulo(
            titulo.model_copy(update={"encargos": Encargos(abatimento="1")})
        )
    with pytest.raises(Validacao, match="maximo"):
        await adapter.registrar_titulo(fazer_titulo(valor="5000000.01"))


async def test_pagador_sem_endereco_e_email_longo(adapter):
    titulo = fazer_titulo()
    sem = titulo.pagador.model_copy(update={"endereco": None})
    with pytest.raises(Validacao, match="endereco"):
        await adapter.registrar_titulo(titulo.model_copy(update={"pagador": sem}))
    longo = titulo.pagador.model_copy(update={"email": "a" * 64 + "@example.com"})
    with pytest.raises(Validacao, match="e-mail"):
        await adapter.registrar_titulo(titulo.model_copy(update={"pagador": longo}))


async def test_textos_longos_sao_cortados_em_40(adapter, falso):
    titulo = fazer_titulo()
    nome = "Empresa " + "X" * 60
    pagador = titulo.pagador.model_copy(update={"nome": nome})
    await adapter.registrar_titulo(titulo.model_copy(update={"pagador": pagador}))
    assert len(falso.ultimo_corpo["payer"]["name"]) == 40


async def test_consulta_liquidada_soma_pagamentos_da_listagem(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.50")
    assert consultado.data_pagamento == date(2026, 11, 10)
    assert falso.chamadas[-1] == ("GET", "/v2/bank_slips/list")
    assert falso.ultima_query == {
        "external_reference_id": registrado.id_banco,
        "due_date_from": "2026-12-10",
        "due_date_to": "2026-12-10",
    }


async def test_consulta_usa_pagamentos_quando_vem_na_consulta(dormir):
    falso = C6Falso(pagamentos_na_consulta=True)
    adapter = C6Adapter(_config(), client=falso.cliente(), dormir=dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.valor_pago == Decimal("150.50")
    assert ("GET", "/v2/bank_slips/list") not in falso.chamadas


async def test_aguardando_confirmacao_segue_em_aberto(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[registrado.id_banco]["status"] = "WAITING_CONFIRMATION"
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.valor_pago is None


async def test_status_desconhecido_vira_validacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[registrado.id_banco]["status"] = "EXPIRED"
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_baixa_usa_put_cancel(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.baixar_titulo(registrado.id_banco)
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.chamadas[-1] == (
        "PUT",
        f"/v2/bank_slips/{registrado.id_banco}/cancel",
    )


async def test_instrucao_prorrogacao_faz_patch(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    alterado = await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(
            chave_idempotencia="i1",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=date(2027, 1, 15),
        ),
    )
    assert falso.chamadas[-1] == ("PATCH", f"/v2/bank_slips/{registrado.id_banco}")
    assert falso.ultimo_corpo == {"due_date": "2027-01-15"}
    assert alterado.status is StatusCanonico.REGISTRADO
    assert falso.cobrancas[registrado.id_banco]["due_date"] == "2027-01-15"


async def test_instrucao_baixa_e_invalidas(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixa = await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(chave_idempotencia="b", tipo=TipoInstrucao.BAIXA),
    )
    assert baixa.status is StatusCanonico.BAIXADO
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.PRORROGACAO)
        )
    for tipo in (
        TipoInstrucao.ABATIMENTO,
        TipoInstrucao.PROTESTO,
        TipoInstrucao.NEGATIVACAO,
    ):
        with pytest.raises(NaoSuportado):
            await adapter.enviar_instrucao(
                "1", Instrucao(chave_idempotencia="x", tipo=tipo, valor="1")
            )


async def test_pdf_binario_e_em_json(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert await adapter.obter_pdf(registrado.id_banco) == PDF
    assert falso.ultimo_cabecalho["accept"] == "application/pdf"
    falso.pdf_em_json = True
    assert await adapter.obter_pdf(registrado.id_banco) == PDF


def _notificacao(status: str, information) -> dict:
    return {
        "external_id": "01KP9C34NVB711SM0GT5C0CWVT",
        "date_time": "2026-04-15T20:08:35.152525501Z",
        "client_id": "6abeb8cd-3dda-4f3a-b038-3b55c9d87d6a",
        "partner_id": "01J6W5RJTVB5CV1Z8QAB1K08QG",
        "service": "BANK_SLIP_PIX",
        "status": status,
        "information": information,
    }


def test_webhook_pagamento_com_information_aninhado(adapter):
    interno = json.dumps(
        {
            "id": "01KP9C34NVB711SM0GT5C0CWVT",
            "status": "SUCCESS",
            "payment_date": "2026-04-15",
            "payment_method": "PIX",
            "external_reference_id": "P5IMR8UUDN9O8P2LKBN8EUKXCR",
            "amount": "109.25",
        }
    )
    corpo = json.dumps(_notificacao("PAID", json.dumps({"information": interno})))
    [evento] = adapter.parse_webhook(corpo.encode())
    assert evento.referencia == "P5IMR8UUDN9O8P2LKBN8EUKXCR"
    assert evento.tipo is StatusCanonico.LIQUIDADO
    assert evento.valor_pago == Decimal("109.25")
    assert evento.ocorrido_em == datetime(
        2026, 4, 15, 20, 8, 35, 152525, tzinfo=timezone.utc
    )


def test_webhook_aguardando_e_criado(adapter):
    info = {
        "id": "01KXH8BDPWA2NRFHJF4JX037DZ",
        "payment_date": "2026-07-14",
        "payment_method": "BANK_SLIP",
        "external_reference_id": "CU9D02SEW1Q1OW7KZXSM01MI7B",
        "amount": "1.00",
    }
    eventos = adapter.parse_webhook(
        [
            _notificacao("WAITING_CONFIRMATION", json.dumps(info)),
            _notificacao("CREATED", {**info, "payer": {"name": "Maria"}}),
        ]
    )
    assert [e.tipo for e in eventos] == [StatusCanonico.REGISTRADO] * 2
    assert all(e.valor_pago is None for e in eventos)
    assert "Maria" not in json.dumps(eventos[1].resposta_bruta)


def test_webhook_invalidos(adapter):
    with pytest.raises(Validacao):
        adapter.parse_webhook(b"nao json")
    with pytest.raises(Validacao, match="servico"):
        adapter.parse_webhook({**_notificacao("PAID", "{}"), "service": "BANK_SLIP"})
    with pytest.raises(Validacao, match="external_reference_id"):
        adapter.parse_webhook(_notificacao("PAID", "{}"))
    with pytest.raises(Validacao, match="information"):
        adapter.parse_webhook(_notificacao("PAID", "{nao json"))
    with pytest.raises(Validacao, match="information"):
        adapter.parse_webhook(_notificacao("PAID", None))
    with pytest.raises(Validacao, match="desconhecido"):
        adapter.parse_webhook(
            _notificacao("EXPIRED", {"external_reference_id": "A" * 26})
        )
    with pytest.raises(Validacao, match="formato"):
        adapter.parse_webhook(["texto"])


async def test_referencia_divergente_na_resposta_e_validacao(falso, dormir):
    original = falso.tratar

    def trocado(request):
        resp = original(request)
        if request.method == "POST" and request.url.path == "/v2/bank_slips":
            corpo = resp.json()
            corpo["external_reference_id"] = "Z" * 26
            return httpx.Response(201, json=corpo)
        return resp

    falso.tratar = trocado
    adapter = C6Adapter(_config(), client=falso.cliente(), dormir=dormir)
    with pytest.raises(Validacao, match="diferente"):
        await adapter.registrar_titulo(fazer_titulo())


async def test_liquidado_sem_pagamento_na_listagem_fica_sem_valor(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.cobrancas[registrado.id_banco]["status"] = "PAID"
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago is None
    del falso.cobrancas[registrado.id_banco]["due_date"]
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.valor_pago is None


async def test_pagamento_com_valor_ou_data_invalidos(dormir):
    falso = C6Falso(pagamentos_na_consulta=True)
    adapter = C6Adapter(_config(), client=falso.cliente(), dormir=dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    falso.pagamentos[registrado.id_banco][0]["amount"] = "abc"
    with pytest.raises(Validacao, match="monetario"):
        await adapter.consultar_titulo(registrado.id_banco)
    falso.pagamentos[registrado.id_banco][0]["amount"] = "1.00"
    falso.pagamentos[registrado.id_banco][0]["payment_date"] = "ontem"
    with pytest.raises(Validacao, match="data"):
        await adapter.consultar_titulo(registrado.id_banco)


def _desviar(falso, prefixo, resposta):
    original = falso.tratar

    def tratar(request):
        if request.url.path.startswith(prefixo):
            return resposta
        return original(request)

    falso.tratar = tratar
    return falso.cliente()


@pytest.mark.parametrize(
    ("prefixo", "resposta", "operacao", "erro"),
    [
        ("/v2/", httpx.Response(200, json=[1, 2]), "consultar_titulo", "formato"),
        ("/v2/", httpx.Response(200, content=b"nao json"), "consultar_titulo", "JSON"),
        ("/v2/", httpx.Response(200, content=b"nao json"), "obter_pdf", "JSON"),
        (
            "/v2/",
            httpx.Response(200, json={"base64_pdf_file": "@@@"}),
            "obter_pdf",
            "corrompido",
        ),
        (
            "/v2/",
            httpx.Response(200, json={"base64_pdf_file": "aGVsbG8="}),
            "obter_pdf",
            "nao contem PDF",
        ),
    ],
)
async def test_respostas_fora_do_formato(
    falso, dormir, prefixo, resposta, operacao, erro
):
    adapter = C6Adapter(
        _config(), client=_desviar(falso, prefixo, resposta), dormir=dormir
    )
    with pytest.raises(Validacao, match=erro):
        await getattr(adapter, operacao)("A" * 26)


def test_webhook_data_invalida(adapter):
    with pytest.raises(Validacao, match="date_time"):
        adapter.parse_webhook(
            {
                **_notificacao("PAID", {"external_reference_id": "A" * 26}),
                "date_time": "ontem",
            }
        )


def test_repr_da_config_nao_vaza_segredos():
    texto = repr(_config(certificado_pem=b"cert", chave_pem=b"k"))
    assert "segredo" not in texto
    assert CHAVE_PIX not in texto
    assert "cert" not in texto
