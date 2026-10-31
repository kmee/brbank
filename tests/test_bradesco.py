import json
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from brbank import (
    Capacidade,
    Encargos,
    Indisponivel,
    Instrucao,
    NaoSuportado,
    StatusCanonico,
    TipoInstrucao,
    Validacao,
    obter_classe,
)
from brbank.adapters.bradesco import TABELA_STATUS, BradescoAdapter, BradescoConfig
from brbank.transporte import PoliticaRetry
from tests.bradesco_falso import (
    ALTERACAO,
    ALTERACAO_HIBRIDO,
    BAIXA,
    BARRAS_DIGITOS,
    CONSULTA,
    CONSULTA_HIBRIDO,
    EMV,
    LINHA_DIGITOS,
    PROTESTO,
    REGISTRO,
    REGISTRO_HIBRIDO,
    TXID,
    WEBHOOK_PRODUCAO,
    WEBHOOK_SANDBOX,
    BradescoFalso,
    webhook_pagamento,
)
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


def _adapter(falso, dormir, **extra) -> BradescoAdapter:
    return BradescoAdapter(
        _config(**extra),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: date(2026, 11, 1),
    )


def _instrucao(tipo: TipoInstrucao, **extra) -> Instrucao:
    return Instrucao(chave_idempotencia="i", tipo=tipo, **extra)


def _resposta_fixa(corpo: dict, dormir, **extra) -> BradescoAdapter:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        return httpx.Response(200, json=corpo)

    return BradescoAdapter(
        _config(**extra),
        client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
        dormir=dormir,
    )


@pytest.fixture
def falso():
    return BradescoFalso()


@pytest.fixture
def adapter(falso, dormir):
    return _adapter(falso, dormir)


@pytest.fixture
def hibrido(falso, dormir):
    return _adapter(falso, dormir, hibrido=True)


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("bradesco") is BradescoAdapter
    assert BradescoAdapter.validado_sandbox is False


def test_capacidades_seguem_a_config(falso, dormir):
    base = Capacidade.ALTERACAO | Capacidade.BAIXA
    assert _adapter(falso, dormir).capacidades == base
    assert _adapter(falso, dormir, hibrido=True).capacidades == (
        base | Capacidade.HIBRIDO
    )
    assert _adapter(falso, dormir, webhook=True).capacidades == (
        base | Capacidade.WEBHOOK
    )
    assert not _adapter(falso, dormir, hibrido=True).suporta(Capacidade.PDF)


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
    adapter = _adapter(falso, dormir, token_sem_bearer=True, ambiente="sandbox")
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
    assert corpo["cdEspecieTitulo"] == 2
    assert corpo["nomePagador"] == "Maria da Silva"
    assert corpo["logradouroPagador"] == "Rua das Flores"
    assert corpo["nuLogradouroPagador"] == "10"
    assert (corpo["cepPagador"], corpo["complementoCepPagador"]) == ("30110", "000")
    assert corpo["ufPagador"] == "MG"
    assert corpo["cdIndCpfcnpjPagador"] == 1
    assert corpo["nuCpfcnpjPagador"] == "52998224725"
    assert corpo["endEletronicoPagador"] == "maria@example.com"
    assert corpo["nomeSacadorAvalista"] == "KMEE Ltda"
    assert corpo["cdIndCpfcnpjSacadorAvalista"] == 2
    assert corpo["nuCpfcnpjSacadorAvalista"] == "91300766000147"
    assert corpo["listaMsgs"] == [
        {"mensagem": "linha um"},
        {"mensagem": "Jose da Conceicao"},
    ]
    assert "nuTitulo" not in corpo
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.id_banco == registrado.nosso_numero == "90000000001"


async def test_valor_e_percentuais_em_texto_com_duas_casas(adapter, falso):
    """Oficial (manual e collection): "500.00". Centavos seria o erro de 100 vezes."""
    titulo = fazer_titulo(valor="150.5").model_copy(
        update={
            "encargos": Encargos(multa_percentual="2", juros_mensal_percentual="1.5")
        }
    )
    await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["vlNominalTitulo"] == "150.50"
    assert corpo["percentualMulta"] == "2.00"
    assert corpo["percentualJuros"] == "1.50"


