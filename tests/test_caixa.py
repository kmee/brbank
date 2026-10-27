import base64
import hashlib
import itertools
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
    Pessoa,
    StatusCanonico,
    TipoInstrucao,
    Validacao,
    obter_classe,
)
from brbank.adapters.caixa import (
    NS_CONSULTA,
    NS_MANUTENCAO,
    NS_SIBAR,
    CaixaAdapter,
    CaixaConfig,
    autenticacao,
)
from brbank.transporte import PoliticaRetry
from tests.caixa_falso import (
    CODIGO_BENEFICIARIO,
    DOCUMENTO_BENEFICIARIO,
    CaixaFalso,
    _filho,
    _texto,
)
from tests.conftest import CNPJ_VALIDO, endereco, fazer_titulo

NN_1 = "14000000000000001"


def _config(**extra) -> CaixaConfig:
    sequencia = itertools.count(1)
    base = {
        "conta": "c1",
        "codigo_beneficiario": CODIGO_BENEFICIARIO,
        "agencia": "1234",
        "documento_beneficiario": "91.300.766/0001-47",
        "tipo_especie": 2,
        "gerar_nosso_numero": lambda _titulo: next(sequencia),
    }
    return CaixaConfig(**{**base, **extra})


@pytest.fixture
def falso():
    return CaixaFalso()


def _adapter(falso, dormir, **extra) -> CaixaAdapter:
    return CaixaAdapter(
        _config(**extra),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: date(2026, 11, 1),
        agora=lambda: datetime(2026, 11, 1, 9, 30, 15),
    )


@pytest.fixture
def adapter(falso, dormir):
    return _adapter(falso, dormir)


def _titulo_xml(falso):
    return _filho(falso.ultimo_xml, "Body", "SERVICO_ENTRADA", "DADOS")


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("caixa") is CaixaAdapter
    assert CaixaAdapter.validado_sandbox is False
    assert Capacidade.HIBRIDO in CaixaAdapter.capacidades
    assert Capacidade.ALTERACAO in CaixaAdapter.capacidades
    assert Capacidade.BAIXA in CaixaAdapter.capacidades
    assert Capacidade.WEBHOOK not in CaixaAdapter.capacidades
    assert Capacidade.PDF not in CaixaAdapter.capacidades


def test_hash_de_autenticacao_inclusao_e_baixa():
    cadeia = "0862011" + NN_1 + "10122026" + "000000000015000" + DOCUMENTO_BENEFICIARIO
    esperado = base64.b64encode(hashlib.sha256(cadeia.encode()).digest()).decode()
    obtido = autenticacao(
        "862011", NN_1, DOCUMENTO_BENEFICIARIO, date(2026, 12, 10), Decimal("150")
    )
    assert obtido == esperado
    cadeia_baixa = "0862011" + NN_1 + "0" * 23 + DOCUMENTO_BENEFICIARIO
    assert autenticacao("862011", NN_1, DOCUMENTO_BENEFICIARIO) == (
        base64.b64encode(hashlib.sha256(cadeia_baixa.encode()).digest()).decode()
    )


