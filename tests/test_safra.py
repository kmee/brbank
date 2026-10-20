import itertools
from datetime import date
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
    Titulo,
    Validacao,
    criar_adapter,
    obter_classe,
)
from brbank.adapters.safra import (
    CABECALHO_CORRELACAO,
    SafraAdapter,
    SafraConfig,
    linha_digitavel,
)
from brbank.transporte import PoliticaRetry
from tests.conftest import CPF_VALIDO, fazer_titulo
from tests.safra_falso import SafraFalso


def config(**extra) -> SafraConfig:
    sequencia = itertools.count(1)
    base = dict(
        conta="conta-sf",
        client_id="cid-1",
        usuario="usuario-api",
        senha="SENHA-SECRETA",
        agencia="0290",
        conta_corrente="0058604",
        especie="01",
        ambiente="sandbox",
        gerar_nosso_numero=lambda _titulo: next(sequencia),
    )
    return SafraConfig(**{**base, **extra})


def montar(dormir, **extra):
    falso = SafraFalso()
    adapter = SafraAdapter(config(**extra), client=falso.cliente(), dormir=dormir)
    return adapter, falso


def titulo_completo(**extra) -> Titulo:
    base = dict(
        chave_idempotencia="c1",
        seu_numero="FATURA-12345",
        valor="150.005",
        vencimento=date(2026, 12, 10),
        pagador=Pessoa(
            nome="Maria da Silva Pereira de Albuquerque Neta",
            documento=CPF_VALIDO,
            endereco=Endereco(
                logradouro="Rua das Flores",
                numero="10",
                complemento="ap 101",
                bairro="Centro Historico",
                cidade="Belo Horizonte Capital",
                uf="mg",
                cep="30110-000",
            ),
            email="maria@example.com",
        ),
        encargos=Encargos(abatimento=Decimal("5.00")),
        mensagens=("Nao receber apos 30 dias", "x" * 80),
        dias_baixa_automatica=30,
    )
    return Titulo(**{**base, **extra})


def test_entry_point_capacidades_e_nao_validado():
    assert obter_classe("safra") is SafraAdapter
    assert SafraAdapter.validado_sandbox is False
    adapter = criar_adapter("safra", config())
    for capacidade in (
        Capacidade.BAIXA,
        Capacidade.ALTERACAO,
        Capacidade.PDF,
        Capacidade.WEBHOOK,
        Capacidade.HIBRIDO,
    ):
        assert not adapter.suporta(capacidade)


def test_repr_da_config_nao_vaza_segredo():
    assert "SENHA-SECRETA" not in repr(config())


@pytest.mark.parametrize(
    "extra",
    [
        {"ambiente": "homologacao"},
        {"agencia": "02-90"},
        {"conta_corrente": "58604-1"},
        {"especie": "DM"},
        {"posicao_mensagem": "3"},
    ],
)
def test_config_invalida(extra):
    with pytest.raises(Validacao):
        config(**extra)


def test_urls_por_ambiente():
    assert config().url_base.startswith("https://api-hml.safranegocios.com.br")
    producao = config(ambiente="producao")
    assert producao.url_base == "https://api.safranegocios.com.br/gateway/cobrancas/v1"
    assert producao.url_token.endswith("/gateway/v1/oauth2/token")


