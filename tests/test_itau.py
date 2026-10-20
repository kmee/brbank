import itertools
from datetime import date
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
from brbank.adapters.itau import ItauAdapter, ItauConfig
from tests.conftest import fazer_titulo
from tests.itau_falso import ItauFalso


def _config(**extra) -> ItauConfig:
    sequencia = itertools.count(1)
    base = {
        "conta": "c1",
        "client_id": "cid",
        "client_secret": "segredo",
        "agencia": "1500",
        "conta_corrente": "5000",
        "digito_conta": "5",
        "carteira": 109,
        "chave_pix": "chave@kmee.com.br",
        "gerar_nosso_numero": lambda _titulo: next(sequencia),
    }
    return ItauConfig(**{**base, **extra})


@pytest.fixture
def falso():
    return ItauFalso()


def _adapter(falso, dormir, **extra) -> ItauAdapter:
    return ItauAdapter(
        _config(**extra),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: date(2026, 11, 1),
        correlacao=lambda: "corr-1",
    )


@pytest.fixture
def adapter(falso, dormir):
    return _adapter(falso, dormir)


@pytest.fixture
def adapter_cash(falso, dormir):
    return _adapter(falso, dormir, bolecode=False, chave_pix=None)


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("itau") is ItauAdapter
    assert ItauAdapter.validado_sandbox is False
    assert Capacidade.HIBRIDO in ItauAdapter.capacidades
    assert Capacidade.WEBHOOK not in ItauAdapter.capacidades
    assert Capacidade.PDF not in ItauAdapter.capacidades


def test_cash_nao_declara_hibrido(adapter_cash, adapter):
    assert not adapter_cash.suporta(Capacidade.HIBRIDO)
    assert adapter_cash.suporta(Capacidade.BAIXA)
    assert adapter.suporta(Capacidade.HIBRIDO)


def test_config_invalida():
    with pytest.raises(Validacao, match="chave_pix"):
        _config(chave_pix=None)
    with pytest.raises(Validacao, match="ambiente"):
        _config(ambiente="homologacao")
    with pytest.raises(Validacao, match="agencia"):
        _config(agencia="12345")
    with pytest.raises(Validacao, match="carteira"):
        _config(carteira=1000)
    with pytest.raises(Validacao, match="especie"):
        _config(especie="XX")