async def test_envelope_soap_e_header(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.id_banco == NN_1
    caminho, acao, operacao = falso.chamadas[-1]
    assert caminho == "ManutencaoCobrancaBancaria/Boleto/Externo"
    assert acao == '"IncluiBoleto"'
    assert operacao == "INCLUI_BOLETO"
    assert falso.ultimo_cabecalho["content-type"] == "text/xml; charset=utf-8"
    corpo = falso.ultimo_corpo.decode()
    assert corpo.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert f'xmlns:manutencaocobrancabancaria="{NS_MANUTENCAO}"' in corpo
    assert f'xmlns:sib="{NS_SIBAR}"' in corpo
    entrada = _filho(falso.ultimo_xml, "Body", "SERVICO_ENTRADA")
    assert entrada.tag == f"{{{NS_MANUTENCAO}}}SERVICO_ENTRADA"
    assert _filho(entrada, "HEADER").tag == f"{{{NS_SIBAR}}}HEADER"
    header = {
        k: _texto(entrada, "HEADER", k)
        for k in ("VERSAO", "USUARIO_SERVICO", "SISTEMA_ORIGEM", "UNIDADE", "DATA_HORA")
    }
    assert header == {
        "VERSAO": "3.2",
        "USUARIO_SERVICO": "SGCBS02P",
        "SISTEMA_ORIGEM": "SIGCB",
        "UNIDADE": "1234",
        "DATA_HORA": "20261101093015",
    }
    # O falso confere o hash; aqui o valor exato tambem.
    assert _texto(entrada, "HEADER", "AUTENTICACAO") == autenticacao(
        CODIGO_BENEFICIARIO,
        NN_1,
        DOCUMENTO_BENEFICIARIO,
        date(2026, 12, 10),
        Decimal("150.00"),
    )


async def test_payload_de_inclusao(adapter, falso):
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
    registrado = await adapter.registrar_titulo(titulo)
    dados = _titulo_xml(falso)
    assert _texto(dados, "INCLUI_BOLETO", "CODIGO_BENEFICIARIO") == "0862011"
    t = _filho(dados, "INCLUI_BOLETO", "TITULO")
    assert [f.tag for f in t] == [
        "NOSSO_NUMERO",
        "TIPO",
        "NUMERO_DOCUMENTO",
        "DATA_VENCIMENTO",
        "VALOR",
        "TIPO_ESPECIE",
        "FLAG_ACEITE",
        "DATA_EMISSAO",
        "JUROS_MORA",
        "VALOR_ABATIMENTO",
        "POS_VENCIMENTO",
        "CODIGO_MOEDA",
        "PAGADOR",
        "SACADOR_AVALISTA",
        "MULTA",
        "DESCONTOS",
        "IDENTIFICACAO_EMPRESA",
        "FICHA_COMPENSACAO",
    ]
    assert _texto(t, "NOSSO_NUMERO") == NN_1
    assert _texto(t, "TIPO") == "HIBRIDO"
    assert _texto(t, "NUMERO_DOCUMENTO") == "FAT-chave-1"
    assert _texto(t, "VALOR") == "150.01"
    assert _texto(t, "TIPO_ESPECIE") == "02"
    assert _texto(t, "FLAG_ACEITE") == "N"
    assert _texto(t, "DATA_EMISSAO") == "2026-11-01"
    assert _texto(t, "JUROS_MORA", "TIPO") == "TAXA_MENSAL"
    assert _texto(t, "JUROS_MORA", "DATA") == "2026-12-11"
    assert _texto(t, "JUROS_MORA", "PERCENTUAL") == "1.0000"
    assert _texto(t, "VALOR_ABATIMENTO") == "1.50"
    assert _texto(t, "POS_VENCIMENTO", "ACAO") == "DEVOLVER"
    assert _texto(t, "POS_VENCIMENTO", "NUMERO_DIAS") == "30"
    assert _texto(t, "CODIGO_MOEDA") == "9"
    assert _texto(t, "PAGADOR", "CPF") == "52998224725"
    assert _texto(t, "PAGADOR", "NOME") == "Maria da Silva"
    assert _texto(t, "PAGADOR", "ENDERECO", "LOGRADOURO") == "Rua das Flores 10"
    assert _texto(t, "PAGADOR", "ENDERECO", "CEP") == "30110000"
    assert _texto(t, "SACADOR_AVALISTA", "CNPJ") == "91300766000147"
    assert _texto(t, "SACADOR_AVALISTA", "RAZAO_SOCIAL") == "KMEE Ltda"
    assert _texto(t, "MULTA", "DATA") == "2026-12-11"
    assert _texto(t, "MULTA", "PERCENTUAL") == "2.0000"
    assert _texto(t, "DESCONTOS", "DESCONTO", "DATA") == "2026-12-07"
    assert _texto(t, "DESCONTOS", "DESCONTO", "PERCENTUAL") == "5.0000"
    assert _texto(t, "DESCONTOS", "DESCONTO", "TIPO") == "PERCENTUAL_ATE_DATA"
    assert _texto(t, "IDENTIFICACAO_EMPRESA") == "FAT-chave-1"
    mensagens = _filho(t, "FICHA_COMPENSACAO", "MENSAGENS")
    assert [m.text for m in mensagens] == ["linha um", "linha dois"]
    assert registrado.linha_digitavel
    assert registrado.pix_copia_cola
    assert registrado.status is StatusCanonico.REGISTRADO


async def test_sem_encargos_juros_isento_e_sem_grupos_opcionais(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    t = _filho(_titulo_xml(falso), "INCLUI_BOLETO", "TITULO")
    assert _texto(t, "JUROS_MORA", "TIPO") == "ISENTO"
    assert _texto(t, "JUROS_MORA", "VALOR") == "0.00"
    assert _filho(t, "JUROS_MORA", "DATA") is None
    for ausente in ("MULTA", "DESCONTOS", "VALOR_ABATIMENTO", "FICHA_COMPENSACAO"):
        assert _filho(t, ausente) is None
    assert _texto(t, "POS_VENCIMENTO", "NUMERO_DIAS") == "0"


async def test_convencional_sem_hibrido(falso, dormir):
    adapter = _adapter(falso, dormir, hibrido=False)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    entrada = _filho(falso.ultimo_xml, "Body", "SERVICO_ENTRADA")
    assert _texto(entrada, "HEADER", "VERSAO") == "3.0"
    assert _filho(entrada, "DADOS", "INCLUI_BOLETO", "TITULO", "TIPO") is None
    assert registrado.pix_copia_cola is None


async def test_pagador_pj_truncado_e_escapado(adapter, falso):
    longa = "Comercio & Industria <Teste> de Pecas Automotivas e Mais Ltda"
    pagador = Pessoa(
        nome=longa,
        documento=CNPJ_VALIDO,
        endereco=endereco().model_copy(update={"cidade": "Campos dos Goytacazes"}),
    )
    titulo = fazer_titulo().model_copy(
        update={"pagador": pagador, "beneficiario": None}
    )
    await adapter.registrar_titulo(titulo)
    assert b"&amp;" in falso.ultimo_corpo and b"&lt;Teste&gt;" in falso.ultimo_corpo
    t = _filho(_titulo_xml(falso), "INCLUI_BOLETO", "TITULO")
    assert _texto(t, "PAGADOR", "CNPJ") == "91300766000147"
    assert _texto(t, "PAGADOR", "RAZAO_SOCIAL") == longa[:40]
    assert _texto(t, "PAGADOR", "ENDERECO", "CIDADE") == "Campos dos Goyt"
    assert _filho(t, "SACADOR_AVALISTA") is None


@pytest.mark.parametrize(
    ("update", "trecho"),
    [
        ({"mensagens": ("a", "b", "c")}, "2 mensagens"),
        ({"mensagens": ("x" * 41,)}, "2 mensagens"),
        ({"seu_numero": "123456789012"}, "NUMERO_DOCUMENTO"),
    ],
)
async def test_inclusao_recusada_antes_da_chamada(adapter, falso, update, trecho):
    with pytest.raises(Validacao, match=trecho):
        await adapter.registrar_titulo(fazer_titulo().model_copy(update=update))
    assert falso.chamadas == []


async def test_nosso_numero_padrao_e_validacoes(falso, dormir):
    adapter = _adapter(falso, dormir, gerar_nosso_numero=None)
    with pytest.raises(Validacao, match="nosso numero"):
        await adapter.registrar_titulo(fazer_titulo())
    numerico = fazer_titulo().model_copy(update={"seu_numero": "4242"})
    registrado = await adapter.registrar_titulo(numerico)
    assert registrado.id_banco == "14" + "4242".zfill(15)
    pronto = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: "14000000000000777")
    assert (await pronto.registrar_titulo(fazer_titulo("k2"))).id_banco == (
        "14000000000000777"
    )
    errado = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: "99000000000000777")
    with pytest.raises(Validacao, match="comecar com 14"):
        await errado.registrar_titulo(fazer_titulo("k3"))


