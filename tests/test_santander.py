import itertools
import json
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from brbank import (
    Capacidade,
    Duplicado,
    Encargos,
    Indisponivel,
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
from tests import santander_falso
from tests.conftest import CNPJ_VALIDO, fazer_titulo
from tests.santander_falso import BARRAS, LINHA, QR, WORKSPACE, SantanderFalso

API = "/collection_bill_management/v2"
HOJE = date(2026, 11, 1)


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


def _adapter(falso, dormir, *, hoje=HOJE, **extra) -> SantanderAdapter:
    return SantanderAdapter(
        _config(**extra),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: hoje,
    )


@pytest.fixture
def falso():
    return SantanderFalso()


@pytest.fixture
def adapter(falso, dormir):
    return _adapter(falso, dormir)


@pytest.fixture
def hibrido(falso, dormir):
    return _adapter(falso, dormir, chave_pix="91300766000147", tipo_chave_pix="CNPJ")


@pytest.fixture
def com_webhook(falso, dormir):
    return _adapter(falso, dormir, webhook_ativo=True)


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("santander") is SantanderAdapter
    assert SantanderAdapter.validado_sandbox is False
    assert SantanderAdapter.capacidades == (
        Capacidade.HIBRIDO
        | Capacidade.ALTERACAO
        | Capacidade.BAIXA
        | Capacidade.PDF
        | Capacidade.WEBHOOK
    )


def test_registro_e_instrucao_nao_sao_assincronos():
    # Guia oficial: o POST devolve a linha na hora e o PATCH e processado online.
    assert Capacidade.EMISSAO_ASSINCRONA not in SantanderAdapter.capacidades
    assert Capacidade.INSTRUCAO_ASSINCRONA not in SantanderAdapter.capacidades


def test_hibrido_so_com_chave_pix(adapter, hibrido):
    assert not adapter.suporta(Capacidade.HIBRIDO)
    assert hibrido.suporta(Capacidade.HIBRIDO)
    assert adapter.suporta(Capacidade.BAIXA)
    assert Capacidade.HIBRIDO in SantanderAdapter.capacidades


def test_webhook_so_com_workspace_configurado(adapter, com_webhook):
    assert not adapter.suporta(Capacidade.WEBHOOK)
    assert com_webhook.suporta(Capacidade.WEBHOOK)


def test_ambiente_teste_so_registra(falso, dormir):
    teste = _adapter(falso, dormir, ambiente="teste", webhook_ativo=True)
    for capacidade in (
        Capacidade.ALTERACAO,
        Capacidade.BAIXA,
        Capacidade.PDF,
        Capacidade.WEBHOOK,
    ):
        assert not teste.suporta(capacidade)


def test_sandbox_nao_tem_pdf(falso, dormir):
    sandbox = _adapter(falso, dormir, ambiente="sandbox")
    assert not sandbox.suporta(Capacidade.PDF)
    assert sandbox.suporta(Capacidade.BAIXA)


def test_certificado_e_obrigatorio_fora_de_teste_unitario():
    with pytest.raises(Validacao, match="mTLS"):
        SantanderAdapter(_config())


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
        "documentNumber": "91300766000147",
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


async def test_sem_encargos_nao_envia_os_campos(adapter, falso):
    # Os exemplos oficiais omitem o que nao se aplica; zero era invencao nossa.
    await adapter.registrar_titulo(fazer_titulo())
    corpo = falso.ultimo_corpo
    for ausente in (
        "finePercentage",
        "fineQuantityDays",
        "interestPercentage",
        "discount",
        "deductionValue",
        "writeOffQuantityDays",
        "messages",
    ):
        assert ausente not in corpo


async def test_ambiente_teste_usa_host_de_producao_e_nsu_tst(falso, dormir):
    adapter = _adapter(falso, dormir, ambiente="teste")
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["environment"] == "TESTE"
    assert falso.ultimo_corpo["nsuCode"] == "TST1001"
    assert falso.urls[-1].startswith("https://trust-open.api.santander.com.br/")
    assert registrado.linha_digitavel == LINHA
    # O banco nao guarda o que veio em TESTE.
    assert falso.boletos == {}


async def test_sandbox_usa_host_proprio_e_environment_producao(falso, dormir):
    # Colecao Postman oficial do sandbox: environment PRODUCAO e NSU numerico.
    adapter = _adapter(falso, dormir, ambiente="sandbox")
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.urls[0] == (
        "https://trust-sandbox.api.santander.com.br/auth/oauth/v2/token"
    )
    assert falso.ultimo_corpo["environment"] == "PRODUCAO"
    assert falso.ultimo_corpo["nsuCode"] == "1001"


async def test_registro_devolve_linha_e_mascara_resposta(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.id_banco == registrado.nosso_numero == "1001"
    assert registrado.linha_digitavel == LINHA
    assert registrado.codigo_barras == BARRAS
    assert registrado.pix_copia_cola is None
    assert registrado.resposta_bruta["payer"] == MASCARA
    assert "Maria" not in repr(registrado.resposta_bruta)


async def test_registro_sem_linha_fica_pendente_e_a_consulta_completa(dormir):
    falso = SantanderFalso(devolve_linha=False)
    adapter = _adapter(falso, dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.status is StatusCanonico.PENDENTE_REGISTRO
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.linha_digitavel == LINHA


async def test_hibrido_envia_chave_e_le_qr_da_sonda(hibrido, falso):
    registrado = await hibrido.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["key"] == {"type": "CNPJ", "dictKey": "91300766000147"}
    assert registrado.pix_copia_cola == QR
    assert registrado.txid
    consultado = await hibrido.consultar_titulo(registrado.id_banco)
    assert consultado.pix_copia_cola == QR
    assert consultado.txid == registrado.txid
    assert consultado.linha_digitavel == LINHA
    assert falso.urls[-1].endswith(
        f"{API}/workspaces/{WORKSPACE}/bank_slips/"
        f"{registrado.id_banco}.2026-11-01.P.3567206.{registrado.id_banco}"
    )
    assert "Maria" not in repr(consultado.resposta_bruta)
    assert "LOJA AVALISTA" not in repr(consultado.resposta_bruta)


async def test_depois_de_d_mais_2_nao_chama_a_sonda(falso, dormir):
    registro = _adapter(
        falso, dormir, chave_pix="91300766000147", tipo_chave_pix="CNPJ"
    )
    registrado = await registro.registrar_titulo(fazer_titulo())
    depois = _adapter(
        falso,
        dormir,
        hoje=date(2026, 11, 4),
        chave_pix="91300766000147",
        tipo_chave_pix="CNPJ",
    )
    consultado = await depois.consultar_titulo(registrado.id_banco)
    assert consultado.pix_copia_cola is None
    assert consultado.linha_digitavel == LINHA
    assert not any("bank_slips/" in url for url in falso.urls)


async def test_sonda_recusada_nao_derruba_a_consulta(falso, dormir):
    hibrido = _adapter(falso, dormir, chave_pix="91300766000147", tipo_chave_pix="CNPJ")
    registrado = await hibrido.registrar_titulo(fazer_titulo())
    # O NSU muda quando o titulo foi registrado por fora: a sonda da 404.
    falso.boletos[registrado.id_banco]["nsuCode"] = "999"
    consultado = await hibrido.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.pix_copia_cola is None


async def test_consulta_ativa_traz_linha_mesmo_sem_pix(adapter, falso):
    # Sem isso, um Duplicado recuperado por consulta ficaria sem linha digitavel.
    registrado = await adapter.registrar_titulo(fazer_titulo())
    antes = len(falso.chamadas)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.linha_digitavel == LINHA
    assert consultado.codigo_barras == BARRAS
    assert len(falso.chamadas) == antes + 2
    assert falso.urls[-2].endswith(
        f"{API}/bills?beneficiaryCode=3567206&bankNumber={registrado.id_banco}"
    )
    assert falso.urls[-1].endswith(
        f"{API}/bills/3567206.{registrado.id_banco}?tipoConsulta=bankslip"
    )
    assert consultado.valor_pago is None


async def test_consulta_aceita_content_em_objeto(falso, dormir):
    # Schema e telas mostram lista; os exemplos do OpenAPI mostram objeto.
    transporte = httpx.MockTransport(
        lambda request: (
            httpx.Response(200, json={"access_token": "t", "expires_in": 900})
            if request.url.path.endswith("/token")
            else httpx.Response(
                200,
                json={
                    "_pageable": {"_moreElements": False},
                    "_content": {
                        "bankNumber": 7,
                        "status": "Ativo",
                        "bankslipData": {"digitableLine": LINHA},
                    },
                },
            )
        )
    )
    adapter = SantanderAdapter(
        _config(), client=httpx.AsyncClient(transport=transporte), dormir=dormir
    )
    consultado = await adapter.consultar_titulo("7")
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.linha_digitavel == LINHA


async def test_pagamento_de_hoje_continua_registrado(adapter, falso):
    # Ativo + "Baixa Operacional": o banco ainda pode estornar ate D+1.
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.pagar_hoje(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.valor_pago is None
    assert (
        consultado.resposta_bruta["consulta"]["statusComplement"] == "Baixa Operacional"
    )


async def test_liquidado_parcialmente_nao_liquida(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    boleto = falso.boletos[registrado.id_banco]
    boleto["status"] = "Liquidado Parcialmente"
    boleto["paidValue"] = "50.00"
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.valor_pago is None
    assert consultado.resposta_bruta["consulta"]["paidValue"] == "50.00"


async def test_consulta_liquidada_le_valor_pago_e_data(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    # paidValue tem precedencia sobre o valor creditado (liquido de tarifa).
    assert consultado.valor_pago == Decimal("150.00")
    assert consultado.data_pagamento == date(2026, 11, 10)
    assert falso.urls[-1].endswith("?tipoConsulta=settlement")


async def test_sem_paid_value_usa_settlement_value_e_nao_o_creditado(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    del falso.boletos[registrado.id_banco]["paidValue"]
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.valor_pago == Decimal("150.00")


async def test_baixa_por_pix_vira_liquidacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar_por_pix(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.00")
    assert consultado.data_pagamento == date(2026, 11, 9)


async def test_indicacao_de_pix_no_complemento_vira_liquidacao(adapter, falso):
    # O guia diz que a indicacao vem tambem nas rotas por nosso e seu numero.
    registrado = await adapter.registrar_titulo(fazer_titulo())
    boleto = falso.boletos[registrado.id_banco]
    boleto["status"] = "Baixado"
    boleto["statusComplement"] = "Baixa de pagamento  via Pix"
    boleto["paidValue"] = "150.00"
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.00")


@pytest.mark.parametrize(
    "descricao", ["BAIXA AUTOMATICA", "BAIXA CONFORME INSTRUCAO", "PIX", ""]
)
async def test_outra_baixa_nao_liquida(adapter, falso, descricao):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    boleto = falso.boletos[registrado.id_banco]
    boleto["status"] = "Baixado"
    boleto["paidValue"] = "10.00"
    boleto["_settlement"] = {"writeOffData": [{"writeOffDescription": descricao}]}
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.BAIXADO
    assert consultado.valor_pago is None


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
    falso.boletos[registrado.id_banco]["status"] = None
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_consulta_sem_boleto_vira_validacao(falso, dormir):
    transporte = httpx.MockTransport(
        lambda request: (
            httpx.Response(200, json={"access_token": "t", "expires_in": 900})
            if request.url.path.endswith("/token")
            else httpx.Response(200, json={"_content": []})
        )
    )
    adapter = SantanderAdapter(
        _config(), client=httpx.AsyncClient(transport=transporte), dormir=dormir
    )
    with pytest.raises(Validacao, match="sem o boleto"):
        await adapter.consultar_titulo("7")


async def test_duplicado_vem_em_400_com_codigo_0001(falso, dormir):
    # Codigo oficial "0001 - Nosso Numero ja registrado". O guia so lista 400.
    adapter = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: 77)
    primeiro = await adapter.registrar_titulo(fazer_titulo("a"))
    with pytest.raises(Duplicado) as erro:
        await adapter.registrar_titulo(fazer_titulo("b"))
    assert erro.value.id_banco == "77"
    assert erro.value.status_http == 400
    resultado = await Orquestrador(adapter).emitir(fazer_titulo("b"))
    assert resultado.id_banco == primeiro.id_banco
    assert resultado.status is StatusCanonico.REGISTRADO
    assert resultado.linha_digitavel == LINHA


async def test_codigo_em_numero_tambem_e_duplicado(dormir):
    # O OpenAPI define `_code` como inteiro; o guia escreve "0001".
    def tratar(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 900})
        return httpx.Response(400, json={"_errors": [{"_code": 1, "_message": "x"}]})

    adapter = SantanderAdapter(
        _config(gerar_nosso_numero=lambda _t: 5),
        client=httpx.AsyncClient(transport=httpx.MockTransport(tratar)),
        dormir=dormir,
        hoje=lambda: HOJE,
    )
    with pytest.raises(Duplicado):
        await adapter.registrar_titulo(fazer_titulo())


async def test_409_continua_sendo_duplicado(dormir):
    falso = SantanderFalso(duplicado_409=True)
    adapter = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: 77)
    await adapter.registrar_titulo(fazer_titulo("a"))
    with pytest.raises(Duplicado) as erro:
        await adapter.registrar_titulo(fazer_titulo("b"))
    assert erro.value.id_banco == "77"


async def test_outro_erro_400_nao_vira_duplicado(falso, dormir):
    transporte = httpx.MockTransport(
        lambda request: (
            santander_falso.erro_400("00058", "CPF/CNPJ incorreto")
            if request.method == "POST" and request.url.path.endswith("bank_slips")
            else falso.tratar(request)
        )
    )
    adapter = SantanderAdapter(
        _config(gerar_nosso_numero=lambda _t: 9),
        client=httpx.AsyncClient(transport=transporte),
        dormir=dormir,
    )
    with pytest.raises(Validacao) as erro:
        await adapter.registrar_titulo(fazer_titulo())
    assert not isinstance(erro.value, Duplicado)
    assert erro.value.status_http == 400


async def test_nosso_numero_invalido_e_recusado(falso, dormir):
    adapter = SantanderAdapter(
        _config(gerar_nosso_numero=None),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: HOJE,
    )
    with pytest.raises(Validacao, match="nosso numero"):
        await adapter.registrar_titulo(fazer_titulo())
    longo = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: "1" * 14)
    with pytest.raises(Validacao):
        await longo.registrar_titulo(fazer_titulo())
    zero = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: "000")
    with pytest.raises(Validacao, match="diferente"):
        await zero.registrar_titulo(fazer_titulo())
    numerico = fazer_titulo().model_copy(update={"seu_numero": "42"})
    registrado = await adapter.registrar_titulo(numerico)
    assert registrado.id_banco == "42"


async def test_nosso_numero_de_13_digitos_e_sem_zeros_a_esquerda(falso, dormir):
    adapter = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: "1234567890123")
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.id_banco == "1234567890123"
    zeros = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: "0000000000018")
    registrado = await zeros.registrar_titulo(fazer_titulo())
    assert registrado.id_banco == "18"
    assert falso.ultimo_corpo["bankNumber"] == "18"
    consultado = await zeros.consultar_titulo("18")
    assert consultado.nosso_numero == "18"


async def test_seu_numero_so_com_letras_numeros_e_espaco(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["clientNumber"] == "FATchave1"
    registrado = await adapter.registrar_titulo(
        fazer_titulo().model_copy(update={"seu_numero": "NF 12/3"})
    )
    assert registrado.seu_numero == "NF 12/3"
    assert falso.ultimo_corpo["clientNumber"] == "NF 123"
    await adapter.registrar_titulo(
        fazer_titulo().model_copy(update={"seu_numero": "---"})
    )
    assert "clientNumber" not in falso.ultimo_corpo
    with pytest.raises(Validacao, match="15"):
        await adapter.registrar_titulo(
            fazer_titulo().model_copy(update={"seu_numero": "1" * 16})
        )


async def test_mensagens_no_limite_oficial(adapter, falso):
    await adapter.registrar_titulo(
        fazer_titulo().model_copy(update={"mensagens": ("x" * 120,)})
    )
    assert falso.ultimo_corpo["messages"] == ["x" * 100]
    with pytest.raises(Validacao, match="45"):
        await adapter.registrar_titulo(
            fazer_titulo().model_copy(update={"mensagens": ("m",) * 46})
        )


async def test_limites_de_multa_juros_e_dias_de_baixa(adapter):
    for encargos in (
        Encargos(multa_percentual="60.01"),
        Encargos(juros_mensal_percentual="61"),
    ):
        with pytest.raises(Validacao, match="60%"):
            await adapter.registrar_titulo(
                fazer_titulo().model_copy(update={"encargos": encargos})
            )
    with pytest.raises(Validacao, match="99"):
        await adapter.registrar_titulo(
            fazer_titulo().model_copy(update={"dias_baixa_automatica": 100})
        )


async def test_beneficiario_final_igual_ao_original_nao_e_enviado(dormir):
    falso = SantanderFalso(raiz_original="91300766")
    sem_config = _adapter(falso, dormir)
    with pytest.raises(Validacao):
        await sem_config.registrar_titulo(fazer_titulo())
    filial = "91300766000228"
    adapter = _adapter(falso, dormir, documento_beneficiario=filial)
    await adapter.registrar_titulo(fazer_titulo())
    assert "beneficiary" not in falso.ultimo_corpo


async def test_beneficiario_final_de_outro_titular_e_enviado(falso, dormir):
    adapter = _adapter(falso, dormir, documento_beneficiario="52998224725")
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["beneficiary"]["documentNumber"] == "91300766000147"
    assert (
        SantanderConfig(
            conta="c",
            client_id="c",
            client_secret="s",
            workspace_id="w",
            convenio="1",
            documento_beneficiario=CNPJ_VALIDO,
        ).documento_beneficiario
        == "91300766000147"
    )


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
    assert falso.boletos[id_banco]["deductionValue"] == "2.50"
    await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="t", tipo=TipoInstrucao.PROTESTO)
    )
    assert falso.boletos[id_banco]["_protesto"] is True
    baixado = await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="b", tipo=TipoInstrucao.BAIXA)
    )
    assert baixado.status is StatusCanonico.BAIXADO