async def test_modo_digitos_quando_configurado(falso, dormir):
    adapter = _adapter(falso, dormir, valor_em_centavos=True)
    titulo = fazer_titulo(valor="150.00").model_copy(
        update={"encargos": Encargos(multa_percentual="10")}
    )
    await adapter.registrar_titulo(titulo)
    assert falso.ultimo_corpo["vlNominalTitulo"] == 15000
    assert falso.ultimo_corpo["percentualMulta"] == "10000"


async def test_valor_divergente_no_retorno_e_recusa_com_nosso_numero(falso, dormir):
    adapter = _adapter(falso, dormir)
    falso.eco_valor = 150
    with pytest.raises(Validacao, match=r"90000000001.*valor_em_centavos"):
        await adapter.registrar_titulo(fazer_titulo())


async def test_dias_de_encargo_somam_um_no_convencional(adapter, falso):
    """Manual: para iniciar 2 dias depois, enviar 3; logo, 1 dia depois = 2."""
    titulo = fazer_titulo().model_copy(
        update={"encargos": Encargos(multa_percentual="2", juros_mensal_percentual="1")}
    )
    await adapter.registrar_titulo(titulo)
    assert falso.ultimo_corpo["qtdeDiasMulta"] == 2
    assert falso.ultimo_corpo["qtdeDiasJuros"] == 2


async def test_dias_de_encargo_configuraveis(falso, dormir):
    adapter = _adapter(falso, dormir, dias_encargos=1)
    titulo = fazer_titulo().model_copy(
        update={"encargos": Encargos(multa_percentual="2")}
    )
    await adapter.registrar_titulo(titulo)
    assert falso.ultimo_corpo["qtdeDiasMulta"] == 1


async def test_desconto_abatimento_e_baixa_automatica_no_registro(adapter, falso):
    titulo = fazer_titulo().model_copy(
        update={
            "encargos": Encargos(
                desconto_percentual="5", desconto_dias_antes=3, abatimento="1.50"
            ),
            "dias_baixa_automatica": 30,
        }
    )
    await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["percentualDesconto1"] == "5.00"
    assert corpo["dataLimiteDesconto1"] == "07.12.2026"
    assert corpo["vlAbatimento"] == "1.50"
    assert corpo["tipoPrazoDecursoTres"] == 30