async def test_token_e_cabecalhos(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    form = falso.ultimo_form
    assert form["client_id"] == ["cid"]
    assert form["client_secret"] == ["segredo"]
    assert form["grant_type"] == ["client_credentials"]
    assert "scope" not in form
    cab = falso.ultimo_cabecalho
    assert cab["authorization"] == "Bearer tok-1"
    assert cab["x-itau-apikey"] == "cid"
    assert cab["x-itau-correlationid"] == "corr-1"
    assert falso.hosts[0] == "sts.itau.com.br"


async def test_bolecode_registra_no_endpoint_pix(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert falso.chamadas[-1] == ("POST", "/recebimentos-pix/v1/boletos-pix")
    assert falso.hosts[-1] == "pix-pj.api.itau.com"
    corpo = falso.ultimo_corpo
    assert "data" not in corpo
    assert corpo["etapa_processo_boleto"] == "efetivacao"
    assert corpo["beneficiario"] == {"id_beneficiario": "150000050005"}
    assert corpo["dados_qrcode"] == {"chave": "chave@kmee.com.br"}
    assert corpo["dado_boleto"]["descricao_instrumento_cobranca"] == "boleto_pix"
    assert registrado.id_banco == "00000001"
    assert registrado.pix_copia_cola == "00020101021226..."
    assert registrado.txid == "tx00000001"
    assert registrado.linha_digitavel


async def test_cash_registra_com_envelope_data(adapter_cash, falso):
    registrado = await adapter_cash.registrar_titulo(fazer_titulo())
    assert falso.chamadas[-1] == ("POST", "/cash_management/v2/boletos")
    assert falso.hosts[-1] == "api.itau.com.br"
    data = falso.ultimo_corpo["data"]
    assert data["codigo_canal_operacao"] == "API"
    assert data["etapa_processo_boleto"] == "efetivacao"
    assert data["dado_boleto"]["descricao_instrumento_cobranca"] == "boleto"
    assert "dados_qrcode" not in data
    assert registrado.pix_copia_cola is None


async def test_payload_valores_e_encargos(adapter, falso):
    titulo = fazer_titulo(valor="150.005").model_copy(
        update={
            "encargos": Encargos(
                multa_percentual="2",
                juros_mensal_percentual="1",
                desconto_percentual="5",
                desconto_dias_antes=3,
                abatimento="1.50",
            )
        }
    )
    await adapter.registrar_titulo(titulo)
    dado = falso.ultimo_corpo["dado_boleto"]
    assert dado["valor_total_titulo"] == "00000000000015001"
    assert dado["valor_abatimento"] == "00000000000000150"
    assert dado["codigo_carteira"] == 109
    assert dado["codigo_especie"] == "01"
    assert dado["data_emissao"] == "2026-11-01"
    individual = dado["dados_individuais_boleto"][0]
    assert individual == {
        "numero_nosso_numero": "00000001",
        "data_vencimento": "2026-12-10",
        "valor_titulo": "00000000000015001",
        "texto_uso_beneficiario": "0",
        "texto_seu_numero": "FAT-chave-1",
    }
    assert dado["multa"] == {
        "codigo_tipo_multa": "02",
        "percentual_multa": "000000200000",
        "quantidade_dias_multa": 1,
    }
    assert dado["juros"] == {
        "codigo_tipo_juros": "90",
        "quantidade_dias_juros": 1,
        "percentual_juros": "000000100000",
    }
    assert dado["desconto"] == {
        "codigo_tipo_desconto": "02",
        "descontos": [
            {"data_desconto": "2026-12-07", "percentual_desconto": "000000500000"}
        ],
    }
    pessoa = dado["pagador"]["pessoa"]
    assert pessoa["tipo_pessoa"] == {
        "codigo_tipo_pessoa": "F",
        "numero_cadastro_pessoa_fisica": "52998224725",
    }
    assert dado["pagador"]["endereco"]["nome_logradouro"] == "Rua das Flores 10"
    assert dado["pagador"]["endereco"]["numero_CEP"] == "30110000"
    sacador = dado["sacador_avalista"]["pessoa"]["tipo_pessoa"]
    assert sacador["numero_cadastro_nacional_pessoa_juridica"] == "11222333000181"


async def test_sem_encargos_dispensa_multa_e_omite_juros(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    dado = falso.ultimo_corpo["dado_boleto"]
    assert dado["multa"] == {"codigo_tipo_multa": "03"}
    assert "juros" not in dado
    assert "desconto" not in dado
    assert dado["valor_abatimento"] == "0" * 17


async def test_sandbox_usa_simulacao_e_urls_de_sandbox(falso, dormir):
    adapter = _adapter(falso, dormir, ambiente="sandbox")
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["etapa_processo_boleto"] == "simulacao"
    assert falso.hosts == ["devportal.itau.com.br", "sandbox.devportal.itau.com.br"]
    cash = _adapter(ItauFalso(), dormir, ambiente="sandbox", bolecode=False)
    assert cash._config.etapa == "validacao"


async def test_nosso_numero_invalido(falso, dormir):
    adapter = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: "123456789")
    with pytest.raises(Validacao, match="8 digitos"):
        await adapter.registrar_titulo(fazer_titulo())
    sem_gerador = _adapter(falso, dormir, gerar_nosso_numero=None)
    with pytest.raises(Validacao):
        await sem_gerador.registrar_titulo(fazer_titulo())


async def test_recusas_de_payload(adapter):
    with pytest.raises(Validacao, match="baixa automatica"):
        await adapter.registrar_titulo(
            fazer_titulo().model_copy(update={"dias_baixa_automatica": 30})
        )
    titulo = fazer_titulo()
    sem = titulo.pagador.model_copy(update={"endereco": None})
    with pytest.raises(Validacao, match="endereco"):
        await adapter.registrar_titulo(titulo.model_copy(update={"pagador": sem}))


async def test_resposta_sem_individuais_e_validacao(adapter, falso):
    falso._registrar = lambda corpo, bolecode: httpx.Response(
        200, json={"data": {"dado_boleto": {}}}
    )
    with pytest.raises(Validacao, match="dados_individuais"):
        await adapter.registrar_titulo(fazer_titulo())


async def test_consulta_query_e_host(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert falso.chamadas[-1] == ("GET", "/boletoscash/v2/boletos")
    assert falso.hosts[-1] == "secure.api.cloud.itau.com.br"
    assert falso.ultima_query == {
        "id_beneficiario": "150000050005",
        "codigo_carteira": "109",
        "nosso_numero": "00000001",
        "view": "specific",
    }
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.pix_copia_cola


async def test_consulta_liquidada(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.50")
    assert consultado.data_pagamento == date(2026, 11, 10)


async def test_baixa_por_liquidacao_vira_liquidado_sem_valor(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    dado = falso.boletos[registrado.id_banco]["dado_boleto"]
    dado["dados_individuais_boleto"][0]["situacao_geral_boleto"] = "Baixada"
    dado["baixa"] = {"motivo_baixa": "Baixa por ter sido liquidado"}
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago is None


async def test_nosso_numero_reaproveitado_usa_ultimo(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    atual = falso.boletos[registrado.id_banco]
    antigo = {
        "dado_boleto": {
            "dados_individuais_boleto": [{"situacao_geral_boleto": "Paga"}],
        }
    }
    falso.boletos[registrado.id_banco] = atual
    original = falso.tratar

    def tratar(request):
        resp = original(request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"data": [antigo, falso.boletos[registrado.id_banco]]}
            )
        return resp

    falso.tratar = tratar
    adapter._transporte._client = falso.cliente()
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.REGISTRADO


async def test_status_desconhecido_vira_validacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    individual = falso._individual(registrado.id_banco)
    individual["situacao_geral_boleto"] = "Pagamento devolvido"
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_consulta_lista_vazia_e_validacao(adapter, falso):
    original = falso.tratar

    def tratar(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return original(request)

    falso.tratar = tratar
    adapter._transporte._client = falso.cliente()
    with pytest.raises(Validacao, match="sem titulo"):
        await adapter.consultar_titulo("00000001")


async def test_instrucao_prorrogacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(
            chave_idempotencia="i1",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=date(2027, 1, 15),
        ),
    )
    caminho = "/cash_management/v2/boletos/15000005000510900000001/data_vencimento"
    assert ("PATCH", caminho) in falso.chamadas
    assert falso.ultimo_corpo == {"data_vencimento": "2027-01-15"}
    assert falso._individual(registrado.id_banco)["data_vencimento"] == "2027-01-15"


async def test_instrucao_abatimento_e_baixa(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    id_banco = registrado.id_banco
    await adapter.enviar_instrucao(
        id_banco,
        Instrucao(chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor="2.5"),
    )
    assert falso.ultimo_corpo == {"valor_abatimento": "2.50"}
    assert falso.chamadas[-2][1].endswith("/abatimento")
    baixado = await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="b", tipo=TipoInstrucao.BAIXA)
    )
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.chamadas[-1] == (
        "PATCH",
        "/cash_management/v2/boletos/15000005000510900000001/baixa",
    )
    assert falso.ultimo_corpo == {}


async def test_instrucoes_invalidas(adapter):
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.PRORROGACAO)
        )
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.ABATIMENTO)
        )
    for tipo in (TipoInstrucao.PROTESTO, TipoInstrucao.NEGATIVACAO):
        with pytest.raises(NaoSuportado):
            await adapter.enviar_instrucao(
                "1", Instrucao(chave_idempotencia="x", tipo=tipo)
            )
    with pytest.raises(Validacao, match="8 digitos"):
        await adapter.baixar_titulo("abc")


async def test_webhook_e_pdf_nao_suportados(adapter):
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook(b"{}")
    with pytest.raises(NaoSuportado):
        await adapter.obter_pdf("1")


def test_repr_da_config_nao_vaza_segredos():
    texto = repr(_config(certificado_pem=b"cert-x", chave_pem=b"chave-y"))
    assert "segredo" not in texto
    assert "cert-x" not in texto
    assert "chave@kmee" not in texto