async def test_corpo_da_instrucao_tem_so_os_campos_da_alteracao(falso, dormir):
    corpos = []

    def tratar(request):
        if request.method == "PATCH":
            corpos.append(json.loads(request.content))
        return falso.tratar(request)

    adapter = SantanderAdapter(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(tratar)),
        dormir=dormir,
        hoje=lambda: HOJE,
    )
    registrado = await adapter.registrar_titulo(fazer_titulo())
    id_banco = registrado.id_banco
    base = {"covenantCode": "3567206", "bankNumber": id_banco}
    await adapter.enviar_instrucao(
        id_banco,
        Instrucao(
            chave_idempotencia="p",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=date(2027, 1, 15),
        ),
    )
    await adapter.enviar_instrucao(
        id_banco,
        Instrucao(chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor="2.5"),
    )
    await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="t", tipo=TipoInstrucao.PROTESTO)
    )
    await adapter.baixar_titulo(id_banco)
    assert corpos == [
        {**base, "dueDate": "2027-01-15"},
        {**base, "deductionValue": "2.50"},
        {**base, "operation": "PROTESTAR"},
        {**base, "operation": "BAIXAR"},
    ]


async def test_instrucao_recusada_pelo_banco_e_validacao(falso, dormir):
    transporte = httpx.MockTransport(
        lambda request: (
            santander_falso.erro_400("213", "Boleto nao vencido")
            if request.method == "PATCH"
            else falso.tratar(request)
        )
    )
    adapter = SantanderAdapter(
        _config(),
        client=httpx.AsyncClient(transport=transporte),
        dormir=dormir,
        hoje=lambda: HOJE,
    )
    registrado = await adapter.registrar_titulo(fazer_titulo())
    with pytest.raises(Validacao) as erro:
        await adapter.enviar_instrucao(
            registrado.id_banco,
            Instrucao(chave_idempotencia="t", tipo=TipoInstrucao.PROTESTO),
        )
    assert erro.value.status_http == 400


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