async def test_sem_encargos_nao_envia_grupo(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    corpo = falso.ultimo_corpo
    for campo in (
        "percentualMulta",
        "qtdeDiasMulta",
        "percentualJuros",
        "percentualDesconto1",
        "vlAbatimento",
        "tipoPrazoDecursoTres",
        "listaMsgs",
    ):
        assert campo not in corpo


async def test_mais_casas_que_o_campo_e_recusado(adapter, falso):
    titulo = fazer_titulo().model_copy(
        update={"encargos": Encargos(multa_percentual="1.333")}
    )
    with pytest.raises(Validacao, match="casas"):
        await adapter.registrar_titulo(titulo)
    assert falso.chamadas == []


async def test_endereco_sem_pontuacao_nem_acento(adapter, falso):
    titulo = fazer_titulo()
    endereco = titulo.pagador.endereco.model_copy(
        update={
            "logradouro": "Av. S\u00e3o-Jo\u00e3o/Centro",
            "numero": "10-A",
            "complemento": "Sala 3, 2\u00ba andar",
            "bairro": "Sta. Ceci\u0301lia",
            "cidade": "S\u00e3o Jos\u00e9 do Rio-Preto",
        }
    )
    pagador = titulo.pagador.model_copy(
        update={"endereco": endereco, "nome": "Jos\u00e9 & Filhos Ltda."}
    )
    await adapter.registrar_titulo(titulo.model_copy(update={"pagador": pagador}))
    corpo = falso.ultimo_corpo
    assert corpo["logradouroPagador"] == "Av Sao Joao Centro"
    assert corpo["nuLogradouroPagador"] == "10 A"
    assert corpo["complementoLogradouroPagador"] == "Sala 3 2o andar"
    assert corpo["bairroPagador"] == "Sta Cecilia"
    assert corpo["municipioPagador"] == "Sao Jose do Rio Preto"
    assert corpo["nomePagador"] == "Jose Filhos Ltda"


async def test_sacador_com_numero_separado_do_logradouro(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    corpo = falso.ultimo_corpo
    assert corpo["logradouroSacadorAvalista"] == "Rua das Flores"
    assert corpo["nuLogradouroSacadorAvalista"] == "10"


async def test_nome_cortado_no_tamanho(adapter, falso):
    titulo = fazer_titulo()
    pagador = titulo.pagador.model_copy(update={"nome": "Jos\u00e9 " + "x" * 80})
    await adapter.registrar_titulo(titulo.model_copy(update={"pagador": pagador}))
    nome = falso.ultimo_corpo["nomePagador"]
    assert nome.startswith("Jose x")
    assert len(nome) == 70
    assert nome.isascii()


async def test_email_maior_que_o_campo_nao_vai_cortado(adapter, falso):
    titulo = fazer_titulo()
    pagador = titulo.pagador.model_copy(update={"email": "x" * 71 + "@a.br"})
    await adapter.registrar_titulo(titulo.model_copy(update={"pagador": pagador}))
    assert "endEletronicoPagador" not in falso.ultimo_corpo


async def test_linha_e_barras_viram_digitos(adapter, falso):
    """O exemplo oficial traz a linha com pontos e o codigo no padrao EBCDIC."""
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.linha_digitavel == LINHA_DIGITOS
    assert registrado.codigo_barras == BARRAS_DIGITOS
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.linha_digitavel == LINHA_DIGITOS
    assert consultado.codigo_barras == BARRAS_DIGITOS


async def test_pagador_sem_endereco_e_recusado(adapter):
    titulo = fazer_titulo()
    sem = titulo.pagador.model_copy(update={"endereco": None})
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(titulo.model_copy(update={"pagador": sem}))


async def test_registro_sem_nosso_numero_e_validacao(dormir):
    adapter = _resposta_fixa(
        {"status": 422, "causa": "CBTT0505 - INFORME TODOS OS CAMPOS"}, dormir
    )
    with pytest.raises(Validacao, match="nuTituloGerado") as erro:
        await adapter.registrar_titulo(fazer_titulo())
    assert erro.value.status_http == 422
    assert erro.value.resposta_bruta["causa"].startswith("CBTT0505")


def _adapter_sem_rede(dormir, **extra) -> tuple[BradescoAdapter, list[str]]:
    tentativas: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        tentativas.append(request.url.path)
        raise httpx.ReadTimeout("sem resposta", request=request)

    adapter = BradescoAdapter(
        _config(**extra),
        client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
        dormir=dormir,
        retry=PoliticaRetry(tentativas=3, base_s=0, maximo_s=0, jitter=0),
    )
    return adapter, tentativas


async def test_registro_sem_nosso_numero_proprio_nao_repete(dormir):
    adapter, tentativas = _adapter_sem_rede(dormir)
    with pytest.raises(Indisponivel):
        await adapter.registrar_titulo(fazer_titulo())
    assert tentativas == [REGISTRO]


async def test_registro_com_nosso_numero_proprio_pode_repetir(dormir):
    adapter, tentativas = _adapter_sem_rede(dormir, gerar_nosso_numero=lambda _t: 7)
    with pytest.raises(Indisponivel):
        await adapter.registrar_titulo(fazer_titulo())
    assert len(tentativas) == 3


async def test_nosso_numero_do_beneficiario(falso, dormir):
    adapter = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: 123)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["nuTitulo"] == 123
    assert registrado.id_banco == "123"


async def test_nosso_numero_acima_de_11_digitos_e_recusado(falso, dormir):
    adapter = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: 10**11)
    with pytest.raises(Validacao, match="11 digitos"):
        await adapter.registrar_titulo(fazer_titulo())


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