async def test_registro_idempotente_no_processo(adapter, falso):
    primeiro = await adapter.registrar_titulo(fazer_titulo())
    segundo = await adapter.registrar_titulo(fazer_titulo())
    assert primeiro is segundo
    assert len(falso.chamadas) == 1


async def test_consulta_envelope_e_liquidacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    caminho, acao, operacao = falso.chamadas[-1]
    assert (caminho, operacao) == ("ConsultaCobrancaBancaria/Boleto", "CONSULTA_BOLETO")
    assert acao == (
        '"http://caixa.gov.br/sibar/consulta_cobranca_bancaria/boleto/NewOperation"'
    )
    entrada = _filho(falso.ultimo_xml, "Body", "SERVICO_ENTRADA")
    assert entrada.tag == f"{{{NS_CONSULTA}}}SERVICO_ENTRADA"
    assert _texto(entrada, "HEADER", "VERSAO") == "5.4"
    assert _texto(entrada, "DADOS", "CONSULTA_BOLETO", "NOSSO_NUMERO") == NN_1
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("150.00")
    assert consultado.data_pagamento == date(2026, 11, 10)
    assert consultado.seu_numero == "FAT-chave-1"


async def test_resposta_bruta_mascarada_e_sem_autenticacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    bruta = consultado.resposta_bruta
    pagador = bruta["CONSULTA_BOLETO"]["TITULO"]["PAGADOR"]
    assert pagador == {"CPF": "***", "NOME": "***"}
    assert "Maria" not in repr(bruta)
    assert "hash-devolvido" not in repr(bruta)
    assert "HEADER" not in bruta