async def test_em_teste_instrucao_nao_chama_o_banco(falso, dormir):
    teste = _adapter(falso, dormir, ambiente="teste")
    with pytest.raises(NaoSuportado):
        await teste.baixar_titulo("1")
    with pytest.raises(NaoSuportado):
        await teste.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="t", tipo=TipoInstrucao.PROTESTO)
        )
    with pytest.raises(NaoSuportado):
        await teste.obter_pdf("1")
    assert falso.chamadas == []


# -- PDF


async def test_pdf_fluxo_oficial(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    pdf = await adapter.obter_pdf(registrado.id_banco)
    assert pdf.startswith(b"%PDF")
    # O documento vem da consulta, em numero, e a ordem do id e nosso.convenio.
    assert falso.ultimo_corpo == {"payerDocumentNumber": 52998224725}
    assert (
        "POST",
        f"{API}/bills/{registrado.id_banco}.3567206/bank_slips",
    ) in falso.chamadas


async def test_download_do_pdf_nao_leva_token(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.obter_pdf(registrado.id_banco)
    assert falso.urls[-1] == santander_falso.LINK_PDF
    assert "authorization" not in falso.cabecalho_download
    assert "x-application-key" not in falso.cabecalho_download


async def test_pdf_de_titulo_baixado_e_recusado(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.baixar_titulo(registrado.id_banco)
    with pytest.raises(Validacao) as erro:
        await adapter.obter_pdf(registrado.id_banco)
    assert erro.value.status_http == 400


@pytest.mark.parametrize(
    ("resposta", "tipo"),
    [
        (httpx.Response(403, content=b"expirado"), Validacao),
        (httpx.Response(200, content=b"<html>"), Validacao),
        (httpx.Response(503, content=b"fora"), Indisponivel),
    ],
)
async def test_download_do_pdf_com_falha_nao_expoe_o_link(
    adapter, falso, resposta, tipo
):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.resposta_pdf = resposta
    with pytest.raises(tipo) as erro:
        await adapter.obter_pdf(registrado.id_banco)
    assert "assinatura-secreta" not in str(erro.value)
    assert "assinatura-secreta" not in repr(erro.value.resposta_bruta)


async def test_erro_de_transporte_no_download_nao_expoe_o_link(falso, dormir):
    def tratar(request):
        if request.url.host == "blob.exemplo.test":
            raise httpx.ConnectError(f"falha em {request.url}")
        return falso.tratar(request)

    adapter = SantanderAdapter(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(tratar)),
        dormir=dormir,
        hoje=lambda: HOJE,
    )
    registrado = await adapter.registrar_titulo(fazer_titulo())
    with pytest.raises(Indisponivel) as erro:
        await adapter.obter_pdf(registrado.id_banco)
    assert "assinatura-secreta" not in str(erro.value)
    assert erro.value.__cause__ is None


async def test_link_sem_https_e_recusado(falso, dormir):
    def tratar(request):
        if request.url.path.endswith("/bank_slips") and "/bills/" in request.url.path:
            return httpx.Response(200, json={"link": "http://inseguro/x.pdf"})
        return falso.tratar(request)

    adapter = SantanderAdapter(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(tratar)),
        dormir=dormir,
        hoje=lambda: HOJE,
    )
    registrado = await adapter.registrar_titulo(fazer_titulo())
    with pytest.raises(Validacao, match="https"):
        await adapter.obter_pdf(registrado.id_banco)


async def test_pdf_sem_documento_numerico_e_recusado(falso, dormir):
    def tratar(request):
        if "tipoConsulta=bankslip" in str(request.url):
            return httpx.Response(
                200, json={"payerData": {"payerDocumentNumber": "12ABC34500000195"}}
            )
        return falso.tratar(request)

    adapter = SantanderAdapter(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(tratar)),
        dormir=dormir,
        hoje=lambda: HOJE,
    )
    with pytest.raises(Validacao, match="documento"):
        await adapter.obter_pdf("1")


