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
from brbank.adapters.bradesco import BradescoAdapter, BradescoConfig
from tests.bradesco_falso import BAIXA, CONSULTA, REGISTRO, BradescoFalso
from tests.conftest import fazer_titulo


def _config(**extra) -> BradescoConfig:
    base = {
        "conta": "c1",
        "client_id": "cid",
        "client_secret": "segredo-cs",
        "documento_beneficiario": "91.300.766/0001-47",
        "agencia": "2856",
        "conta_corrente": "22654",
        "carteira": 9,
    }
    return BradescoConfig(**{**base, **extra})


@pytest.fixture
def falso():
    return BradescoFalso()


@pytest.fixture
def adapter(falso, dormir):
    return BradescoAdapter(
        _config(), client=falso.cliente(), dormir=dormir, hoje=lambda: date(2026, 11, 1)
    )


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("bradesco") is BradescoAdapter
    assert BradescoAdapter.validado_sandbox is False
    assert BradescoAdapter.capacidades == Capacidade.BAIXA


async def test_token_client_credentials_no_formulario(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.chamadas[0] == ("POST", "/auth/server-mtls/v2/token")
    assert falso.ultimo_form == {
        "client_id": ["cid"],
        "client_secret": ["segredo-cs"],
        "grant_type": ["client_credentials"],
    }
    assert falso.ultimo_cabecalho["authorization"] == "Bearer tok-1"
    assert falso.ultimo_host == "openapi.bradesco.com.br"


async def test_token_cru_quando_configurado(falso, dormir):
    adapter = BradescoAdapter(
        _config(token_sem_bearer=True, ambiente="sandbox"),
        client=falso.cliente(),
        dormir=dormir,
    )
    await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_cabecalho["authorization"] == "tok-1"
    assert falso.ultimo_host == "openapisandbox.prebanco.com.br"


async def test_token_reaproveitado(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    await adapter.registrar_titulo(fazer_titulo("chave-2"))
    assert falso.tokens_emitidos == 1


async def test_payload_de_registro(adapter, falso):
    titulo = fazer_titulo(valor="150.005").model_copy(
        update={
            "encargos": Encargos(multa_percentual="2", juros_mensal_percentual="1"),
            "mensagens": ("linha um", "Jose da Conceicao"),
        }
    )
    registrado = await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert falso.chamadas[-1] == ("POST", REGISTRO)
    assert (corpo["nuCPFCNPJ"], corpo["filialCPFCNPJ"], corpo["ctrlCPFCNPJ"]) == (
        "91300766",
        "0001",
        "47",
    )
    assert corpo["idProduto"] == 9
    assert corpo["nuNegociacao"] == "285600000000022654"
    assert corpo["nuCliente"] == "FAT-chave-1"
    assert corpo["dtEmissaoTitulo"] == "01.11.2026"
    assert corpo["dtVencimentoTitulo"] == "10.12.2026"
    assert corpo["tpVencimento"] == 0
    assert corpo["vlNominalTitulo"] == 15001
    assert corpo["cdEspecieTitulo"] == 2
    assert corpo["nomePagador"] == "Maria da Silva"
    assert corpo["logradouroPagador"] == "Rua das Flores"
    assert corpo["nuLogradouroPagador"] == "10"
    assert (corpo["cepPagador"], corpo["complementoCepPagador"]) == ("30110", "000")
    assert corpo["ufPagador"] == "MG"
    assert corpo["cdIndCpfcnpjPagador"] == 1
    assert corpo["nuCpfcnpjPagador"] == "52998224725"
    assert corpo["percentualMulta"] == 2.0
    assert corpo["qtdeDiasMulta"] == 1
    assert corpo["percentualJuros"] == 1.0
    assert corpo["qtdeDiasJuros"] == 1
    assert corpo["nomeSacadorAvalista"] == "KMEE Ltda"
    assert corpo["cdIndCpfcnpjSacadorAvalista"] == 2
    assert corpo["nuCpfcnpjSacadorAvalista"] == "91300766000147"
    assert corpo["listaMsgs"] == [
        {"mensagem": "linha um"},
        {"mensagem": "Jose da Conceicao"},
    ]
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.id_banco == registrado.nosso_numero == "90000000001"
    assert registrado.linha_digitavel and registrado.codigo_barras


async def test_sem_encargos_nao_envia_grupo(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    corpo = falso.ultimo_corpo
    for campo in ("percentualMulta", "qtdeDiasMulta", "percentualJuros", "listaMsgs"):
        assert campo not in corpo


async def test_texto_sem_acento_e_no_tamanho(adapter, falso):
    titulo = fazer_titulo()
    pagador = titulo.pagador.model_copy(update={"nome": "Jos\u00e9 " + "x" * 80})
    await adapter.registrar_titulo(titulo.model_copy(update={"pagador": pagador}))
    nome = falso.ultimo_corpo["nomePagador"]
    assert nome.startswith("Jose x")
    assert len(nome) == 70
    assert nome.isascii()


async def test_valor_em_texto_quando_configurado(falso, dormir):
    adapter = BradescoAdapter(
        _config(valor_em_centavos=False), client=falso.cliente(), dormir=dormir
    )
    await adapter.registrar_titulo(fazer_titulo(valor="150.5"))
    assert falso.ultimo_corpo["vlNominalTitulo"] == "150.50"


@pytest.mark.parametrize(
    "encargos",
    [
        Encargos(desconto_percentual="5", desconto_dias_antes=3),
        Encargos(abatimento="1.50"),
    ],
)
async def test_desconto_e_abatimento_sao_recusados(adapter, falso, encargos):
    titulo = fazer_titulo().model_copy(update={"encargos": encargos})
    with pytest.raises(Validacao, match="desconto"):
        await adapter.registrar_titulo(titulo)
    assert falso.chamadas == []


async def test_baixa_automatica_e_recusada(adapter):
    titulo = fazer_titulo().model_copy(update={"dias_baixa_automatica": 30})
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(titulo)


async def test_pagador_sem_endereco_e_recusado(adapter):
    titulo = fazer_titulo()
    sem = titulo.pagador.model_copy(update={"endereco": None})
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(titulo.model_copy(update={"pagador": sem}))


async def test_registro_sem_nosso_numero_e_validacao(falso, dormir):
    def recusa(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        return httpx.Response(
            200, json={"status": 422, "causa": "CBTT0505 - INFORME TODOS OS CAMPOS"}
        )

    adapter = BradescoAdapter(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(recusa)),
        dormir=dormir,
    )
    with pytest.raises(Validacao, match="nuTituloGerado") as erro:
        await adapter.registrar_titulo(fazer_titulo())
    assert erro.value.status_http == 422
    assert erro.value.resposta_bruta["causa"].startswith("CBTT0505")


async def test_consulta_envia_identificacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert falso.chamadas[-1] == ("POST", CONSULTA)
    assert falso.ultimo_corpo == {
        "cpfCnpj": {"cpfCnpj": 91300766, "filial": 1, "controle": 47},
        "produto": 9,
        "negociacao": 28560022654,
        "nossoNumero": 90000000001,
        "sequencia": 0,
        "status": 0,
    }
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.seu_numero == "FAT-chave-1"
    assert consultado.valor_pago is None


async def test_consulta_mascara_pii_da_resposta(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.resposta_bruta["nomePagador"] == "***"
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    bruta = consultado.resposta_bruta["titulo"]
    assert bruta["nomeSacado"] == "***"
    assert bruta["cnpjSacado"] == "***"
    assert "Maria" not in repr(consultado.resposta_bruta)


async def test_consulta_liquidada(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.00")
    assert consultado.data_pagamento == date(2026, 11, 10)


async def test_pago_no_dia_sem_valor_nem_data(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.titulos[registrado.id_banco].update(codStatus="13", dtPagto=0, vlrPagto=0)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago is None
    assert consultado.data_pagamento is None


async def test_data_de_pagamento_sem_zero_a_esquerda(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    falso.titulos[registrado.id_banco]["dtPagto"] = 1102026
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.data_pagamento == date(2026, 10, 1)


@pytest.mark.parametrize("codigo", [51, 99, None])
async def test_status_desconhecido_vira_validacao(adapter, falso, codigo):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.titulos[registrado.id_banco]["codStatus"] = codigo
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_consulta_com_erro_no_envelope(falso, dormir):
    def recusa(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        return httpx.Response(
            200, json={"status": 412, "causa": "CBTT0552 - TITULO INEXISTENTE"}
        )

    adapter = BradescoAdapter(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(recusa)),
        dormir=dormir,
    )
    with pytest.raises(Validacao, match="titulo") as erro:
        await adapter.consultar_titulo("123")
    assert erro.value.status_http == 412


async def test_baixa(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(chave_idempotencia="b", tipo=TipoInstrucao.BAIXA),
    )
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.chamadas[-1] == ("POST", BAIXA)
    assert falso.ultimo_corpo == {
        "cpfCnpj": {"cpfCnpj": "91300766", "filial": "0001", "controle": "47"},
        "produto": 9,
        "negociacao": 28560022654,
        "nossoNumero": registrado.id_banco,
        "sequencia": 0,
        "codigoBaixa": 57,
    }


async def test_baixa_recusada_no_envelope(falso, dormir):
    def recusa(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        return httpx.Response(200, json={"status": 422, "causa": "CODIGO INVALIDO"})

    adapter = BradescoAdapter(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(recusa)),
        dormir=dormir,
    )
    with pytest.raises(Validacao, match="baixa"):
        await adapter.baixar_titulo("123")


async def test_beneficiario_cpf_decomposto(falso, dormir):
    adapter = BradescoAdapter(
        _config(documento_beneficiario="529.982.247-25"),
        client=falso.cliente(),
        dormir=dormir,
    )
    await adapter.registrar_titulo(fazer_titulo())
    corpo = falso.ultimo_corpo
    assert (corpo["nuCPFCNPJ"], corpo["filialCPFCNPJ"], corpo["ctrlCPFCNPJ"]) == (
        "529982247",
        "0",
        "25",
    )


@pytest.mark.parametrize(
    "tipo",
    [TipoInstrucao.PRORROGACAO, TipoInstrucao.ABATIMENTO, TipoInstrucao.PROTESTO],
)
async def test_instrucoes_sem_contrato(adapter, tipo):
    with pytest.raises(NaoSuportado):
        await adapter.enviar_instrucao(
            "1",
            Instrucao(
                chave_idempotencia="x",
                tipo=tipo,
                nova_data_vencimento=date(2027, 1, 1),
                valor="1",
            ),
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
        {"agencia": "12345"},
        {"conta_corrente": "12345678"},
        {"conta_corrente": "12-3"},
        {"carteira": 0},
        {"especie": "XYZ"},
        {"documento_beneficiario": "11.222.333/0001-00"},
        {"documento_beneficiario": "12ABC34501DE35"},
    ],
)
def test_config_invalida(extra):
    with pytest.raises(Validacao):
        _config(**extra)


def test_repr_da_config_nao_vaza_segredos():
    texto = repr(_config(certificado_pem=b"cert-pem", chave_pem=b"chave-pem"))
    for segredo in ("segredo-cs", "cert-pem", "chave-pem", "91300766000147"):
        assert segredo not in texto
