from datetime import date
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
from brbank.adapters.bb import BancoDoBrasilAdapter, BBConfig
from tests.bb_falso import CHAVE_DEV, BBFalso
from tests.conftest import fazer_titulo


def config(**extra) -> BBConfig:
    base = {
        "conta": "c1",
        "client_id": "id",
        "client_secret": "segredo-xyz",
        "chave_desenvolvedor": CHAVE_DEV,
        "convenio": "1234567",
        "carteira": 17,
        "variacao_carteira": 35,
        "ambiente": "sandbox",
    }
    base.update(extra)
    return BBConfig(**base)


@pytest.fixture
def falso():
    return BBFalso()


@pytest.fixture
def adapter(falso, dormir):
    return BancoDoBrasilAdapter(
        config(),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: date(2026, 11, 1),
    )


def titulo_numerico(**kw):
    return fazer_titulo(**kw).model_copy(update={"seu_numero": "42"})


def test_entry_point_e_nao_validado():
    classe = obter_classe("bb")
    assert classe is BancoDoBrasilAdapter
    assert classe.validado_sandbox is False


def test_convenio_de_6_e_7_digitos():
    assert config(convenio="123456").convenio == "123456"
    with pytest.raises(Validacao):
        config(convenio="12345")
    with pytest.raises(Validacao):
        config(convenio="12345A7")
    with pytest.raises(Validacao):
        config(ambiente="homolog")


def test_repr_nao_vaza_segredos():
    texto = repr(config())
    assert "segredo-xyz" not in texto
    assert CHAVE_DEV not in texto


async def test_chave_dev_vai_na_query_nunca_em_header(adapter, falso):
    await adapter.registrar_titulo(titulo_numerico())
    assert "gw-dev-app-key=" + CHAVE_DEV in falso.urls[-1]
    assert CHAVE_DEV not in str(falso.ultimo_cabecalho)
    assert CHAVE_DEV not in repr(adapter._transporte.client)


async def test_payload_de_registro(adapter, falso):
    titulo = titulo_numerico(valor="150.005").model_copy(
        update={
            "encargos": Encargos(
                multa_percentual="2",
                juros_mensal_percentual="1",
                desconto_percentual="5",
                desconto_dias_antes=3,
                abatimento="1.50",
            ),
            "mensagens": ("pagar em dia",),
        }
    )
    registrado = await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["numeroConvenio"] == 1234567
    assert corpo["numeroCarteira"] == 17
    assert corpo["numeroVariacaoCarteira"] == 35
    assert corpo["valorOriginal"] == 150.01
    assert corpo["valorAbatimento"] == 1.5
    assert corpo["dataEmissao"] == "01.11.2026"
    assert corpo["dataVencimento"] == "10.12.2026"
    assert corpo["numeroTituloCliente"] == "00012345670000000042"
    assert registrado.id_banco == "00012345670000000042"
    assert corpo["numeroTituloBeneficiario"] == "42"
    assert corpo["codigoTipoTitulo"] == 2
    assert corpo["indicadorPix"] == "N"
    assert corpo["mensagemBloquetoOcorrencia"] == "PAGAR EM DIA"
    assert corpo["desconto"] == {
        "tipo": 2,
        "dataExpiracao": "07.12.2026",
        "porcentagem": 5.0,
    }
    assert corpo["jurosMora"] == {"tipo": 2, "porcentagem": 1.0}
    assert corpo["multa"] == {"tipo": 2, "data": "11.12.2026", "porcentagem": 2.0}
    assert corpo["pagador"]["tipoInscricao"] == 1
    assert corpo["pagador"]["numeroInscricao"] == 52998224725
    assert corpo["pagador"]["cep"] == 30110000
    assert corpo["pagador"]["endereco"] == "Rua das Flores 10"
    assert corpo["beneficiarioFinal"]["tipoInscricao"] == 2


async def test_convenio_de_6_digitos_preenche_a_esquerda(falso, dormir):
    adapter = BancoDoBrasilAdapter(
        config(convenio="123456"), client=falso.cliente(), dormir=dormir
    )
    registrado = await adapter.registrar_titulo(titulo_numerico())
    assert registrado.id_banco == "00001234560000000042"


async def test_registro_traz_pix_e_linha(adapter):
    r = await adapter.registrar_titulo(titulo_numerico())
    assert r.status is StatusCanonico.REGISTRADO
    assert r.pix_copia_cola == "000201"
    assert r.txid == "tx1"
    assert r.codigo_barras
    assert "000201" not in str(r.resposta_bruta)


