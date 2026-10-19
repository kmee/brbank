import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from brbank import (
    Capacidade,
    Encargos,
    Endereco,
    Instrucao,
    NaoSuportado,
    Pessoa,
    StatusCanonico,
    TipoInstrucao,
    Titulo,
    Validacao,
    criar_adapter,
)
from brbank.adapters.sicredi import SicrediAdapter, SicrediConfig
from tests.conftest import CPF_VALIDO, fazer_titulo
from tests.sicredi_falso import CHAVE_API, SicrediFalso


def config(**extra) -> SicrediConfig:
    base = dict(
        conta="conta-sc",
        usuario="123456789",
        senha="SENHA-SECRETA",
        chave_api=CHAVE_API,
        codigo_beneficiario="12345",
        cooperativa="0101",
        posto="02",
        ambiente="sandbox",
    )
    return SicrediConfig(**{**base, **extra})


def montar(dormir, **extra):
    falso = SicrediFalso("/sb")
    return SicrediAdapter(config(**extra), client=falso.cliente(), dormir=dormir), falso


def test_marcado_como_nao_validado_no_sandbox():
    assert SicrediAdapter.validado_sandbox is False
    assert SicrediAdapter.banco == "sicredi"


def test_entry_point_e_capacidades(dormir):
    adapter = criar_adapter("sicredi", config())
    assert isinstance(adapter, SicrediAdapter)
    assert adapter.suporta(Capacidade.BAIXA | Capacidade.ALTERACAO | Capacidade.WEBHOOK)
    assert not adapter.suporta(Capacidade.PDF)


def test_repr_da_config_nao_vaza_segredo():
    texto = repr(config())
    assert "SENHA-SECRETA" not in texto
    assert CHAVE_API not in texto


async def test_token_password_com_chave_api_e_escopo(dormir):
    adapter, falso = montar(dormir)
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.pedidos_token == [
        {
            "username": "123456789",
            "password": "SENHA-SECRETA",
            "grant_type": "password",
            "scope": "cobranca",
        }
    ]
    assert falso.cabecalhos_token["x-api-key"] == CHAVE_API
    assert falso.cabecalhos_token["context"] == "COBRANCA"
    assert falso.ultimo_cabecalho["authorization"] == "Bearer tok-1"


async def test_payload_de_registro(dormir):
    adapter, falso = montar(dormir)
    titulo = Titulo(
        chave_idempotencia="c1",
        seu_numero="FAT-1",
        valor="150.005",
        vencimento=date(2026, 12, 10),
        pagador=Pessoa(
            nome="Maria da Silva",
            documento=CPF_VALIDO,
            endereco=Endereco(
                logradouro="Rua das Flores",
                numero="10",
                bairro="Centro",
                cidade="Belo Horizonte",
                uf="mg",
                cep="30110-000",
            ),
            email="maria@example.com",
        ),
        encargos=Encargos(
            multa_percentual=Decimal("2"),
            desconto_percentual=Decimal("5"),
            desconto_dias_antes=3,
        ),
        mensagens=("a", "b", "c", "d", "e"),
    )
    await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["tipoCobranca"] == "HIBRIDO"
    assert corpo["codigoBeneficiario"] == "12345"
    assert corpo["valor"] == 150.01
    assert corpo["dataVencimento"] == "2026-12-10"
    assert "nossoNumero" not in corpo
    assert corpo["multa"] == 2.0
    assert corpo["tipoDesconto"] == "PERCENTUAL"
    assert corpo["valorDesconto1"] == 5.0
    assert corpo["dataDesconto1"] == "2026-12-07"
    assert corpo["mensagens"] == ["a", "b", "c", "d"]
    assert corpo["pagador"] == {
        "tipoPessoa": "PESSOA_FISICA",
        "documento": "52998224725",
        "nome": "Maria da Silva",
        "endereco": "Rua das Flores,10",
        "cidade": "Belo Horizonte",
        "uf": "MG",
        "cep": "30110000",
        "email": "maria@example.com",
    }
    assert falso.ultimo_cabecalho["x-api-key"] == CHAVE_API
    assert falso.ultimo_cabecalho["cooperativa"] == "0101"
    assert falso.ultimo_cabecalho["posto"] == "02"


async def test_boleto_normal_quando_nao_hibrido(dormir):
    adapter, falso = montar(dormir, hibrido=False)
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["tipoCobranca"] == "NORMAL"


