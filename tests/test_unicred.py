"""Adapter Unicred contra o servidor falso e os vetores oficiais do webhook."""

from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from brbank import (
    Capacidade,
    Encargos,
    Endereco,
    Indisponivel,
    Instrucao,
    NaoSuportado,
    Pessoa,
    StatusCanonico,
    TipoInstrucao,
    Validacao,
    obter_classe,
)
from brbank.adapters.unicred import (
    TABELA_STATUS,
    UnicredAdapter,
    UnicredConfig,
    decifrar_webhook,
)
from brbank.transporte import PoliticaRetry
from tests.conftest import CNPJ_VALIDO, endereco, fazer_titulo
from tests.unicred_falso import (
    API_KEY,
    BASE,
    BENEFICIARIO,
    CHAVE_AUT,
    CHAVE_CRIPTO,
    UnicredFalso,
    cifrar_webhook,
    webhook_liquidacao,
)

# Vetores publicados na pagina "Retorno de Titulos de Cobranca por Webhook" do
# portal Unicred Developers (dados ficticios da propria Unicred).
VETOR_AUT = "f4aecbe7-5061-4522-a1d0-26fc74a920a0"
VETOR_CRIPTO = "3410d803-bdb4-463a-bd5b-d46ddaaba37b"
VETOR_TEXTO = "9liYoy9rGzjnzN0VQDGIxHtUzM7+D+Tveu2MRZrSkxk="
HOMOLOG_AUT = "dbd283c7-c6bc-4135-88bc-f9d30c679c2c"
HOMOLOG_CRIPTO = "af81d220-a717-46f2-9371-18be3777f87b"
CENARIO_REMESSA = (
    "r2ARrolPWr8eEjkodmd2BqiiSHBH2HeN6Y+AHu8awm5S2HwggkQKcSQTkAbv8khFCR+j81amunT3"
    "ELgokXUUQnXsaxqhGOTvjZiWv928V8RCziI5ybQ2kuQCgjNiW33Z2oUgDVHlr2XqI+4zM4Hdjopw"
    "MfBvvwK+uQRkeOzY1718oqX6h2rbTWHtfyLqmCR9kl4SfO6wwhO0Z5wLLm1d+32Eh17PsouDcc3Y"
    "58kiy64BR3635ZdpCUIIDp2jdLPXWiu6FGWUFLAHSPtqsI5AHMT7KLIAbVxCVSa9oSA8WjGG9VWK"
    "YlZmy4tryS59kpCjw+WxfArhBG2xZtrHhOa290XDoK8S660CpsY+dBikN9j4mzV/XJ02pBeXlpz3"
    "1pSu41atW9V73C6DDyNEjJeVdkAapaD00KT5WC8I9fwKZ5J/u6ZxBxkor1f4LkkYr1svcwpXlqEX"
    "q4ZYB5l0JcaO7MRQmwhayZ4daUqeRw0ZRMcnC1d6Dv+1tw26apWhwTgjBoMsvoqdhZByCZ3b3uSJ"
    "4CsSqYzk5Rzcw68r7vD5zlknyo2cr8IHVNiS0oMPFadYU/txj4iTybRRjXhdXTbbyS/XeAM2r2EG"
    "MHvxfwYGmqvRJ/LZ6cmt91rNdSCp1cIz95uvNvTIarap9wSQ06Sbzxya4doIr0CdPEkj7GuKGSN3"
    "/llPoBtE7/+ro0U9Yw5my/7xJNWfmEhMZmEmgP9272susCl6wu9NzrJrT/+HHI4ZodVydcPWc3Oy"
    "aB1E2AaearVIhTaxqcggR80iG3N4vikDh/+JEJKTGktrcAsnkeablW1yezKnf2fz+Pw/PqDs1ac9"
    "gLS7jMPiSuwfgUuP5n8zbGBcJZ1mtMWeZn9qZLgMc/Ml0mA59PbsfMOW24Jb6kPKZgMWk4LdGDVq"
    "Lnhawx8ekLVgqAOAFZaAKeq61s+0rqE98EGKszHNcvaaWpZRQ3XsC2jZRw8YuR8zvPhju0guy6oK"
    "QtDLWOygN2kL1w7J3qMkYH8qCbJ+HAh595nqGfcvKusSqgp/0tkh34IX2gjT5rcaPUDXHkZ0b4Uy"
    "zY6GwzCPOx5u/ijagETF/G1TVr8gCsT+AIOamFB1YWBp/qJ8OT6o1UJy0Si5pJFRbQRCHIgraMwP"
    "ajHcq+HiaTLqWeLuFX2RBQRTxppnRJFkkEuvADi0Lbe2GIGivaPmbh/CHwzyyFyE7Y++Qy2SNd8M"
    "JxhKxdtCqykoOe8Puq+qbSYF8tSgap3pQqDMx/AC637mKe+mh2ESdPFlgzWkt/ZxHkQigxpXeaiA"
    "hArvFxTEBw=="
)
CENARIO_LIQUIDACAO = (
    "VJj+m0ATExUKcDkyn2ynXqlmI9d5G2AL+J+9ExHhoBqVwlCocN+oCnXjL2EhHy1XHoXef+BV8ilI"
    "9NKzwsHi2KgHHah0CGU0MqSQlSb6FnbxSOWZRbZHOSfWY42sHzuRWowj2bzD4b0zq9eGv52jtYJY"
    "K7pD5IOGEgolU8QbsXwG+S93jVo3iCKYg/+WWtK6AYtdym0uL9aRjsN690LnyODw5ZG2J7P16ywl"
    "UAByPrehq56z3OnPC06CY9J8AFgu5aeL1SzHghjLIP17wDxPX2UOCVSJjoT59/eaTBPyJwenfkUw"
    "/xgequLxpnAj7vL9mynaf+lyoeV22m7KoUARM/WZtPoieNnmPEAhbM8/qkvJPjjnZK1j9p/L/6Be"
    "J99ChkvCUI8qIjxWKEPKjIPTWH3aF0o3yfckYgdTBd4xJ4I6nglYvnvsaaZbwLscBjDkozJ0WyLA"
    "buUlUQIbRDwYO0V4yGKMpSVNbi8x5EXCM0cCU8gsJUwoBDefmJ7RmdoqG4RMUx8xBDeo5ALHO6D3"
    "EsPm3P0JRH+SeRB5eu8Y3ANDm2swxrDPIsoNs2awGadoBIROw/MtXsgEyntq1wOMK0KW8lpZ/vAP"
    "TSci/R3GDYiLr2Fo9BkmO9c6C28Nzs1jEo12HdLVG0urUjt630wgv0Xo6e4zOeJFRMtm+DlEbWyT"
    "Mqm+ozolb9G+CFmxNxAAr+fk/MC+ix1giF5vGk4R/1XWfBZwL7/1koUO+umy7FpjEXF+fBcr9tMg"
    "tzSCJeG/cev6ETnZ0sI1TLJnib3YDosN3U3CARgRB+oPJ2OvKbbrL9xVWdXjRowPzMhGqffkjfhc"
    "E0WMP+ZHn0ij/n2VNjn9RAEl/00jA4iK/AN++1PJaG2G8sgiCAgEWcMFVCVYGLRWO8Tt/TiXYt9b"
    "USUn/0mhk9uR7LUegqHHCyN6LiJLhZ8UE0BLmH5g+/Z1+VXYaAOWcmoARjqd8I6j4tiYZ8f7mZy0"
    "e41yYJ7rBkJ20uYP+NF1IjBqwEkWqD7SAogyXLi/6mBMuw0xSN59vUNrRmPsp7kZaKBtEw/Yosog"
    "tpPdEITQok6ZuY0PpxfH/np79KBqBritIHYvRPTYM9+tR3Aisfo9UnmZ02cEI7fE63zgYO8JBjWO"
    "5HHk+i0DqIfK2wTx+5N7ArI/35OYlGeYV+qy+ifzcO5ZUB6XxzfGCz/GH57xwgPk6q0Ai3JrWcQ+"
    "STykRjpfMMJ8VrUXrmd9kqwUdRSKnX5e4u1N79sAyh5OlTRTUQkV+X8pV27uqGJg5MawlTtlAHiB"
    "LUY4W3y4PwI/05oXIQbthk8Q2thuQynoh3as5mnRMAznqqlNC+f1kYXzkfuKNFVEAv55L54klPfB"
    "hgcJqO7YfJXp6TjX5Ev9wExxePsJU4S1YQ6MtzSulwhaZwyC/nJVJDluQGe8b7GBa8glIhS6Wz4l"
    "NyormQhe3+yp6ySB/MCoVlrTFGA2ToIVAL9fLtQYYQBRkwLxWlnnhtTZjdPD4Et5d2YPYm/293kM"
    "Zp7jEqQdDaj0Hx9OlxFQZhfuf0ksG1IdMYWaUvMBQ2rfI5diu3dg6+DxOPiH1Fk9KSOY5wP0Yvl1"
    "E8aEo7qcNQHtByDjcc6AQz/e/qqRlAb64fpd86U/lEXlBG4VxVTOKu7UTBAXEfjewaDK0inQrvVC"
    "2yPNHGNg2J0f4Lj4Cch3pCxfiga2GwQHdvo8K6XhWZ56/D8e10p1K0+a5Fk4/HIBsUYYFJ4HDum2"
    "HwaztQnqvD5dUo4fusnFGKUWXEH+MKdkeB6iag6G3mqP2tZUaVOQ3/dGb9Utx1sPqg=="
)