@pytest.mark.parametrize("pago", [15000.0, 3.0])
async def test_pagamento_fora_de_escala_nao_liquida(adapter, falso, pago):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    falso.titulos[registrado.id_banco]["vlrPagto"] = pago
    with pytest.raises(Validacao, match="vlr_pagto_em_centavos"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_pagamento_parcial_pequeno_e_aceito(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    falso.titulos[registrado.id_banco].update(vlrPagto=2.0, permitePgtoParcial="S")
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.valor_pago == Decimal("2.00")


async def test_vlr_pagto_em_centavos_quando_configurado(falso, dormir):
    adapter = _adapter(falso, dormir, vlr_pagto_em_centavos=True)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    falso.titulos[registrado.id_banco]["vlrPagto"] = 15000.0
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.valor_pago == Decimal("150.00")


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        (1, StatusCanonico.REGISTRADO),
        ("02", StatusCanonico.REGISTRADO),  # pagamento vinculado nao liquida
        (4, StatusCanonico.REGISTRADO),
        (11, StatusCanonico.REGISTRADO),  # baixa so solicitada
        (30, StatusCanonico.REGISTRADO),  # pagamento retido
        (33, StatusCanonico.REGISTRADO),
        (99, StatusCanonico.REGISTRADO),
        (13, StatusCanonico.LIQUIDADO),
        (61, StatusCanonico.LIQUIDADO),
        (62, StatusCanonico.LIQUIDADO),
        (53, StatusCanonico.BAIXADO),
        (57, StatusCanonico.BAIXADO),
        (58, StatusCanonico.BAIXADO),
        (98, StatusCanonico.BAIXADO),
    ],
)
async def test_tabela_oficial_de_status(adapter, falso, codigo, esperado):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.titulos[registrado.id_banco].update(codStatus=codigo, vlrPagto=0)
    assert (await adapter.consultar_titulo(registrado.id_banco)).status is esperado


@pytest.mark.parametrize(
    "codigo", [51, 56, 65, 66, 67, 68, 69, 70, 71, 72, 73, 36, 74, None]
)
async def test_status_sem_prova_de_pagamento_vira_validacao(adapter, falso, codigo):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.titulos[registrado.id_banco]["codStatus"] = codigo
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


def test_tabela_cobre_exatamente_os_codigos_decididos():
    cobertos = {int(c) for c in TABELA_STATUS.mapa}
    assert not cobertos & {51, 56, 65, 66, 67, 68, 69, 70, 71, 72, 73}
    assert cobertos >= {*range(1, 36), 52, 53, 54, 55, 57, 58, 59, 60, 61, 62, 63, 64}


async def test_consulta_com_erro_no_envelope(dormir):
    adapter = _resposta_fixa(
        {"status": 412, "causa": "CBTT0552 - TITULO INEXISTENTE"}, dormir
    )
    with pytest.raises(Validacao, match="titulo") as erro:
        await adapter.consultar_titulo("123")
    assert erro.value.status_http == 412