@pytest.mark.parametrize(
    ("situacao", "esperado"),
    [
        ("Em Aberto", StatusCanonico.REGISTRADO),
        ("ENVIADO AO CARTORIO", StatusCanonico.REGISTRADO),
        ("LIQUIDADO NO CARTÓRIO", StatusCanonico.LIQUIDADO),
        ("BAIXA  POR DEVOLUCAO", StatusCanonico.BAIXADO),
        ("BAIXA POR PROTESTO", StatusCanonico.BAIXADO),
    ],
)
async def test_tabela_de_situacoes(adapter, falso, situacao, esperado):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[registrado.id_banco]["SITUACAO"] = situacao
    assert (await adapter.consultar_titulo(registrado.id_banco)).status is esperado


@pytest.mark.parametrize("situacao", ["BAIXA POR ESTORNO", "VENCIDO", "PAGO"])
async def test_situacao_desconhecida_vira_validacao(adapter, falso, situacao):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[registrado.id_banco]["SITUACAO"] = situacao
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


async def test_situacao_lida_do_texto_de_retorno(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    falso.situacao_no_titulo = False
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert "SITUACAO" not in consultado.resposta_bruta["CONSULTA_BOLETO"]["TITULO"]
    assert consultado.status is StatusCanonico.LIQUIDADO


async def test_erro_de_negocio_vira_validacao_mascarada(adapter, falso):
    with pytest.raises(Validacao, match="TITULO NAO ENCONTRADO") as erro:
        await adapter.consultar_titulo("14000000000009999")
    assert erro.value.status_http == 200
    assert not erro.value.retryable


async def test_cod_retorno_do_barramento(adapter, falso):
    falso.cod_retorno_barramento = "X5"
    with pytest.raises(Validacao, match="barramento recusou"):
        await adapter.registrar_titulo(fazer_titulo())


async def test_id_banco_invalido_nao_chama(adapter, falso):
    for invalido in ("123", "1400000000000000A", "nao-existe"):
        with pytest.raises(Validacao, match="17 digitos"):
            await adapter.consultar_titulo(invalido)
    assert falso.chamadas == []


async def test_soap_fault_http_500_e_indisponivel(falso, dormir):
    adapter = CaixaAdapter(
        _config(),
        client=falso.cliente(),
        dormir=dormir,
        retry=PoliticaRetry(tentativas=2),
    )
    falso.resposta_crua = httpx.Response(
        500,
        content=b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        b"<s:Body><s:Fault><faultcode>s:Server</faultcode>"
        b"<faultstring>erro interno</faultstring></s:Fault></s:Body></s:Envelope>",
    )
    with pytest.raises(Indisponivel):
        await adapter.registrar_titulo(fazer_titulo())
    assert len(falso.chamadas) == 2


async def test_soap_fault_http_200_e_validacao(adapter, falso):
    falso.resposta_crua = httpx.Response(
        200,
        content=b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        b"<s:Body><s:Fault><faultcode>s:Client</faultcode>"
        b"<faultstring>CPF 529.982.247-25 invalido</faultstring></s:Fault>"
        b"</s:Body></s:Envelope>",
    )
    with pytest.raises(Validacao, match="SOAP Fault") as erro:
        await adapter.consultar_titulo(NN_1)
    assert "529" not in str(erro.value)


@pytest.mark.parametrize(
    "conteudo",
    [
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
        b"isto nao e xml",
        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        b"<s:Body/></s:Envelope>",
    ],
)
async def test_resposta_malformada_ou_com_dtd(adapter, falso, conteudo):
    falso.resposta_crua = httpx.Response(200, content=conteudo)
    with pytest.raises(Validacao):
        await adapter.consultar_titulo(NN_1)


async def test_instrucao_prorrogacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    nova = date(2027, 1, 15)
    resultado = await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(
            chave_idempotencia="i1",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=nova,
        ),
    )
    operacoes = [c[2] for c in falso.chamadas]
    assert operacoes == [
        "INCLUI_BOLETO",
        "CONSULTA_BOLETO",
        "ALTERA_BOLETO",
        "CONSULTA_BOLETO",
    ]
    assert falso.chamadas[2][1] == '"AlteraBoleto"'
    assert falso.boletos[registrado.id_banco]["DATA_VENCIMENTO"] == "2027-01-15"
    assert resultado.status is StatusCanonico.REGISTRADO