async def test_idempotencia_nao_registra_duas_vezes(adapter, falso):
    a = await adapter.registrar_titulo(titulo_numerico())
    b = await adapter.registrar_titulo(titulo_numerico())
    assert a == b
    assert falso.chamadas.count(("POST", "/cobrancas/v2/boletos")) == 1


async def test_sem_nosso_numero_numerico_nao_chama_a_rede(adapter, falso):
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(fazer_titulo())  # seu_numero "FAT-..."
    assert falso.chamadas == []


async def test_recusas_sem_rede(adapter, falso):
    base = titulo_numerico()
    longo = base.model_copy(update={"mensagens": ("x" * 31,)})
    baixa = base.model_copy(update={"dias_baixa_automatica": 5})
    for titulo in (longo, baixa):
        with pytest.raises(Validacao):
            await adapter.registrar_titulo(titulo)
    assert falso.chamadas == []


async def test_cnpj_alfanumerico_e_recusado(adapter):
    from brbank import Pessoa
    from tests.conftest import endereco

    # CNPJ alfanumerico valido (DV por ASCII-48): 12ABC34501DE35
    pessoa = Pessoa(nome="X", documento="12ABC34501DE35", endereco=endereco())
    titulo = titulo_numerico().model_copy(update={"pagador": pessoa})
    with pytest.raises(Validacao, match="numerico"):
        await adapter.registrar_titulo(titulo)


async def test_consulta_detalhada_e_data_de_pagamento(adapter, falso):
    r = await adapter.registrar_titulo(titulo_numerico())
    assert "numeroConvenio=1234567" in (await _consultar_url(adapter, falso, r))
    falso.liquidar(r.id_banco)
    c = await adapter.consultar_titulo(r.id_banco)
    assert c.status is StatusCanonico.LIQUIDADO
    assert c.valor_pago == Decimal("150.00")
    assert c.data_pagamento == date(2026, 11, 10)


async def _consultar_url(adapter, falso, r):
    await adapter.consultar_titulo(r.id_banco)
    return falso.urls[-1]


async def test_status_nao_mapeados_nao_liquidam_nem_baixam(adapter, falso):
    r = await adapter.registrar_titulo(titulo_numerico())
    for codigo in (10, 14, 16, 18, 999, None):
        falso.boletos[r.id_banco]["codigoEstadoTituloCobranca"] = codigo
        with pytest.raises(Validacao):
            await adapter.consultar_titulo(r.id_banco)
    falso.boletos[r.id_banco]["codigoEstadoTituloCobranca"] = 80
    c = await adapter.consultar_titulo(r.id_banco)
    assert c.status is StatusCanonico.PENDENTE_REGISTRO


async def test_prorrogacao(adapter, falso):
    r = await adapter.registrar_titulo(titulo_numerico())
    await adapter.enviar_instrucao(
        r.id_banco,
        Instrucao(
            chave_idempotencia="i1",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=date(2027, 1, 10),
        ),
    )
    patch = [c for c in falso.chamadas if c[0] == "PATCH"]
    assert len(patch) == 1
    assert falso.ultimo_corpo == {
        "numeroConvenio": 1234567,
        "indicadorNovaDataVencimento": "S",
        "alteracaoData": {"novaDataVencimento": "10.01.2027"},
    }
    assert falso.boletos[r.id_banco]["dataVencimentoTituloCobranca"] == "10.01.2027"


async def test_abatimento_inclui_e_depois_altera(adapter, falso):
    r = await adapter.registrar_titulo(titulo_numerico())

    def pedir(valor):
        return Instrucao(
            chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor=valor
        )

    await adapter.enviar_instrucao(r.id_banco, pedir("5.00"))
    assert falso.ultimo_corpo["indicadorIncluirAbatimento"] == "S"
    assert falso.ultimo_corpo["abatimento"] == {"valorAbatimento": 5.0}
    await adapter.enviar_instrucao(r.id_banco, pedir("7.00"))
    assert falso.ultimo_corpo["indicadorAlterarAbatimento"] == "S"
    assert falso.ultimo_corpo["alteracaoAbatimento"] == {"novoValorAbatimento": 7.0}


