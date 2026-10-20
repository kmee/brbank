import itertools
from datetime import date
from decimal import Decimal

import pytest

from brbank import (
    Capacidade,
    Duplicado,
    Encargos,
    Instrucao,
    NaoSuportado,
    Orquestrador,
    StatusCanonico,
    TipoInstrucao,
    Validacao,
    obter_classe,
)
from brbank.adapters.santander import SantanderAdapter, SantanderConfig
from brbank.mascara import MASCARA
from tests.conftest import fazer_titulo
from tests.santander_falso import WORKSPACE, SantanderFalso

API = "/collection_bill_management/v2"


def _config(**extra) -> SantanderConfig:
    sequencia = itertools.count(1001)
    base = {
        "conta": "c1",
        "client_id": "cid",
        "client_secret": "segredo-xyz",
        "workspace_id": WORKSPACE,
        "convenio": "3567206",
        "gerar_nosso_numero": lambda _titulo: next(sequencia),
    }
    return SantanderConfig(**{**base, **extra})


def _adapter(falso, dormir, **extra) -> SantanderAdapter:
    return SantanderAdapter(
        _config(**extra),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: date(2026, 11, 1),
    )


@pytest.fixture
def falso():
    return SantanderFalso()


@pytest.fixture
def adapter(falso, dormir):
    return _adapter(falso, dormir)


@pytest.fixture
def hibrido(falso, dormir):
    return _adapter(falso, dormir, chave_pix="11222333000181", tipo_chave_pix="CNPJ")


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("santander") is SantanderAdapter
    assert SantanderAdapter.validado_sandbox is False
    assert Capacidade.WEBHOOK not in SantanderAdapter.capacidades
    assert Capacidade.PDF not in SantanderAdapter.capacidades
    assert Capacidade.EMISSAO_ASSINCRONA in SantanderAdapter.capacidades


def test_hibrido_so_com_chave_pix(adapter, hibrido):
    assert not adapter.suporta(Capacidade.HIBRIDO)
    assert hibrido.suporta(Capacidade.HIBRIDO)
    assert adapter.suporta(Capacidade.BAIXA)
    assert Capacidade.HIBRIDO in SantanderAdapter.capacidades


async def test_token_formulario_e_application_key(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.chamadas[0] == ("POST", "/auth/oauth/v2/token")
    assert falso.pedido_token == {
        "client_id": ["cid"],
        "client_secret": ["segredo-xyz"],
        "grant_type": ["client_credentials"],
    }
    assert falso.ultimo_cabecalho["x-application-key"] == "cid"
    assert falso.ultimo_cabecalho["authorization"] == "Bearer tok-1"
    assert falso.chamadas[1] == ("POST", f"{API}/workspaces/{WORKSPACE}/bank_slips")


async def test_payload_de_registro(adapter, falso):
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
            "dias_baixa_automatica": 30,
        }
    )
    await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["environment"] == "PRODUCAO"
    assert corpo["nsuCode"] == "1001"
    assert corpo["bankNumber"] == "1001"
    assert corpo["nsuDate"] == corpo["issueDate"] == "2026-11-01"
    assert corpo["covenantCode"] == "3567206"
    assert corpo["clientNumber"] == "FAT-chave-1"
    assert corpo["dueDate"] == "2026-12-10"
    assert corpo["nominalValue"] == "150.01"
    assert corpo["payer"] == {
        "name": "Maria da Silva",
        "documentType": "CPF",
        "documentNumber": "52998224725",
        "address": "Rua das Flores 10",
        "neighborhood": "Centro",
        "city": "Belo Horizonte",
        "state": "MG",
        "zipCode": "30110-000",
    }
    assert corpo["beneficiary"] == {
        "name": "KMEE Ltda",
        "documentType": "CNPJ",
        "documentNumber": "11222333000181",
    }
    assert corpo["documentKind"] == "DUPLICATA_MERCANTIL"
    assert corpo["paymentType"] == "REGISTRO"
    assert corpo["protestType"] == "SEM_PROTESTO"
    # 5% de 150.01 = 7.5005 -> 7.50, limite = vencimento - 3 dias.
    assert corpo["discount"] == {
        "type": "VALOR_DATA_FIXA",
        "discountOne": {"value": "7.50", "limitDate": "2026-12-07"},
    }
    assert (corpo["finePercentage"], corpo["fineQuantityDays"]) == ("2.00", "1")
    assert corpo["interestPercentage"] == "1.00"
    assert corpo["deductionValue"] == "1.50"
    assert corpo["writeOffQuantityDays"] == "30"
    assert corpo["messages"] == ["linha um", "linha dois"]
    assert "key" not in corpo