async def test_token_password_com_client_id_e_correlacao_por_chamada(dormir):
    adapter, falso = montar(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.consultar_titulo(registrado.id_banco)
    assert falso.pedidos_token == [
        {
            "client_id": "cid-1",
            "username": "usuario-api",
            "password": "SENHA-SECRETA",
            "grant_type": "password",
        }
    ]
    assert falso.ultimo_cabecalho["authorization"] == "Bearer tok-1"
    ids = [c[CABECALHO_CORRELACAO.lower()] for c in falso.cabecalhos]
    assert len(ids) == 2 and len(set(ids)) == 2


async def test_correlacao_nova_a_cada_tentativa(dormir):
    falso = SafraFalso()
    falso.falhas_503 = 2
    adapter = SafraAdapter(
        config(),
        client=falso.cliente(),
        dormir=dormir,
        retry=PoliticaRetry(tentativas=3),
    )
    await adapter.registrar_titulo(fazer_titulo())
    ids = [c[CABECALHO_CORRELACAO.lower()] for c in falso.cabecalhos]
    assert len(ids) == 3 and len(set(ids)) == 3


async def test_payload_de_registro(dormir):
    adapter, falso = montar(dormir)
    registrado = await adapter.registrar_titulo(titulo_completo())
    corpo = falso.ultimo_corpo
    assert corpo["agencia"] == 290
    assert corpo["conta"] == 58604
    doc = corpo["documento"]
    assert doc["numero"] == 1
    assert doc["numeroCliente"] == "FATURA-123"
    assert doc["especie"] == "01"
    assert doc["dataVencimento"] == "2026-12-10"
    assert doc["valor"] == 150.01
    assert doc["codigoMoeda"] == 9
    assert doc["diasDevolucao"] == 30
    assert doc["valorAbatimento"] == 5.0
    assert doc["mensagens"] == [
        {"posicao": "1", "descricao": "Nao receber apos 30 dias"},
        {"posicao": "1", "descricao": "x" * 72},
    ]
    assert doc["pagador"] == {
        "nome": "Maria da Silva Pereira de Albuquerque Ne",
        "tipoPessoa": "F",
        "numeroDocumento": "52998224725",
        "email": "maria@example.com",
        "endereco": {
            "logradouro": "Rua das Flores, 10, ap 101",
            "bairro": "Centro His",
            "cidade": "Belo Horizonte",
            "uf": "MG",
            "cep": "30110000",
        },
    }
    for ausente in ("multa", "juros", "desconto", "nossoNumero"):
        assert ausente not in doc
    assert registrado.id_banco == "000000001"
    assert registrado.nosso_numero == "000000001"
    assert registrado.seu_numero == "FATURA-12345"
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.codigo_barras.startswith("4229")
    assert registrado.linha_digitavel == linha_digitavel(registrado.codigo_barras)


async def test_dias_devolucao_padrao_da_config_e_omissao(dormir):
    adapter, falso = montar(dormir, dias_devolucao=90, posicao_mensagem="2")
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["documento"]["diasDevolucao"] == 90
    adapter, falso = montar(dormir)
    await adapter.registrar_titulo(fazer_titulo())
    doc = falso.ultimo_corpo["documento"]
    assert "diasDevolucao" not in doc
    assert "valorAbatimento" not in doc
    assert "mensagens" not in doc


async def test_pessoa_juridica_e_sem_email(dormir):
    adapter, falso = montar(dormir)
    pagador = Pessoa(
        nome="KMEE Ltda",
        documento="11.222.333/0001-81",
        endereco=Endereco(
            logradouro="Av. Brasil",
            bairro="Centro",
            cidade="BH",
            uf="MG",
            cep="30110000",
        ),
    )
    await adapter.registrar_titulo(
        fazer_titulo().model_copy(update={"pagador": pagador})
    )
    enviado = falso.ultimo_corpo["documento"]["pagador"]
    assert enviado["tipoPessoa"] == "J"
    assert enviado["numeroDocumento"] == "11222333000181"
    assert enviado["endereco"]["logradouro"] == "Av. Brasil, SN"
    assert "email" not in enviado


@pytest.mark.parametrize(
    "encargos",
    [
        Encargos(multa_percentual=Decimal("2")),
        Encargos(juros_mensal_percentual=Decimal("1")),
        Encargos(desconto_percentual=Decimal("5"), desconto_dias_antes=3),
    ],
)
async def test_encargos_sem_contrato_sao_recusados(dormir, encargos):
    adapter, falso = montar(dormir)
    with pytest.raises(Validacao, match="safra-duvidas"):
        await adapter.registrar_titulo(titulo_completo(encargos=encargos))
    assert falso.chamadas == []


async def test_pagador_sem_endereco_e_recusado(dormir):
    adapter, falso = montar(dormir)
    pagador = Pessoa(nome="Maria", documento=CPF_VALIDO)
    with pytest.raises(Validacao, match="endereco"):
        await adapter.registrar_titulo(
            fazer_titulo().model_copy(update={"pagador": pagador})
        )
    assert falso.chamadas == []


async def test_nosso_numero_padrao_e_seu_numero_numerico(dormir):
    falso = SafraFalso()
    adapter = SafraAdapter(
        config(gerar_nosso_numero=None), client=falso.cliente(), dormir=dormir
    )
    titulo = fazer_titulo().model_copy(update={"seu_numero": "4321"})
    registrado = await adapter.registrar_titulo(titulo)
    assert registrado.id_banco == "000004321"
    with pytest.raises(Validacao, match="9 digitos"):
        await adapter.registrar_titulo(fazer_titulo("outra"))
    longo = fazer_titulo("longa").model_copy(update={"seu_numero": "1234567890"})
    with pytest.raises(Validacao, match="9 digitos"):
        await adapter.registrar_titulo(longo)


async def test_registro_idempotente_no_processo(dormir):
    adapter, falso = montar(dormir)
    primeiro = await adapter.registrar_titulo(fazer_titulo())
    segundo = await adapter.registrar_titulo(fazer_titulo())
    assert primeiro is segundo
    assert [c for c in falso.chamadas if c[0] == "POST" and "boletos" in c[1]] == [
        ("POST", "/gateway/cobrancas/v1/boletos")
    ]


async def test_recusa_422_vira_validacao(dormir):
    adapter, falso = montar(dormir, gerar_nosso_numero=lambda _t: 7)
    await adapter.registrar_titulo(fazer_titulo("a"))
    with pytest.raises(Validacao) as erro:
        await adapter.registrar_titulo(fazer_titulo("b"))
    assert erro.value.status_http == 422
    assert not erro.value.retryable
    assert list(falso.boletos) == ["000000007"]


@pytest.mark.parametrize(
    "resposta",
    [
        {"numero": 1, "codigoBarras": "4229"},
        {"data": {"numero": 1}},
        {"data": {"numero": 2, "codigoBarras": "4" * 44}},
    ],
)
async def test_resposta_de_registro_fora_do_contrato(dormir, resposta):
    adapter, falso = montar(dormir)
    falso.resposta_registro = resposta
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(fazer_titulo())


async def test_consulta_query_e_campos(dormir):
    adapter, falso = montar(dormir)
    registrado = await adapter.registrar_titulo(titulo_completo())
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert falso.ultima_query == {
        "agencia": "0290",
        "conta": "58604",
        "numero": "000000001",
    }
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.seu_numero == "FATURA-123"
    assert consultado.valor_pago is None
    assert consultado.linha_digitavel == linha_digitavel(registrado.codigo_barras)
    assert consultado.resposta_bruta["pagador"]["nome"] == "***"


async def test_consulta_liquidada_tem_valor_e_data(dormir):
    adapter, falso = montar(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.00")
    assert consultado.data_pagamento == date(2026, 11, 10)


@pytest.mark.parametrize(
    ("situacao", "esperado"),
    [
        ("EMABERTO", StatusCanonico.REGISTRADO),
        ("Vencido", StatusCanonico.REGISTRADO),
        ("CANCELADO", StatusCanonico.BAIXADO),
        ("EXPIRADO", StatusCanonico.BAIXADO),
    ],
)
async def test_tabela_de_status(dormir, situacao, esperado):
    adapter, falso = montar(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[registrado.id_banco]["situacao"] = situacao
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is esperado
    assert consultado.valor_pago is None


async def test_status_desconhecido_e_validacao_nunca_pago(dormir):
    adapter, falso = montar(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[registrado.id_banco]["situacao"] = "PROTESTADO"
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)
    assert SafraAdapter.tabela_status.versao == "2026.10"


async def test_consulta_sem_o_titulo_na_lista(dormir):
    adapter, falso = montar(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[registrado.id_banco]["nossoNumero"] = "999"
    with pytest.raises(Validacao, match="nao encontrado"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_consulta_fora_do_formato(dormir):
    def tratar(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(200, json={"data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = SafraAdapter(config(), client=client, dormir=dormir)
    with pytest.raises(Validacao, match="content"):
        await adapter.consultar_titulo("1")


async def test_resposta_nao_json(dormir):
    def tratar(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(200, content=b"<html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = SafraAdapter(config(), client=client, dormir=dormir)
    with pytest.raises(Validacao, match="JSON"):
        await adapter.consultar_titulo("1")


async def test_503_esgotado_vira_indisponivel(dormir):
    falso = SafraFalso()
    falso.falhas_503 = 9
    adapter = SafraAdapter(
        config(), client=falso.cliente(), dormir=dormir, retry=PoliticaRetry(2)
    )
    with pytest.raises(Indisponivel):
        await adapter.consultar_titulo("000000001")


async def test_operacoes_sem_contrato_levantam_nao_suportado(dormir):
    adapter, falso = montar(dormir)
    with pytest.raises(NaoSuportado):
        await adapter.baixar_titulo("000000001")
    with pytest.raises(NaoSuportado):
        await adapter.enviar_instrucao(
            "000000001",
            Instrucao(
                chave_idempotencia="i1",
                tipo=TipoInstrucao.PRORROGACAO,
                nova_data_vencimento=date(2027, 1, 10),
            ),
        )
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook(b"{}")
    with pytest.raises(NaoSuportado):
        await adapter.obter_pdf("000000001")
    assert falso.chamadas == []
    await adapter.aclose()


def test_linha_digitavel_exemplo_febraban():
    # Exemplo classico (BB) de codigo de barras e linha digitavel.
    barras = "00193373700000001000500940144816060680935031"
    assert linha_digitavel(barras) == "00190500954014481606906809350314337370000000100"


@pytest.mark.parametrize("barras", [None, "", "123", "4" * 43, "A" * 44])
def test_linha_digitavel_recusa_codigo_invalido(barras):
    assert linha_digitavel(barras) is None


@pytest.mark.parametrize(
    ("campo", "valor"),
    [("valorTotalRecebimento", "abc"), ("dataHoraSituacao", "10/11/2026")],
)
async def test_liquidacao_ilegivel_e_validacao(dormir, campo, valor):
    adapter, falso = montar(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    falso.boletos[registrado.id_banco][campo] = valor
    with pytest.raises(Validacao, match="ilegivel"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_liquidacao_sem_valor_nem_data(dormir):
    adapter, falso = montar(dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[registrado.id_banco]["situacao"] = "PAGO"
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago is None
    assert consultado.data_pagamento is None