async def test_instrucoes_invalidas_ou_nao_suportadas(adapter):
    assert adapter.suporta(Capacidade.ALTERACAO)
    for tipo in (TipoInstrucao.PROTESTO, TipoInstrucao.NEGATIVACAO):
        with pytest.raises(NaoSuportado):
            await adapter.enviar_instrucao(
                "x", Instrucao(chave_idempotencia="p", tipo=tipo)
            )
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "x", Instrucao(chave_idempotencia="p", tipo=TipoInstrucao.PRORROGACAO)
        )
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "x", Instrucao(chave_idempotencia="p", tipo=TipoInstrucao.ABATIMENTO)
        )


async def test_instrucao_baixa_delega_para_baixar(adapter, falso):
    r = await adapter.registrar_titulo(titulo_numerico())
    baixado = await adapter.enviar_instrucao(
        r.id_banco, Instrucao(chave_idempotencia="b", tipo=TipoInstrucao.BAIXA)
    )
    assert baixado.status is StatusCanonico.BAIXADO
    assert falso.ultimo_corpo == {"numeroConvenio": 1234567}


async def test_sem_pdf_e_sem_webhook(adapter):
    assert not adapter.suporta(Capacidade.PDF)
    assert not adapter.suporta(Capacidade.WEBHOOK)
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook(b"{}")
    with pytest.raises(NaoSuportado):
        await adapter.obter_pdf("x")


async def test_sandbox_manda_credenciais_no_corpo(adapter, falso):
    await adapter.registrar_titulo(titulo_numerico())
    assert falso.pedido_token["client_id"] == "id"
    assert "authorization" not in falso.cabecalho_token
    assert falso.pedido_token["scope"] == "cobrancas.boletos-requisicao"


async def test_producao_usa_basic_e_corpo_sem_segredo(falso, dormir):
    adapter = BancoDoBrasilAdapter(
        config(ambiente="producao"), client=falso.cliente(), dormir=dormir
    )
    # a producao aponta para outro host: o falso atende pelo caminho
    await adapter.registrar_titulo(titulo_numerico())
    assert falso.cabecalho_token["authorization"].startswith("Basic ")
    assert "client_secret" not in falso.pedido_token
    assert "client_id" not in falso.pedido_token


async def test_token_por_escopo_e_reaproveitado(adapter, falso):
    r = await adapter.registrar_titulo(titulo_numerico())
    await adapter.consultar_titulo(r.id_banco)
    await adapter.consultar_titulo(r.id_banco)
    assert falso.tokens_emitidos == 2


async def test_cnpj_parceiro_vai_no_header(falso, dormir):
    adapter = BancoDoBrasilAdapter(
        config(cnpj_parceiro="91300766000147"), client=falso.cliente(), dormir=dormir
    )
    await adapter.registrar_titulo(titulo_numerico())
    assert falso.ultimo_cabecalho["x-bb-portal-devx-cnpj-parceiro"] == "91300766000147"


async def test_indicador_pix_configuravel(falso, dormir):
    adapter = BancoDoBrasilAdapter(
        config(indicador_pix=True), client=falso.cliente(), dormir=dormir
    )
    await adapter.registrar_titulo(titulo_numerico())
    assert falso.ultimo_corpo["indicadorPix"] == "S"


async def test_5xx_e_retentado_ate_esgotar(falso, dormir, dormidas):
    def tratar(request):
        if request.url.path == "/oauth/token":
            return falso.tratar(request)
        return httpx.Response(503, text="fora do ar")

    client = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = BancoDoBrasilAdapter(config(), client=client, dormir=dormir)
    with pytest.raises(Indisponivel) as erro:
        await adapter.registrar_titulo(titulo_numerico())
    assert erro.value.retryable
    assert len(dormidas) == 3


async def test_resposta_de_erro_sai_mascarada(falso, dormir):
    def tratar(request):
        if request.url.path == "/oauth/token":
            return falso.tratar(request)
        return httpx.Response(
            400, json={"erros": [{"mensagem": "pagador 52998224725 invalido"}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = BancoDoBrasilAdapter(config(), client=client, dormir=dormir)
    with pytest.raises(Validacao) as erro:
        await adapter.registrar_titulo(titulo_numerico())
    assert "52998224725" not in str(erro.value.resposta_bruta)


async def test_resposta_de_registro_sem_numero(falso, dormir):
    def tratar(request):
        if request.url.path == "/oauth/token":
            return falso.tratar(request)
        return httpx.Response(201, json={"linhaDigitavel": "x"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    adapter = BancoDoBrasilAdapter(config(), client=client, dormir=dormir)
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(titulo_numerico())