def _config(**extra) -> UnicredConfig:
    base = {
        "conta": "c1",
        "api_key": API_KEY,
        "usuario": "usuario-api",
        "senha": "senha-api",
        "cooperativa": "0001",
        "id_beneficiario": BENEFICIARIO,
        "variacao_carteira": 43,
        "chave_autenticacao_webhook": CHAVE_AUT,
        "chave_criptografia_webhook": CHAVE_CRIPTO,
    }
    return UnicredConfig(**{**base, **extra})


@pytest.fixture
def falso():
    return UnicredFalso()


@pytest.fixture
def adapter(falso, dormir):
    return UnicredAdapter(_config(), client=falso.cliente(), dormir=dormir)


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("unicred") is UnicredAdapter
    assert UnicredAdapter.validado_sandbox is False
    caps = UnicredAdapter.capacidades
    assert Capacidade.BAIXA in caps
    assert Capacidade.PDF in caps
    assert Capacidade.WEBHOOK in caps
    assert Capacidade.EMISSAO_ASSINCRONA in caps
    assert Capacidade.HIBRIDO not in caps
    assert Capacidade.ALTERACAO not in caps
    assert TABELA_STATUS.versao == "2026.10"


def test_repr_sem_segredo():
    texto = repr(_config()) + repr(UnicredAdapter(_config()))
    for segredo in (API_KEY, "senha-api", CHAVE_AUT, CHAVE_CRIPTO):
        assert segredo not in texto