async def test_sem_encargos_envia_zero(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    corpo = falso.ultimo_corpo
    assert corpo["finePercentage"] == "0.00"
    assert corpo["fineQuantityDays"] == "0"
    assert corpo["interestPercentage"] == "0.00"
    for ausente in ("discount", "deductionValue", "writeOffQuantityDays", "messages"):
        assert ausente not in corpo


async def test_ambiente_teste_usa_host_de_producao_e_nsu_tst(falso, dormir):
    adapter = _adapter(falso, dormir, ambiente="teste")
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["environment"] == "TESTE"
    assert falso.ultimo_corpo["nsuCode"] == "TST1001"
    assert falso.urls[-1].startswith("https://trust-open.api.santander.com.br/")


async def test_sandbox_usa_host_proprio(falso, dormir):
    adapter = _adapter(falso, dormir, ambiente="sandbox")
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.urls[0] == (
        "https://trust-sandbox.api.santander.com.br/auth/oauth/v2/token"
    )
    assert falso.ultimo_corpo["environment"] == "TESTE"


async def test_registro_devolve_linha_e_mascara_resposta(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.id_banco == registrado.nosso_numero == "1001"
    assert registrado.linha_digitavel
    assert registrado.codigo_barras
    assert registrado.pix_copia_cola is None
    assert registrado.resposta_bruta["payer"] == MASCARA
    assert "Maria" not in repr(registrado.resposta_bruta)


async def test_registro_sem_linha_fica_pendente(dormir):
    falso = SantanderFalso(devolve_linha=False)
    adapter = _adapter(falso, dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.status is StatusCanonico.PENDENTE_REGISTRO


async def test_hibrido_envia_chave_e_le_qr(hibrido, falso):
    registrado = await hibrido.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["key"] == {"type": "CNPJ", "dictKey": "11222333000181"}
    assert registrado.pix_copia_cola == "00020101021226..."
    consultado = await hibrido.consultar_titulo(registrado.id_banco)
    assert consultado.pix_copia_cola == "00020101021226..."
    assert consultado.linha_digitavel == "03399.00000 00000.000000"
    assert falso.urls[-1].endswith(
        f"{API}/bills/3567206.{registrado.id_banco}?tipoConsulta=bankslip"
    )
    assert "Maria" not in repr(consultado.resposta_bruta)


async def test_consulta_ativo_faz_uma_chamada(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    antes = len(falso.chamadas)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.REGISTRADO
    assert len(falso.chamadas) == antes + 1
    assert falso.urls[-1].endswith(
        f"{API}/bills?beneficiaryCode=3567206&bankNumber={registrado.id_banco}"
    )
    assert consultado.valor_pago is None


async def test_consulta_liquidada_le_valor_pago_e_data(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    # paidValue tem precedencia sobre o valor creditado (liquido de tarifa).
    assert consultado.valor_pago == Decimal("150.00")
    assert consultado.data_pagamento == date(2026, 11, 10)
    assert falso.urls[-1].endswith("?tipoConsulta=settlement")


async def test_baixa_por_pix_vira_liquidacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar_por_pix(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.00")
    assert consultado.data_pagamento == date(2026, 11, 9)


async def test_baixa_comum_nao_liquida(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.baixar_titulo(registrado.id_banco)
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.chamadas[-1] == ("PATCH", f"{API}/workspaces/{WORKSPACE}/bank_slips")
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.BAIXADO
    assert consultado.valor_pago is None


async def test_status_desconhecido_vira_validacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[registrado.id_banco]["status"] = "PROTESTADO"
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)
    falso.boletos[registrado.id_banco].pop("status")
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_duplicado_traz_o_nosso_numero_e_orquestrador_reconsulta(falso, dormir):
    adapter = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: 77)
    primeiro = await adapter.registrar_titulo(fazer_titulo("a"))
    with pytest.raises(Duplicado) as erro:
        await adapter.registrar_titulo(fazer_titulo("b"))
    assert erro.value.id_banco == "77"
    resultado = await Orquestrador(adapter).emitir(fazer_titulo("b"))
    assert resultado.id_banco == primeiro.id_banco
    assert resultado.status is StatusCanonico.REGISTRADO


async def test_nosso_numero_invalido_e_recusado(falso, dormir):
    adapter = SantanderAdapter(
        _config(gerar_nosso_numero=None), client=falso.cliente(), dormir=dormir
    )
    with pytest.raises(Validacao, match="nosso numero"):
        await adapter.registrar_titulo(fazer_titulo())
    longo = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: "1" * 13)
    with pytest.raises(Validacao):
        await longo.registrar_titulo(fazer_titulo())
    numerico = fazer_titulo().model_copy(update={"seu_numero": "42"})
    registrado = await adapter.registrar_titulo(numerico)
    assert registrado.id_banco == "42"


async def test_pagador_sem_endereco_e_recusado(adapter):
    titulo = fazer_titulo()
    sem = titulo.pagador.model_copy(update={"endereco": None})
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(titulo.model_copy(update={"pagador": sem}))


async def test_instrucoes(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    id_banco = registrado.id_banco
    resultado = await adapter.enviar_instrucao(
        id_banco,
        Instrucao(
            chave_idempotencia="p",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=date(2027, 1, 15),
        ),
    )
    assert falso.boletos[id_banco]["dueDate"] == "2027-01-15"
    assert resultado.status is StatusCanonico.REGISTRADO
    assert ("PATCH", f"{API}/workspaces/{WORKSPACE}/bank_slips") in falso.chamadas
    await adapter.enviar_instrucao(
        id_banco,
        Instrucao(chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor="2.5"),
    )
    assert falso.ultimo_corpo == {
        "covenantCode": "3567206",
        "bankNumber": id_banco,
        "deductionValue": "2.50",
    }
    await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="t", tipo=TipoInstrucao.PROTESTO)
    )
    assert falso.boletos[id_banco]["_protesto"] is True
    baixado = await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="b", tipo=TipoInstrucao.BAIXA)
    )
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.ultimo_corpo["operation"] == "BAIXAR"


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


@pytest.mark.parametrize(
    "extra",
    [
        {"ambiente": "homologacao"},
        {"workspace_id": ""},
        {"convenio": "12A"},
        {"especie": "XX"},
        {"chave_pix": "x@y.com"},
        {"chave_pix": "x@y.com", "tipo_chave_pix": "TELEFONE"},
    ],
)
def test_config_invalida(extra):
    with pytest.raises(Validacao):
        _config(**extra)


def test_repr_da_config_nao_vaza_segredos():
    texto = repr(
        _config(
            certificado_pem=b"cert-pem",
            chave_pem=b"chave-pem",
            chave_pix="pix@kmee.com.br",
            tipo_chave_pix="EMAIL",
        )
    )
    assert "segredo-xyz" not in texto
    assert "cert-pem" not in texto
    assert "pix@kmee.com.br" not in texto