# -- webhook


def test_webhook_desligado_nao_e_suportado(adapter):
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook(santander_falso.webhook())


def test_webhook_pagamento_so_avisa(com_webhook):
    eventos = com_webhook.parse_webhook(
        santander_falso.webhook("18"), {"Notification-UUID": "u-1"}
    )
    assert [e.tipo for e in eventos] == [StatusCanonico.LIQUIDADO]
    evento = eventos[0]
    # Sem zeros, para casar com o id_banco.
    assert evento.chave_dedup == ("santander", "18", StatusCanonico.LIQUIDADO)
    assert evento.valor_pago == Decimal("150.10")
    assert evento.ocorrido_em == datetime(2025, 11, 24, 10, 46, 52, 15034)
    assert evento.resposta_bruta["notification-uuid"] == "u-1"
    assert evento.resposta_bruta["paymentType"] == "PIX"


def test_webhook_mascara_dados_pessoais(com_webhook):
    evento = com_webhook.parse_webhook(santander_falso.webhook())[0]
    texto = repr(evento.resposta_bruta)
    assert "JOAO DA SILVA" not in texto
    assert "00000015860" not in texto
    assert "00111222000105" not in texto


def test_webhook_aceita_dict_sem_float(com_webhook):
    corpo = json.loads(santander_falso.webhook("18"))
    evento = com_webhook.parse_webhook(corpo)[0]
    assert evento.valor_pago == Decimal("150.10")