async def test_baixa(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.enviar_instrucao(
        registrado.id_banco, _instrucao(TipoInstrucao.BAIXA)
    )
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.chamadas[-1] == ("POST", BAIXA)
    assert falso.ultimo_corpo == {
        "cpfCnpj": {"cpfCnpj": "91300766", "filial": "0001", "controle": "47"},
        "produto": 9,
        "negociacao": 28560022654,
        "nossoNumero": int(registrado.id_banco),
        "sequencia": 0,
        "codigoBaixa": 57,
    }


async def test_baixa_recusada_no_envelope(dormir):
    adapter = _resposta_fixa({"status": 412, "causa": "CBTT0552"}, dormir)
    with pytest.raises(Validacao, match="baixa"):
        await adapter.baixar_titulo("123")


async def test_baixa_apenas_solicitada_nao_vira_baixado(dormir):
    adapter = _resposta_fixa({"status": 200, "dados": {"status": 11}}, dormir)
    assert (await adapter.baixar_titulo("123")).status is StatusCanonico.REGISTRADO


async def test_prorrogacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    alterado = await adapter.enviar_instrucao(
        registrado.id_banco,
        _instrucao(TipoInstrucao.PRORROGACAO, nova_data_vencimento=date(2027, 1, 5)),
    )
    alteracao = falso.corpos[-2]
    assert falso.chamadas[-2] == ("PUT", ALTERACAO)
    assert falso.chamadas[-1] == ("POST", CONSULTA)
    assert alteracao["cpfCnpj"] == {"cpfCnpj": 91300766, "filial": 1, "controle": 47}
    assert (alteracao["produto"], alteracao["negociacao"]) == (9, 28560022654)
    assert alteracao["nossoNumero"] == int(registrado.id_banco)
    assert alteracao["dadosTitulo"]["vencimento"] == {
        "dataVencimento": 5012027,
        "tipoVencimento": 0,
    }
    assert alteracao["dadosTitulo"]["abatimento"] == {
        "tipoAbatimento": 0,
        "valorAbatimento": 0,
    }
    assert alteracao["dadosPagador"]["sacado"] == ""
    assert alterado.status is StatusCanonico.REGISTRADO


async def test_abatimento(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    alterado = await adapter.enviar_instrucao(
        registrado.id_banco, _instrucao(TipoInstrucao.ABATIMENTO, valor="5.00")
    )
    dados = falso.corpos[-2]["dadosTitulo"]
    assert dados["abatimento"] == {"tipoAbatimento": 1, "valorAbatimento": "5.00"}
    assert dados["vencimento"]["dataVencimento"] == 0
    assert alterado.resposta_bruta["titulo"]["valAbat"] == 500


@pytest.mark.parametrize(
    ("tipo", "extra"),
    [(TipoInstrucao.PRORROGACAO, {}), (TipoInstrucao.ABATIMENTO, {})],
)
async def test_instrucao_incompleta_e_recusada(adapter, falso, tipo, extra):
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao("1", _instrucao(tipo, **extra))
    assert falso.chamadas == []


async def test_alteracao_recusada_no_envelope(dormir):
    adapter = _resposta_fixa(
        {"status": 412, "causa": "CBTT0401 - SITUACAO INVALIDA"}, dormir
    )
    with pytest.raises(Validacao, match="alteracao") as erro:
        await adapter.enviar_instrucao(
            "123",
            _instrucao(
                TipoInstrucao.PRORROGACAO, nova_data_vencimento=date(2027, 1, 5)
            ),
        )
    assert erro.value.status_http == 412


async def test_abatimento_gravado_em_outra_escala_e_erro(adapter, falso, monkeypatch):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    original = falso._alterar

    def errado(corpo):
        resposta = original(corpo)
        falso.titulos[registrado.id_banco]["valAbat"] = 50000
        return resposta

    monkeypatch.setattr(falso, "_alterar", errado)
    with pytest.raises(Validacao, match="outro abatimento"):
        await adapter.enviar_instrucao(
            registrado.id_banco, _instrucao(TipoInstrucao.ABATIMENTO, valor="5.00")
        )


async def test_vencimento_gravado_diferente_e_erro(adapter, falso, monkeypatch):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    original = falso._alterar

    def errado(corpo):
        resposta = original(corpo)
        falso.titulos[registrado.id_banco]["dataVencto"] = "10/12/2026"
        return resposta

    monkeypatch.setattr(falso, "_alterar", errado)
    with pytest.raises(Validacao, match="outro vencimento"):
        await adapter.enviar_instrucao(
            registrado.id_banco,
            _instrucao(
                TipoInstrucao.PRORROGACAO, nova_data_vencimento=date(2027, 1, 5)
            ),
        )


async def test_alteracao_aceita_com_consulta_fora_do_ar(dormir):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        if request.url.path == ALTERACAO:
            return httpx.Response(200, json={"status": 200})
        return httpx.Response(503)

    adapter = BradescoAdapter(
        _config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
        dormir=dormir,
        retry=PoliticaRetry(tentativas=1),
    )
    alterado = await adapter.enviar_instrucao(
        "123", _instrucao(TipoInstrucao.ABATIMENTO, valor="5.00")
    )
    assert alterado.status is StatusCanonico.REGISTRADO


@pytest.mark.parametrize(
    ("tipo", "funcao", "parametro"),
    [(TipoInstrucao.PROTESTO, "1", "P"), (TipoInstrucao.NEGATIVACAO, "3", "")],
)
async def test_protesto_e_negativacao(adapter, falso, tipo, funcao, parametro):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    resultado = await adapter.enviar_instrucao(registrado.id_banco, _instrucao(tipo))
    assert falso.chamadas[-1] == ("POST", PROTESTO)
    assert falso.ultimo_corpo == {
        "cpfCnpj": {"cpfCnpj": "091300766", "filial": "0001", "controle": "47"},
        "codigoProduto": "9",
        "contaProduto": "28560022654",
        "nossoNumero": registrado.id_banco,
        "codigoFuncao": funcao,
        "parmFuncao": parametro,
    }
    assert resultado.status is StatusCanonico.REGISTRADO


async def test_protesto_recusado(dormir):
    adapter = _resposta_fixa(
        {"status": 412, "causa": "CBTT0987 - SITUACAO INVALIDA"}, dormir
    )
    with pytest.raises(Validacao, match="protesto") as erro:
        await adapter.enviar_instrucao("1", _instrucao(TipoInstrucao.PROTESTO))
    assert erro.value.status_http == 412


async def test_beneficiario_cpf_decomposto(falso, dormir):
    adapter = _adapter(falso, dormir, documento_beneficiario="529.982.247-25")
    await adapter.registrar_titulo(fazer_titulo())
    corpo = falso.ultimo_corpo
    assert (corpo["nuCPFCNPJ"], corpo["filialCPFCNPJ"], corpo["ctrlCPFCNPJ"]) == (
        "529982247",
        "0",
        "25",
    )


async def test_pdf_nao_suportado(adapter):
    with pytest.raises(NaoSuportado):
        await adapter.obter_pdf("1")


# -- boleto com QR Code ------------------------------------------------------


async def test_hibrido_registro_em_centavos_e_com_qrcode(hibrido, falso):
    titulo = fazer_titulo(valor="150.50").model_copy(
        update={
            "encargos": Encargos(
                multa_percentual="2",
                juros_mensal_percentual="1",
                desconto_percentual="5",
                desconto_dias_antes=3,
                abatimento="1.50",
            ),
            "dias_baixa_automatica": 30,
        }
    )
    registrado = await hibrido.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert falso.chamadas[-1] == ("POST", REGISTRO_HIBRIDO)
    assert corpo["vnmnalTitloCobr"] == 15050
    assert corpo["vabtmtTitloCobr"] == 150
    assert corpo["pmultaAplicVcto"] == "2.00000"
    assert corpo["ptxJuroVcto"] == "1.00000"
    assert corpo["pdescBonifPgto01"] == "5.00000"
    assert corpo["dlimDescBonif1"] == "07.12.2026"
    assert corpo["ctpoPrzCobr"] == 1
    assert corpo["qdiaInicMulta"] == corpo["qdiaInicJuro"] == 1
    assert corpo["qtdDecurPrz"] == 30
    assert corpo["cnegocCobr"] == "285600000000022654"
    assert (
        corpo["nroCpfCnpjBenef"],
        corpo["filCpfCnpjBenef"],
        corpo["digCpfCnpjBenef"],
    ) == (91300766, 1, 47)
    assert corpo["cindcdCobrMisto"] == "S"
    assert corpo["fase"] == 1
    assert corpo["codUsuario"] == "APISERVIC"
    assert corpo["ialiasAdsaoCta"] == ""
    assert corpo["isacdrAvalsTitlo"] == "KMEE Ltda"
    assert registrado.id_banco == "90000000001"
    assert registrado.pix_copia_cola == EMV
    assert registrado.txid == TXID
    assert registrado.linha_digitavel == LINHA_DIGITOS
    assert registrado.codigo_barras == BARRAS_DIGITOS
    assert "chave-pix-do-contrato" not in repr(registrado.resposta_bruta)
    assert EMV not in repr(registrado.resposta_bruta)


async def test_hibrido_valor_divergente(hibrido, dormir):
    adapter = _resposta_fixa(
        {"ctitloCobrCdent": 5, "valMoeda10": 150, "wqrcdPdraoMercd": EMV},
        dormir,
        hibrido=True,
    )
    with pytest.raises(Validacao, match="valor divergente"):
        await adapter.registrar_titulo(fazer_titulo())


async def test_hibrido_consulta(hibrido, falso):
    registrado = await hibrido.registrar_titulo(fazer_titulo())
    consultado = await hibrido.consultar_titulo(registrado.id_banco)
    assert falso.chamadas[-1] == ("POST", CONSULTA_HIBRIDO)
    assert falso.ultimo_corpo == {
        "cpfCnpjUsuario": 91300766,
        "filialCnpjUsuario": 1,
        "controleCpfCnpjUsuario": 47,
        "idProduto": 9,
        "contaProduto": 28560022654,
        "nomePersonalizado": "",
        "nossoNumero": int(registrado.id_banco),
        "seqTitulo": 0,
        "status": 0,
    }
    assert consultado.status is StatusCanonico.REGISTRADO
    assert consultado.pix_copia_cola == EMV
    assert consultado.txid == TXID
    assert EMV not in repr(consultado.resposta_bruta)


async def test_hibrido_pago_por_pix_e_liquidado(hibrido, falso):
    """Manual: 13 ate 20 minutos depois do Pix, 51 no processamento noturno."""
    registrado = await hibrido.registrar_titulo(fazer_titulo())
    falso.titulos[registrado.id_banco].update(codStatus=13)
    consultado = await hibrido.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    falso.titulos[registrado.id_banco].update(
        codStatus=51, dtPagto=9122026, vlrPagto=150.0
    )
    consultado = await hibrido.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.00")


async def test_hibrido_prorrogacao_com_txid_no_cabecalho(hibrido, falso):
    registrado = await hibrido.registrar_titulo(fazer_titulo())
    await hibrido.enviar_instrucao(
        registrado.id_banco,
        _instrucao(TipoInstrucao.PRORROGACAO, nova_data_vencimento=date(2027, 1, 5)),
    )
    assert falso.chamadas[-2] == ("POST", ALTERACAO_HIBRIDO)
    alteracao = falso.corpos[-2]
    assert alteracao["codUsuario"] == "OPENAPI"
    assert alteracao["vnmnalTitloCobr"] == 15000
    assert alteracao["chave"] == {
        "cnpjCpf": 91300766,
        "filial": 1,
        "controle": "47",
        "idprod": 9,
        "ctaprod": 28560022654,
        "nossoNumero": registrado.id_banco,
    }
    assert alteracao["dadosTitulo"]["dataVencimento"] == 5012027
    assert alteracao["dadosTitulo"]["codAbatimento"] == 0
    assert falso.titulos[registrado.id_banco]["dataVencto"] == "05/01/2027"


async def test_hibrido_abatimento_mantem_vencimento_e_centavos(hibrido, falso):
    registrado = await hibrido.registrar_titulo(fazer_titulo())
    await hibrido.enviar_instrucao(
        registrado.id_banco, _instrucao(TipoInstrucao.ABATIMENTO, valor="5.00")
    )
    dados = falso.corpos[-2]["dadosTitulo"]
    assert dados["dataVencimento"] == 10122026
    assert dados["codAbatimento"] == 1
    assert dados["valorAbatimentoTitulo"] == 500
    await hibrido.enviar_instrucao(
        registrado.id_banco, _instrucao(TipoInstrucao.ABATIMENTO, valor="6.00")
    )
    assert falso.corpos[-2]["dadosTitulo"]["codAbatimento"] == 3


async def test_hibrido_alteracao_exige_txid(falso, dormir):
    # Instancia nova nao tem o txId do registro: vem do callback ou e erro.
    primeiro = _adapter(falso, dormir, hibrido=True)
    registrado = await primeiro.registrar_titulo(fazer_titulo())
    sem_txid = _adapter(falso, dormir, hibrido=True)
    with pytest.raises(Validacao, match="obter_txid"):
        await sem_txid.enviar_instrucao(
            registrado.id_banco, _instrucao(TipoInstrucao.ABATIMENTO, valor="5.00")
        )
    com_callback = _adapter(falso, dormir, hibrido=True, obter_txid=lambda _i: TXID)
    await com_callback.enviar_instrucao(
        registrado.id_banco, _instrucao(TipoInstrucao.ABATIMENTO, valor="5.00")
    )
    assert falso.chamadas[-2] == ("POST", ALTERACAO_HIBRIDO)


@pytest.mark.parametrize("tipo", [TipoInstrucao.PROTESTO, TipoInstrucao.NEGATIVACAO])
async def test_hibrido_sem_protesto_por_instrucao(hibrido, tipo):
    with pytest.raises(NaoSuportado):
        await hibrido.enviar_instrucao("1", _instrucao(tipo))


async def test_hibrido_baixa_usa_a_rota_comum(hibrido, falso):
    registrado = await hibrido.registrar_titulo(fazer_titulo())
    baixado = await hibrido.baixar_titulo(registrado.id_banco)
    assert falso.chamadas[-1] == ("POST", BAIXA)
    assert baixado.status is StatusCanonico.BAIXADO


# -- webhook -----------------------------------------------------------------


async def test_webhook_desligado_nao_e_suportado(adapter):
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook(b"{}")
    with pytest.raises(NaoSuportado):
        await adapter.cadastrar_webhook("https://exemplo.com.br/w")


def test_webhook_so_dispara_consulta(falso, dormir):
    adapter = _adapter(falso, dormir, webhook=True)
    corpo = json.dumps(webhook_pagamento()).encode()
    [evento] = adapter.parse_webhook(corpo)
    assert evento.referencia == "123456"
    assert evento.tipo is StatusCanonico.LIQUIDADO
    assert evento.valor_pago is None
    assert evento.ocorrido_em == datetime(2026, 12, 9, 0, 15, 8)
    assert evento.chave_dedup == ("bradesco", "123456", StatusCanonico.LIQUIDADO)


def test_webhook_aceita_lista_e_ignora_outros_eventos(falso, dormir):
    adapter = _adapter(falso, dormir, webhook=True)
    eventos = adapter.parse_webhook(
        [webhook_pagamento(), webhook_pagamento(tipo_evento="X")]
    )
    assert len(eventos) == 1


@pytest.mark.parametrize(
    "aviso",
    [
        webhook_pagamento(produto="26"),
        webhook_pagamento(negociacao="99999999999"),
        webhook_pagamento(nosso_numero=""),
    ],
)
def test_webhook_de_outro_contrato_e_recusado(falso, dormir, aviso):
    adapter = _adapter(falso, dormir, webhook=True)
    with pytest.raises(Validacao):
        adapter.parse_webhook(aviso)


@pytest.mark.parametrize("corpo", [b"nao-json", b"[1]"])
def test_webhook_ilegivel(falso, dormir, corpo):
    adapter = _adapter(falso, dormir, webhook=True)
    with pytest.raises(Validacao):
        adapter.parse_webhook(corpo)


def test_webhook_data_ilegivel_nao_derruba(falso, dormir):
    adapter = _adapter(falso, dormir, webhook=True)
    aviso = {**webhook_pagamento(), "dataPagamento": ""}
    assert adapter.parse_webhook(aviso)[0].ocorrido_em is None


@pytest.mark.parametrize(
    ("ambiente", "caminho"),
    [("sandbox", WEBHOOK_SANDBOX), ("producao", WEBHOOK_PRODUCAO)],
)
async def test_cadastro_de_webhook(falso, dormir, ambiente, caminho):
    adapter = _adapter(falso, dormir, webhook=True, ambiente=ambiente)
    dados = await adapter.cadastrar_webhook("https://exemplo.com.br/w")
    assert falso.chamadas[-1] == ("POST", caminho)
    assert falso.ultimo_corpo == {
        "documento": {"cpfCnpj": "91300766", "filial": "0001", "controle": "47"},
        "versaoLayout": "1",
        "tipoCadastro": "I",
        "utilizaWebhook": "S",
        "urlEnvio": "https://exemplo.com.br/w",
        "tipoAviso": "1",
    }
    assert dados["utilizaWebhook"] == "S"


async def test_consulta_de_webhook_dispensa_url(falso, dormir):
    adapter = _adapter(falso, dormir, webhook=True)
    await adapter.cadastrar_webhook(operacao="C")
    assert falso.ultimo_corpo["tipoCadastro"] == "C"


@pytest.mark.parametrize(
    ("url", "operacao"),
    [
        ("http://exemplo.com.br", "I"),
        ("https://exemplo.com.br/" + "x" * 100, "A"),
        ("https://exemplo.com.br", "Z"),
    ],
)
async def test_cadastro_de_webhook_invalido(falso, dormir, url, operacao):
    adapter = _adapter(falso, dormir, webhook=True)
    with pytest.raises(Validacao):
        await adapter.cadastrar_webhook(url, operacao=operacao)
    assert falso.chamadas == []


# -- config ------------------------------------------------------------------


def test_padroes_por_produto():
    assert _config().valor_em_centavos is False
    assert _config().dias_encargos == 2
    assert _config(hibrido=True).valor_em_centavos is True
    assert _config(hibrido=True).dias_encargos == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"ambiente": "homologacao"},
        {"agencia": "12345"},
        {"conta_corrente": "12345678"},
        {"conta_corrente": "12-3"},
        {"carteira": 0},
        {"carteira": 100},
        {"especie": "XYZ"},
        {"dias_encargos": 0},
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
