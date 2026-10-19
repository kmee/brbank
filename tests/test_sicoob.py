from datetime import date
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
from brbank.adapters.sicoob import SicoobAdapter, SicoobConfig
from tests.conftest import fazer_titulo
from tests.sicoob_falso import SicoobFalso


def _config(**extra) -> SicoobConfig:
    base = {
        "conta": "c1",
        "client_id": "cid",
        "numero_cliente": 25546454,
        "numero_conta_corrente": "123456",
        "codigo_especie_documento": 2,
    }
    return SicoobConfig(**{**base, **extra})


@pytest.fixture
def falso():
    return SicoobFalso()


@pytest.fixture
def adapter(falso, dormir):
    return SicoobAdapter(
        _config(), client=falso.cliente(), dormir=dormir, hoje=lambda: date(2026, 11, 1)
    )


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("sicoob") is SicoobAdapter
    assert SicoobAdapter.validado_sandbox is False
    assert Capacidade.HIBRIDO in SicoobAdapter.capacidades
    assert Capacidade.WEBHOOK not in SicoobAdapter.capacidades
    assert Capacidade.PDF not in SicoobAdapter.capacidades


async def test_token_sem_segredo_e_cabecalho_client_id(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    form = falso.ultimo_form
    assert form["client_id"] == ["cid"]
    assert form["scope"] == ["boletos_inclusao"]
    assert form["grant_type"] == ["client_credentials"]
    assert "client_secret" not in form
    assert falso.ultimo_cabecalho["client_id"] == "cid"
    assert falso.ultimo_cabecalho["authorization"] == "Bearer tok-1"


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
    assert corpo["numeroCliente"] == 25546454
    assert corpo["numeroContaCorrente"] == 123456
    assert corpo["valor"] == 150.01
    assert corpo["dataEmissao"] == "2026-11-01"
    assert corpo["dataVencimento"] == "2026-12-10"
    assert corpo["pagador"]["numeroCpfCnpj"] == "52998224725"
    assert corpo["pagador"]["endereco"] == "Rua das Flores 10"
    assert corpo["beneficiarioFinal"]["numeroCpfCnpj"] == "11222333000181"
    assert corpo["tipoMulta"] == 2
    assert corpo["dataMulta"] == "2026-12-11"
    assert corpo["tipoJurosMora"] == 2
    assert corpo["tipoDesconto"] == 2
    assert corpo["dataPrimeiroDesconto"] == "2026-12-07"
    assert corpo["valorPrimeiroDesconto"] == 5.0
    assert corpo["valorAbatimento"] == 1.5
    assert corpo["mensagensInstrucao"] == ["linha um", "linha dois"]
    assert corpo["codigoCadastrarPIX"] == 1
    assert corpo["gerarPdf"] is False


async def test_sem_encargos_envia_isento(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    corpo = falso.ultimo_corpo
    assert (corpo["tipoDesconto"], corpo["tipoMulta"], corpo["tipoJurosMora"]) == (
        0,
        0,
        3,
    )


async def test_baixa_automatica_e_recusada(adapter):
    titulo = fazer_titulo().model_copy(update={"dias_baixa_automatica": 30})
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(titulo)


async def test_pagador_sem_endereco_e_recusado(adapter):
    titulo = fazer_titulo()
    sem = titulo.pagador.model_copy(update={"endereco": None})
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(titulo.model_copy(update={"pagador": sem}))


async def test_consulta_liquidada_le_historico(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("1150.50")
    assert consultado.data_pagamento == date(2026, 11, 10)


async def test_consulta_envia_identificacao_na_query(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.consultar_titulo(registrado.id_banco)
    assert falso.chamadas[-1] == ("GET", "/cobranca-bancaria/v3/boletos")
    assert falso.ultima_query == {
        "numeroCliente": "25546454",
        "codigoModalidade": "1",
        "nossoNumero": registrado.id_banco,
    }


async def test_status_desconhecido_vira_validacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[registrado.id_banco]["situacaoBoleto"] = "Vencido"
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_resposta_sem_conteudo_e_validacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    del falso.boletos[registrado.id_banco]["situacaoBoleto"]
    with pytest.raises(Validacao):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_instrucao_prorrogacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    nova = date(2027, 1, 15)
    await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(
            chave_idempotencia="i1",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=nova,
        ),
    )
    assert ("PATCH", f"/cobranca-bancaria/v3/boletos/{registrado.id_banco}") in (
        falso.chamadas
    )
    assert falso.ultimo_corpo["prorrogacaoVencimento"] == {
        "dataVencimento": "2027-01-15"
    }
    assert falso.boletos[registrado.id_banco]["dataVencimento"] == "2027-01-15"


async def test_instrucao_abatimento_protesto_e_baixa(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    id_banco = registrado.id_banco
    await adapter.enviar_instrucao(
        id_banco,
        Instrucao(chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor="2.5"),
    )
    assert falso.ultimo_corpo["abatimento"] == {"valorAbatimento": 2.5}
    await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="p", tipo=TipoInstrucao.PROTESTO)
    )
    assert falso.chamadas[-2][1].endswith(f"{id_banco}/protestos")
    baixado = await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="b", tipo=TipoInstrucao.BAIXA)
    )
    assert baixado.status is StatusCanonico.BAIXADO


async def test_instrucoes_invalidas(adapter):
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.PRORROGACAO)
        )
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.ABATIMENTO)
        )
    with pytest.raises(NaoSuportado):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.NEGATIVACAO)
        )


async def test_webhook_e_pdf_nao_suportados(adapter):
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook(b"{}")
    with pytest.raises(NaoSuportado):
        await adapter.obter_pdf("1")


async def test_sandbox_com_token_fixo_nao_chama_oauth(falso, dormir):
    adapter = SicoobAdapter(
        _config(ambiente="sandbox", token_sandbox="fixo-sandbox"),
        client=falso.cliente(),
        dormir=dormir,
    )
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.tokens_emitidos == 0
    assert falso.ultimo_cabecalho["authorization"] == "Bearer fixo-sandbox"
    assert "sandbox.sicoob" in adapter._config.url_base


def test_repr_da_config_nao_vaza_segredos():
    texto = repr(
        _config(token_sandbox="segredo-tok", certificado_pem=b"cert", chave_pem=b"k")
    )
    assert "segredo-tok" not in texto
    assert "cert" not in texto