def test_webhook_de_estorno_devolve_titulo_aberto(com_webhook):
    evento = com_webhook.parse_webhook(santander_falso.webhook("18", "ESTORNO"))[0]
    assert evento.tipo is StatusCanonico.REGISTRADO
    assert evento.valor_pago is None


@pytest.mark.parametrize(
    "corpo",
    [
        {"message": "Webhook URL validation"},
        {"message": "Validacao da URL do Webhook"},
        b"[]",
    ],
)
def test_validacao_da_url_nao_gera_evento(com_webhook, corpo):
    assert com_webhook.parse_webhook(corpo) == []


@pytest.mark.parametrize(
    "corpo",
    [
        b"nao e json",
        b'"texto"',
        {"message": "OUTRA", "bankNumber": "1"},
        {"bankNumber": "1", "function": "PAGAMENTO"},
        {"message": "WBHKPAGEST", "function": "PAGAMENTO"},
        {"message": "WBHKPAGEST", "bankNumber": "1", "function": "OUTRA"},
        {
            "message": "WBHKPAGEST",
            "bankNumber": "1",
            "function": "PAGAMENTO",
            "paymentDate": "ontem",
        },
    ],
)
def test_webhook_fora_do_formato_e_recusado(com_webhook, corpo):
    with pytest.raises(Validacao):
        com_webhook.parse_webhook(corpo)