async def test_trunca_campos_longos(dormir):
    adapter, falso = montar(dormir)
    base = fazer_titulo()
    pagador = Pessoa(
        nome="N" * 60,
        documento="11.222.333/0001-81",
        endereco=Endereco(
            logradouro="L" * 60,
            numero="123",
            bairro="B",
            cidade="C" * 40,
            uf="SP",
            cep="01001000",
        ),
    )
    await adapter.registrar_titulo(base.model_copy(update={"pagador": pagador}))
    enviado = falso.ultimo_corpo["pagador"]
    assert enviado["tipoPessoa"] == "PESSOA_JURIDICA"
    assert len(enviado["nome"]) == 40
    assert len(enviado["endereco"]) == 40
    assert enviado["endereco"].endswith(",123")
    assert len(enviado["cidade"]) == 25
    assert "email" not in enviado


@pytest.mark.parametrize(
    "encargos, titulo_extra",
    [
        ({"juros_mensal_percentual": Decimal("1")}, {}),
        ({"abatimento": Decimal("1")}, {}),
        ({}, {"dias_baixa_automatica": 30}),
    ],
)
async def test_recusa_contrato_incerto(dormir, encargos, titulo_extra):
    adapter, falso = montar(dormir)
    titulo = fazer_titulo().model_copy(
        update={"encargos": Encargos(**encargos), **titulo_extra}
    )
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(titulo)
    assert falso.chamadas == []


async def test_pagador_sem_endereco_e_recusado(dormir):
    adapter, _ = montar(dormir)
    sem = Pessoa(nome="Ana", documento=CPF_VALIDO)
    with pytest.raises(Validacao, match="endereco"):
        await adapter.registrar_titulo(
            fazer_titulo().model_copy(update={"pagador": sem})
        )


async def test_registro_idempotente_no_processo(dormir):
    adapter, falso = montar(dormir)
    a = await adapter.registrar_titulo(fazer_titulo())
    b = await adapter.registrar_titulo(fazer_titulo())
    assert a == b
    assert [c for c in falso.chamadas if c[0] == "POST" and "token" not in c[1]] == [
        ("POST", "/cobranca/boleto/v1/boletos")
    ]