async def test_instrucao_abatimento_e_baixa(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    id_banco = registrado.id_banco
    await adapter.enviar_instrucao(
        id_banco,
        Instrucao(chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor="2.5"),
    )
    assert falso.boletos[id_banco]["VALOR_ABATIMENTO"] == "2.50"
    baixado = await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="b", tipo=TipoInstrucao.BAIXA)
    )
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.chamadas[-1][1] == '"BaixaBoleto"'


async def test_instrucoes_invalidas(adapter, falso):
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            NN_1, Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.PRORROGACAO)
        )
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            NN_1, Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.ABATIMENTO)
        )
    for tipo in (TipoInstrucao.PROTESTO, TipoInstrucao.NEGATIVACAO):
        with pytest.raises(NaoSuportado):
            await adapter.enviar_instrucao(
                NN_1, Instrucao(chave_idempotencia="x", tipo=tipo)
            )
    assert falso.chamadas == []


async def test_webhook_e_pdf_nao_suportados(adapter):
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook(b"{}")
    with pytest.raises(NaoSuportado):
        await adapter.obter_pdf(NN_1)


@pytest.mark.parametrize(
    "extra",
    [
        {"codigo_beneficiario": "12345678"},
        {"agencia": "12a"},
        {"documento_beneficiario": "123"},
        {"tipo_especie": 100},
        {"dias_devolucao_padrao": 1000},
        {"usuario_servico": "LONGO-DEMAIS"},
    ],
)
def test_config_invalida(extra):
    with pytest.raises(Validacao):
        _config(**extra)


def test_repr_da_config_nao_vaza_documento():
    texto = repr(_config())
    assert "91300766000147" not in texto
    assert "gerar_nosso_numero" not in texto