def test_webhook_de_outro_convenio_e_recusado(com_webhook):
    with pytest.raises(Validacao, match="convenio"):
        com_webhook.parse_webhook(santander_falso.webhook("18", convenio="999"))


def test_webhook_contrato_comum(com_webhook):
    # Mesmo roteiro de tests/test_contrato.py::test_webhook.
    eventos = com_webhook.parse_webhook(santander_falso.webhook("cod-1"))
    assert [e.tipo for e in eventos] == [StatusCanonico.LIQUIDADO]
    assert eventos[0].chave_dedup == (
        "santander",
        "cod-1",
        StatusCanonico.LIQUIDADO,
    )


async def test_webhook_mais_reconsulta_de_pagamento_de_hoje_nao_liquida(
    com_webhook, falso
):
    # O evento diz pago; a consulta diz Ativo ate D+1, e a consulta e quem vale.
    registrado = await com_webhook.registrar_titulo(fazer_titulo())
    falso.pagar_hoje(registrado.id_banco)
    evento = com_webhook.parse_webhook(santander_falso.webhook(registrado.id_banco))[0]
    assert evento.referencia == registrado.id_banco
    consultado = await com_webhook.consultar_titulo(evento.referencia)
    assert consultado.status is StatusCanonico.REGISTRADO
    falso.liquidar(registrado.id_banco)
    consultado = await com_webhook.consultar_titulo(evento.referencia)
    assert consultado.status is StatusCanonico.LIQUIDADO