async def test_registro_sem_nosso_numero_e_validacao(dormir):
    def tratar(request):
        if "token" in request.url.path:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
        return httpx.Response(200, json={"linhaDigitavel": "x"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = SicrediAdapter(config(), client=client, dormir=dormir)
    with pytest.raises(Validacao, match="nossoNumero"):
        await adapter.registrar_titulo(fazer_titulo())


async def test_consulta_liquidada_traz_valor_e_data(dormir):
    adapter, falso = montar(dormir)
    reg = await adapter.registrar_titulo(fazer_titulo(valor="150.00"))
    consulta = await adapter.consultar_titulo(reg.id_banco)
    assert consulta.status is StatusCanonico.REGISTRADO
    assert consulta.valor_pago is None
    assert consulta.pix_copia_cola == "00020126..."
    assert consulta.txid == "tx123"
    falso.liquidar(reg.id_banco)
    consulta = await adapter.consultar_titulo(reg.id_banco)
    assert consulta.status is StatusCanonico.LIQUIDADO
    assert consulta.valor_pago == Decimal("150.00")
    assert consulta.data_pagamento == date(2026, 11, 10)


async def test_consulta_mascara_pii_na_resposta_bruta(dormir):
    adapter, _ = montar(dormir)
    reg = await adapter.registrar_titulo(fazer_titulo())
    consulta = await adapter.consultar_titulo(reg.id_banco)
    assert consulta.resposta_bruta["pagador"] == {"nome": "***", "documento": "***"}
    assert "Maria" not in json.dumps(consulta.resposta_bruta)


@pytest.mark.parametrize(
    "situacao, esperado",
    [
        ("Em Carteira", StatusCanonico.REGISTRADO),
        ("VENCIDO", StatusCanonico.REGISTRADO),
        ("Baixado por solicitação", StatusCanonico.BAIXADO),
        ("L", StatusCanonico.LIQUIDADO),
    ],
)
async def test_traducao_de_situacao(dormir, situacao, esperado):
    adapter, falso = montar(dormir)
    reg = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[reg.id_banco]["situacao"] = situacao
    assert (await adapter.consultar_titulo(reg.id_banco)).status is esperado


async def test_situacao_desconhecida_e_validacao(dormir):
    adapter, falso = montar(dormir)
    reg = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[reg.id_banco]["situacao"] = "PROTESTADO"
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(reg.id_banco)


async def test_baixa_usa_patch_com_codigo_beneficiario(dormir):
    adapter, falso = montar(dormir)
    reg = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.baixar_titulo(reg.id_banco)
    assert baixado.status is StatusCanonico.BAIXADO
    assert ("PATCH", f"/cobranca/boleto/v1/boletos/{reg.id_banco}/baixa") in (
        falso.chamadas
    )
    cabecalho = next(c for m, p, c in falso.cabecalhos if p.endswith("/baixa"))
    assert cabecalho["codigobeneficiario"] == "12345"


async def test_baixa_pendente_devolve_estado_consultado(dormir):
    adapter, falso = montar(dormir)
    falso.baixa_pendente = True
    reg = await adapter.registrar_titulo(fazer_titulo())
    assert (await adapter.baixar_titulo(reg.id_banco)).status is (
        StatusCanonico.REGISTRADO
    )


async def test_instrucao_prorrogacao_e_abatimento(dormir):
    adapter, falso = montar(dormir)
    reg = await adapter.registrar_titulo(fazer_titulo())
    await adapter.enviar_instrucao(
        reg.id_banco,
        Instrucao(
            chave_idempotencia="i1",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=date(2027, 1, 5),
        ),
    )
    assert falso.boletos[reg.id_banco]["dataVencimento"] == "2027-01-05"
    await adapter.enviar_instrucao(
        reg.id_banco,
        Instrucao(
            chave_idempotencia="i2", tipo=TipoInstrucao.ABATIMENTO, valor=Decimal("10")
        ),
    )
    assert falso.boletos[reg.id_banco]["abatimento"] == 10.0


async def test_instrucao_baixa_delega(dormir):
    adapter, _ = montar(dormir)
    reg = await adapter.registrar_titulo(fazer_titulo())
    r = await adapter.enviar_instrucao(
        reg.id_banco, Instrucao(chave_idempotencia="i", tipo=TipoInstrucao.BAIXA)
    )
    assert r.status is StatusCanonico.BAIXADO


@pytest.mark.parametrize(
    "instrucao",
    [
        Instrucao(chave_idempotencia="i", tipo=TipoInstrucao.PRORROGACAO),
        Instrucao(chave_idempotencia="i", tipo=TipoInstrucao.ABATIMENTO),
    ],
)
async def test_instrucao_incompleta_e_validacao(dormir, instrucao):
    adapter, _ = montar(dormir)
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao("1", instrucao)


async def test_instrucao_nao_mapeada(dormir):
    adapter, _ = montar(dormir)
    with pytest.raises(NaoSuportado):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="i", tipo=TipoInstrucao.PROTESTO)
        )


def webhook(**campos) -> dict:
    return {"nossoNumero": "200000001", "movimento": "LIQUIDACAO_PIX", **campos}


def test_webhook_liquidacao(dormir):
    adapter, _ = montar(dormir)
    corpo = json.dumps(webhook(dataEvento="2026-11-10T10:00:00", nome="Maria"))
    (evento,) = adapter.parse_webhook(corpo)
    assert evento.tipo is StatusCanonico.LIQUIDADO
    assert evento.referencia == "200000001"
    assert evento.ocorrido_em.year == 2026
    assert evento.resposta_bruta["nome"] == "***"
    assert evento.valor_pago is None


def test_webhook_aceita_lista_e_dict(dormir):
    adapter, _ = montar(dormir)
    assert len(adapter.parse_webhook([webhook(), webhook(nossoNumero="2")])) == 2
    assert len(adapter.parse_webhook(webhook())) == 1


@pytest.mark.parametrize(
    "corpo",
    [
        b"nao e json",
        {"movimento": "LIQUIDACAO_PIX"},
        ["texto"],
        webhook(movimento="OUTRO"),
        webhook(dataEvento="ontem"),
    ],
)
def test_webhook_invalido_e_validacao(dormir, corpo):
    adapter, _ = montar(dormir)
    with pytest.raises(Validacao):
        adapter.parse_webhook(corpo)


def test_ambiente_invalido():
    with pytest.raises(Validacao):
        _ = config(ambiente="x").url_base