async def test_token_json_com_api_key_e_cabecalhos(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.consultar_titulo(registrado.id_banco)
    assert falso.pedido_token == {"nomeUsuario": "usuario-api", "senha": "senha-api"}
    assert falso.cabecalho_token["apikey"] == API_KEY
    assert falso.tokens_emitidos == 1
    cab = falso.ultimo_cabecalho
    assert cab["authorization"] == "Bearer tok-1"
    assert cab["apikey"] == API_KEY
    assert cab["cooperativa"] == "0001"


async def test_sandbox_usa_homolog(dormir):
    falso = UnicredFalso("/homolog")
    adapter = UnicredAdapter(
        _config(ambiente="sandbox"), client=falso.cliente(), dormir=dormir
    )
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.chamadas == [("POST", "/oauth2/v2/grant-token"), ("POST", BASE)]


async def test_credencial_recusada_e_validacao(dormir):
    def tratar(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Usuario de senha nao conferem"})

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = UnicredAdapter(_config(), client=cliente, dormir=dormir)
    with pytest.raises(Validacao) as erro:
        await adapter.registrar_titulo(fazer_titulo())
    assert erro.value.status_http == 401
    assert "senha-api" not in str(erro.value.resposta_bruta)


async def test_payload_sem_encargos_vai_isento(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.status is StatusCanonico.PENDENTE_REGISTRO
    assert len(registrado.id_banco) == 32
    corpo = falso.ultimo_corpo
    assert corpo["beneficiarioVariacaoCarteira"] == 43
    assert corpo["seuNumero"] == "FAT-chave-1"
    assert corpo["valor"] == 150.0
    assert corpo["vencimento"] == "2026-12-10"
    assert corpo["juros"] == {"codigo": "5"}
    assert corpo["multa"] == {"codigo": "3"}
    assert "nossoNumero" not in corpo
    assert "desconto" not in corpo
    pagador = corpo["pagador"]
    assert pagador["tipoPessoa"] == "F"
    assert pagador["tipoDocumento"] == "CPF"
    assert pagador["numeroDocumento"] == "52998224725"
    assert "nomeFantasia" not in pagador
    assert pagador["endereco"] == {
        "logradouro": "Rua das Flores",
        "numero": "10",
        "bairro": "Centro",
        "cidade": "Belo Horizonte",
        "uf": "MG",
        "cep": "30110000",
    }


async def test_payload_com_encargos_e_mensagens(adapter, falso):
    titulo = fazer_titulo().model_copy(
        update={
            "encargos": Encargos(
                multa_percentual=Decimal("2"),
                juros_mensal_percentual=Decimal("1"),
                desconto_percentual=Decimal("5"),
                desconto_dias_antes=3,
            ),
            "mensagens": tuple(f"linha {i} " + "x" * 90 for i in range(8)),
        }
    )
    await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["juros"] == {"codigo": "3", "dataInicio": "2026-12-11", "valor": 1.0}
    assert corpo["multa"] == {"codigo": "2", "dataInicio": "2026-12-11", "valor": 2.0}
    assert corpo["desconto"] == {
        "indicador": 1,
        "dataLimite": "2026-12-07",
        "valor": 7.5,
    }
    mensagens = corpo["mensagensFichaCompensacao"]
    assert len(mensagens) == 6
    assert all(len(m) == 80 for m in mensagens)


async def test_pagador_juridico_leva_nome_fantasia(adapter, falso):
    pj = Pessoa(
        nome="Empresa Pagadora Ltda", documento=CNPJ_VALIDO, endereco=endereco()
    )
    await adapter.registrar_titulo(fazer_titulo().model_copy(update={"pagador": pj}))
    pagador = falso.ultimo_corpo["pagador"]
    assert pagador["tipoPessoa"] == "J"
    assert pagador["tipoDocumento"] == "CNPJ"
    assert pagador["nomeFantasia"] == "Empresa Pagadora Ltda"


@pytest.mark.parametrize(
    "update",
    [
        {"encargos": Encargos(abatimento=Decimal("1"))},
        {"dias_baixa_automatica": 30},
        {"seu_numero": "1234567890123456"},
        {"pagador": Pessoa(nome="Sem Endereco", documento="529.982.247-25")},
        {
            "pagador": Pessoa(
                nome="Numero Longo",
                documento="529.982.247-25",
                endereco=Endereco(
                    logradouro="Rua A",
                    numero="123456",
                    bairro="Centro",
                    cidade="Porto Alegre",
                    uf="RS",
                    cep="90000-000",
                ),
            )
        },
    ],
)
async def test_recusa_o_que_a_api_nao_tem(adapter, falso, update):
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(fazer_titulo().model_copy(update=update))
    assert falso.titulos == {}


async def test_emissao_em_json_tambem_e_aceita(adapter, falso):
    falso.resposta_emissao = "json"
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.id_banco in falso.titulos


async def test_emissao_sem_id_e_validacao(dormir):
    def tratar(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("grant-token"):
            return httpx.Response(200, json={"accessToken": "t"})
        return httpx.Response(200, text="<html>erro</html>")

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = UnicredAdapter(_config(), client=cliente, dormir=dormir)
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(fazer_titulo())


async def test_consulta_em_processamento_depois_aberto(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.processando = 1
    pendente = await adapter.consultar_titulo(registrado.id_banco)
    assert pendente.status is StatusCanonico.PENDENTE_REGISTRO
    aberto = await adapter.consultar_titulo(registrado.id_banco)
    assert aberto.status is StatusCanonico.REGISTRADO
    assert aberto.linha_digitavel == "2" * 46
    assert aberto.codigo_barras == "1" * 44
    assert aberto.nosso_numero == "00000000001"
    assert aberto.valor_pago is None


async def test_status_desconhecido_e_validacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.titulos[registrado.id_banco]["status"] = "VENCIDO"
    with pytest.raises(Validacao):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_liquidado_traz_valor_e_data(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.00")
    assert consultado.data_pagamento == date(2026, 12, 9)
    # Pagamentos como objeto (o portal mostra os dois formatos).
    falso.titulos[registrado.id_banco]["pagamentos"] = {
        "data": "2026-12-08",
        "valor": 149.5,
    }
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.valor_pago == Decimal("149.5")
    assert consultado.data_pagamento == date(2026, 12, 8)


async def test_id_com_barra_nao_vira_caminho(adapter, falso):
    with pytest.raises(Validacao):
        await adapter.consultar_titulo("../outro")
    assert falso.chamadas == []


async def test_baixa_e_baixa_repetida(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.baixar_titulo(registrado.id_banco)
    assert falso.ultimo_corpo == {"tipo": "SOLICITACAO_CEDENTE"}
    assert baixado.status is StatusCanonico.BAIXADO
    assert baixado.resposta_bruta["codigoInstrucao"].endswith("1")
    with pytest.raises(Validacao) as erro:
        await adapter.baixar_titulo(registrado.id_banco)
    assert erro.value.status_http == 422


async def test_instrucao_baixa_e_alteracao_nao_suportada(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixa = Instrucao(chave_idempotencia="i1", tipo=TipoInstrucao.BAIXA)
    resultado = await adapter.enviar_instrucao(registrado.id_banco, baixa)
    assert resultado.status is StatusCanonico.BAIXADO
    prorroga = Instrucao(
        chave_idempotencia="i2",
        tipo=TipoInstrucao.PRORROGACAO,
        nova_data_vencimento=date(2027, 1, 10),
    )
    with pytest.raises(NaoSuportado):
        await adapter.enviar_instrucao(registrado.id_banco, prorroga)


async def test_pdf(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert (await adapter.obter_pdf(registrado.id_banco)).startswith(b"%PDF")


async def test_pdf_em_processamento_e_indisponivel(dormir):
    def tratar(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("grant-token"):
            return httpx.Response(200, json={"accessToken": "t"})
        return httpx.Response(202)

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = UnicredAdapter(_config(), client=cliente, dormir=dormir)
    with pytest.raises(Indisponivel) as erro:
        await adapter.obter_pdf("abc123")
    assert erro.value.retryable


def test_vetor_oficial_de_criptografia():
    claro = decifrar_webhook(VETOR_TEXTO, VETOR_AUT, VETOR_CRIPTO)
    assert claro == b"Unicred via Webhook"


def _adapter_homolog() -> UnicredAdapter:
    return UnicredAdapter(
        _config(
            chave_autenticacao_webhook=HOMOLOG_AUT,
            chave_criptografia_webhook=HOMOLOG_CRIPTO,
        )
    )


def test_webhook_oficial_de_liquidacao():
    eventos = _adapter_homolog().parse_webhook(
        CENARIO_LIQUIDACAO, {"X-Api-Key": HOMOLOG_AUT}
    )
    assert len(eventos) == 1
    evento = eventos[0]
    assert evento.tipo is StatusCanonico.LIQUIDADO
    assert evento.referencia == "c3978d9dec9a4f61877329d9e7828894"
    assert evento.valor_pago == Decimal("96.76")
    assert evento.ocorrido_em == datetime(2024, 8, 21)
    bruta = evento.resposta_bruta
    assert bruta["uuidRequisicaoWebhook"] == "d5f776e0-b80b-4883-8b56-d5ad1c4e7ef8"
    assert "Grafica" not in str(bruta)
    assert "84227798035" not in str(bruta)


def test_webhook_oficial_de_remessa_confirmada():
    (evento,) = _adapter_homolog().parse_webhook(CENARIO_REMESSA.encode())
    assert evento.tipo is StatusCanonico.REGISTRADO
    assert evento.valor_pago is None


def test_webhook_x_api_key_errado_ou_ausente():
    adapter = _adapter_homolog()
    with pytest.raises(Validacao):
        adapter.parse_webhook(CENARIO_LIQUIDACAO, {"x-api-key": CHAVE_AUT})
    with pytest.raises(Validacao):
        adapter.parse_webhook(CENARIO_LIQUIDACAO, {})


def test_webhook_com_chave_errada_e_validacao(adapter):
    with pytest.raises(Validacao):
        adapter.parse_webhook(CENARIO_LIQUIDACAO)


def test_webhook_exige_texto_e_chaves():
    with pytest.raises(Validacao):
        UnicredAdapter(_config()).parse_webhook({"titulo": {"uuidTitulo": "x"}})
    sem_chaves = UnicredAdapter(
        _config(chave_autenticacao_webhook=None, chave_criptografia_webhook=None)
    )
    with pytest.raises(Validacao):
        sem_chaves.parse_webhook(webhook_liquidacao("t1"))


@pytest.mark.parametrize(
    ("movimento", "instrucao", "situacao", "esperado"),
    [
        ("03", "01", "01", StatusCanonico.REJEITADO),
        ("03", "09", "01", StatusCanonico.REGISTRADO),
        ("02", "02", "02", StatusCanonico.BAIXADO),
        ("02", "01", "101", StatusCanonico.REJEITADO),
        ("02", "01", "100", StatusCanonico.PENDENTE_REGISTRO),
        ("01", "00", "06", StatusCanonico.LIQUIDADO),
    ],
)
def test_webhook_movimentos(movimento, instrucao, situacao, esperado):
    dados = {
        "uuidRequisicaoWebhook": "r1",
        "codigoMovimento": movimento,
        "codigoInstrucaoOrigem": instrucao,
        "titulo": {"uuidTitulo": "t1", "codigoSituacao": situacao},
    }
    corpo = cifrar_webhook(dados, CHAVE_AUT, CHAVE_CRIPTO)
    (evento,) = UnicredAdapter(_config()).parse_webhook(corpo)
    assert evento.tipo is esperado
    assert evento.chave_dedup == ("unicred", "t1", esperado)


def test_webhook_situacao_desconhecida_e_validacao():
    dados = {
        "codigoMovimento": "02",
        "codigoInstrucaoOrigem": "00",
        "titulo": {"uuidTitulo": "t1", "codigoSituacao": "07"},
    }
    corpo = cifrar_webhook(dados, CHAVE_AUT, CHAVE_CRIPTO)
    with pytest.raises(Validacao):
        UnicredAdapter(_config()).parse_webhook(corpo)


def test_webhook_sem_titulo_e_validacao():
    corpo = cifrar_webhook({"codigoMovimento": "06"}, CHAVE_AUT, CHAVE_CRIPTO)
    with pytest.raises(Validacao):
        UnicredAdapter(_config()).parse_webhook(corpo)


@pytest.mark.parametrize(
    ("aut", "cripto"),
    [
        (CHAVE_AUT, None),
        ("curta", CHAVE_CRIPTO),
        (CHAVE_CRIPTO, CHAVE_CRIPTO),
    ],
)
def test_config_de_webhook_invalida(aut, cripto):
    with pytest.raises(Validacao):
        _config(chave_autenticacao_webhook=aut, chave_criptografia_webhook=cripto)


async def test_401_na_api_renova_o_token(dormir):
    tokens: list[str] = []
    vistos: list[str] = []

    def tratar(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("grant-token"):
            tokens.append(f"t{len(tokens) + 1}")
            return httpx.Response(
                200, json={"accessToken": tokens[-1], "tokenExpirationTime": 2700}
            )
        vistos.append(request.headers["authorization"])
        if len(vistos) == 1:
            return httpx.Response(401, json={"message": "token expirado"})
        return httpx.Response(200, json={"status": "ABERTO"})

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = UnicredAdapter(_config(), client=cliente, dormir=dormir)
    consultado = await adapter.consultar_titulo("abc")
    assert consultado.status is StatusCanonico.REGISTRADO
    assert vistos == ["Bearer t1", "Bearer t2"]


@pytest.mark.parametrize(
    ("status", "corpo", "erro"),
    [
        (503, b"fora", Indisponivel),
        (200, b'{"refreshToken": "r"}', Validacao),
        (200, b'{"accessToken": ""}', Validacao),
        (200, b"nao e json", Validacao),
    ],
)
async def test_falhas_do_token(dormir, status, corpo, erro):
    def tratar(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=corpo)

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = UnicredAdapter(
        _config(), client=cliente, dormir=dormir, retry=PoliticaRetry(tentativas=1)
    )
    with pytest.raises(erro):
        await adapter.consultar_titulo("abc")