# -- config


@pytest.mark.parametrize(
    "extra",
    [
        {"ambiente": "homologacao"},
        {"workspace_id": ""},
        {"convenio": "12A"},
        {"convenio": "1234567890"},
        {"especie": "XX"},
        {"especie": "BDA"},
        {"chave_pix": "x@y.com"},
        {"chave_pix": "x@y.com", "tipo_chave_pix": "TELEFONE"},
        {"documento_beneficiario": "123"},
    ],
)
def test_config_invalida(extra):
    with pytest.raises(Validacao):
        _config(**extra)


@pytest.mark.parametrize(
    ("sigla", "enum"),
    [
        ("DM", "DUPLICATA_MERCANTIL"),
        ("NS", "DUPLICATA_SERVICO"),
        ("NP", "NOTA_PROMISSORIA"),
        ("NPR", "NOTA_PROMISSORIA_RURAL"),
        ("REC", "RECIBO"),
        ("AP", "APOLICE_SEGURO"),
        ("CH", "CHEQUE"),
        ("NPD", "NOTA_PROMISSORIA_DIRETA"),
        ("OUTROS", "OUTROS"),
    ],
)
async def test_especies_do_enum_oficial(falso, dormir, sigla, enum):
    adapter = _adapter(falso, dormir, especie=sigla)
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["documentKind"] == enum


def test_repr_da_config_nao_vaza_segredos():
    texto = repr(
        _config(
            certificado_pem=b"cert-pem",
            chave_pem=b"chave-pem",
            chave_pix="pix@kmee.com.br",
            tipo_chave_pix="EMAIL",
            documento_beneficiario=CNPJ_VALIDO,
        )
    )
    assert "segredo-xyz" not in texto
    assert "cert-pem" not in texto
    assert "pix@kmee.com.br" not in texto
    assert "91300766000147" not in texto
