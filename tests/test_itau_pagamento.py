from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from brbank import (
    AdapterPagamento,
    CapacidadePagamento,
    ContaBancaria,
    DestinoPix,
    LotePagamento,
    NaoSuportado,
    Pagamento,
    StatusLote,
    StatusPagamento,
    TipoChavePix,
    TipoPagamento,
    Validacao,
)
from brbank.adapters.itau_pagamento import (
    URL_PRODUCAO,
    URL_SANDBOX,
    URL_TOKEN,
    URL_TOKEN_SANDBOX,
    ItauPagamentoAdapter,
    ItauPagamentoConfig,
)
from tests.itau_pagamento_falso import PREFIXO, ItauPagamentoFalso
from tests.sicoob_pagamento_falso import brcode

HOJE = date(2026, 10, 29)
CNPJ_PAGADOR = "95752207000129"
CPF_RECEBEDOR = "28133847044"
DEBITO = ContaBancaria(agencia="1", conta="12345", digito="6")
POST_INCLUSAO = ("POST", f"{PREFIXO}transferencias")


def config(**extra) -> ItauPagamentoConfig:
    base = {
        "conta": "c1",
        "client_id": "id",
        "client_secret": "segredo-xyz",
        "documento_pagador": CNPJ_PAGADOR,
        "ambiente": "sandbox",
    }
    base.update(extra)
    return ItauPagamentoConfig(**base)


@pytest.fixture
def falso():
    return ItauPagamentoFalso()


@pytest.fixture
def adapter(falso, dormir):
    return ItauPagamentoAdapter(
        config(),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: HOJE,
        correlacao=lambda: "corr-1",
    )


def pagamento(tipo, chave="x1", valor="10.00", **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": tipo,
        "valor": Decimal(valor),
        "data": HOJE,
    }
    return Pagamento(**{**campos, **extra})


def qr(chave="q1", **extra):
    return pagamento(
        TipoPagamento.PIX_QRCODE,
        chave,
        brcode=extra.pop("brcode", brcode("10.00")),
        **extra,
    )


def pix_chave(chave="p1", tipo=TipoChavePix.EMAIL, valor="fulano@example.com", **extra):
    destino = DestinoPix(tipo_chave=tipo, chave=valor)
    return pagamento(TipoPagamento.PIX, chave, pix=destino, **extra)


def adapter_com_resposta(falso, dormir, responder):
    """Adapter contra um servidor que responde o que o teste mandar (o token segue
    pelo falso)."""

    def tratar(request):
        if request.url.path.endswith("/jwt"):
            return falso.tratar(request)
        return responder(request)

    return ItauPagamentoAdapter(
        config(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(tratar)),
        dormir=dormir,
    )


def lote(*pagamentos):
    return LotePagamento(
        chave_idempotencia="l1",
        tipo=pagamentos[0].tipo,
        conta_debito=DEBITO,
        pagamentos=pagamentos,
    )


def test_contrato_e_capacidades():
    assert issubclass(ItauPagamentoAdapter, AdapterPagamento)
    caps = ItauPagamentoAdapter.capacidades_pagamento
    for cap in ("PIX", "PIX_QRCODE", "LOTE_LOCAL", "RESERVA_NUMERO"):
        assert CapacidadePagamento[cap] in caps
    for cap in ("BOLETO", "GUIA", "TRANSFERENCIA", "LIBERACAO"):
        assert CapacidadePagamento[cap] not in caps
    assert CapacidadePagamento.CANCELAMENTO not in caps
    assert CapacidadePagamento.WEBHOOK not in caps
    assert ItauPagamentoAdapter.validado_sandbox is False


def test_config_nao_expoe_segredo_nem_documento():
    texto = repr(config())
    assert "segredo-xyz" not in texto
    assert CNPJ_PAGADOR not in texto


def test_config_invalida():
    with pytest.raises(Validacao, match="ambiente"):
        config(ambiente="uat")
    with pytest.raises(Validacao, match="modulo_sispag"):
        config(modulo_sispag="CONTAS")
    with pytest.raises(Validacao, match="CPF ou CNPJ"):
        config(documento_pagador="123")


def test_producao_exige_certificado():
    with pytest.raises(Validacao, match="mTLS"):
        ItauPagamentoAdapter(config(ambiente="producao"))


def test_hosts_por_ambiente():
    producao, sandbox = config(ambiente="producao"), config()
    assert (producao.base, producao.endereco_token) == (URL_PRODUCAO, URL_TOKEN)
    assert (sandbox.base, sandbox.endereco_token) == (URL_SANDBOX, URL_TOKEN_SANDBOX)
    troca = config(url_base="https://a.example.com", url_token="https://b.example.com")
    assert (troca.base, troca.endereco_token) == (
        "https://a.example.com",
        "https://b.example.com",
    )


async def test_pix_copia_e_cola(adapter, falso):
    registrado = await adapter.pagar_lote(lote(qr(descricao="Fornecedor nº 1 ação")))
    assert falso.corpos[0] == {
        "valor_pagamento": "10.00",
        "data_pagamento": "2026-10-29",
        "referencia_empresa": "q1",
        "pagador": {
            "tipo_conta": "CC",
            "agencia": "0001",
            "conta": "00123456",
            "tipo_pessoa": "J",
            "documento": CNPJ_PAGADOR,
            "modulo_sispag": "Fornecedores",
        },
        "emv": brcode("10.00"),
        "informacoes_entre_usuarios": "Fornecedor n 1 acao",
    }
    cabecalho = falso.cabecalhos[0]
    assert cabecalho["x-itau-apikey"] == "id"
    assert cabecalho["x-itau-flowid"] == "BRBANK_PAGAMENTO"
    assert cabecalho["x-itau-correlationid"] == "corr-1"
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.ACEITO
    assert linha.id_banco in falso.pagamentos
    assert linha.valor == Decimal("10.00")
    assert linha.data == HOJE
    assert registrado.status is StatusLote.EM_PROCESSAMENTO


@pytest.mark.parametrize(
    ("tipo", "bruta", "enviada"),
    [
        (TipoChavePix.TELEFONE, "(61) 99999-9999", "+5561999999999"),
        (TipoChavePix.TELEFONE, "+55 61 99999-9999", "+5561999999999"),
        (TipoChavePix.DOCUMENTO, "281.338.470-44", CPF_RECEBEDOR),
        (TipoChavePix.DOCUMENTO, "12.ABC.345/01DE-35", "12ABC34501DE35"),
        (TipoChavePix.EMAIL, " fulano@example.com ", "fulano@example.com"),
        (
            TipoChavePix.ALEATORIA,
            "9D36B84F-C70B-478F-B95C-12729B90CA25",
            "9d36b84f-c70b-478f-b95c-12729b90ca25",
        ),
    ],
)
async def test_pix_por_chave(adapter, falso, tipo, bruta, enviada):
    registrado = await adapter.pagar_lote(lote(pix_chave(tipo=tipo, valor=bruta)))
    assert falso.corpos[0]["chave"] == enviada
    assert registrado.pagamentos[0].status is StatusPagamento.ACEITO


@pytest.mark.parametrize(
    ("tipo", "bruta"),
    [(TipoChavePix.TELEFONE, "123"), (TipoChavePix.DOCUMENTO, "123")],
)
async def test_chave_invalida_nao_chama_o_banco(adapter, falso, tipo, bruta):
    registrado = await adapter.pagar_lote(lote(pix_chave(tipo=tipo, valor=bruta)))
    assert registrado.pagamentos[0].status is StatusPagamento.RECUSADO
    assert falso.corpos == []


async def test_pix_por_dados_bancarios(adapter, falso):
    conta = ContaBancaria(
        agencia="1", conta="7654", digito="x", ispb="00416968", poupanca=True
    )
    destino = DestinoPix(conta=conta, documento=CPF_RECEBEDOR)
    await adapter.pagar_lote(lote(pagamento(TipoPagamento.PIX, "p1", pix=destino)))
    corpo = falso.corpos[0]
    assert {k: corpo[k] for k in corpo if k not in ("pagador",)} == {
        "valor_pagamento": "10.00",
        "data_pagamento": "2026-10-29",
        "referencia_empresa": "p1",
        "ispb": "00416968",
        "tipo_identificacao_conta": "PP",
        "agencia_recebedor": "0001",
        "conta_recebedor": "0007654X",
        "tipo_de_identificacao_do_recebedor": "F",
        "identificacao_recebedor": CPF_RECEBEDOR,
    }


async def test_conta_longa_nao_e_cortada(adapter, falso):
    conta = ContaBancaria(agencia="1", conta="1234567890", digito="1", ispb="00416968")
    destino = DestinoPix(conta=conta, documento=CNPJ_PAGADOR)
    await adapter.pagar_lote(lote(pagamento(TipoPagamento.PIX, "p1", pix=destino)))
    assert falso.corpos[0]["conta_recebedor"] == "12345678901"
    assert falso.corpos[0]["tipo_de_identificacao_do_recebedor"] == "J"


@pytest.mark.parametrize(
    ("destino", "motivo"),
    [
        (
            DestinoPix(
                conta=ContaBancaria(agencia="1", conta="1", digito="0", compe="001"),
                documento=CPF_RECEBEDOR,
            ),
            "ISPB",
        ),
        (
            DestinoPix(
                conta=ContaBancaria(agencia="1", conta="1", digito="0", ispb="00416968")
            ),
            "documento",
        ),
        (
            DestinoPix(
                conta=ContaBancaria(
                    agencia="12345", conta="1", digito="0", ispb="00416968"
                ),
                documento=CPF_RECEBEDOR,
            ),
            "agencia",
        ),
        (
            DestinoPix(
                conta=ContaBancaria(
                    agencia="1", conta="1", digito="0", ispb="00416968"
                ),
                documento="123",
            ),
            "CPF ou CNPJ",
        ),
    ],
)
async def test_dados_bancarios_invalidos(adapter, falso, destino, motivo):
    registrado = await adapter.pagar_lote(
        lote(pagamento(TipoPagamento.PIX, "p1", pix=destino))
    )
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.RECUSADO
    assert motivo in linha.motivos[0]
    assert falso.corpos == []


async def test_valor_vai_em_texto_com_duas_casas(adapter, falso):
    destino = DestinoPix(tipo_chave=TipoChavePix.EMAIL, chave="f@example.com")
    await adapter.pagar_lote(
        lote(pagamento(TipoPagamento.PIX, "p1", "1234567.8", pix=destino))
    )
    assert falso.corpos[0]["valor_pagamento"] == "1234567.80"


@pytest.mark.parametrize(
    ("chave", "esperada"),
    [
        ("NF-2026/10.1", "NF-2026/10.1"),
        ("a" * 20, "a" * 20),
    ],
)
def test_referencia_curta_vai_como_esta(adapter, chave, esperada):
    assert adapter.referencia_empresa(qr(chave)) == esperada


@pytest.mark.parametrize("chave", ["a" * 21, "tem espaco", "ação"])
def test_referencia_vira_resumo_estavel(adapter, chave):
    um = adapter.referencia_empresa(qr(chave))
    assert um == adapter.referencia_empresa(qr(chave))
    assert len(um) == 20
    assert um != chave
    assert um != adapter.referencia_empresa(qr(chave + "x"))


async def test_reenvio_leva_a_mesma_referencia(adapter, falso):
    await adapter.pagar_lote(lote(qr("a" * 64)))
    await adapter.pagar_lote(lote(qr("a" * 64)))
    primeira, segunda = (c["referencia_empresa"] for c in falso.corpos)
    assert primeira == segunda


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("Sucesso", StatusPagamento.ACEITO),
        ("Sucesso (pre-autorizado)", StatusPagamento.ACEITO),
        ("Agendado", StatusPagamento.AGENDADO),
    ],
)
async def test_inclusao_nunca_e_pago(adapter, falso, bruto, esperado):
    falso.status_inclusao = bruto
    registrado = await adapter.pagar_lote(lote(qr()))
    assert registrado.pagamentos[0].status is esperado


@pytest.mark.parametrize("bruto", ["Rejeitado", "Nao incluido", "Não incluído"])
async def test_inclusao_recusada_em_200(adapter, falso, bruto):
    falso.status_inclusao = bruto
    registrado = await adapter.pagar_lote(lote(qr("q1"), qr("q2")))
    assert [p.status for p in registrado.pagamentos] == [StatusPagamento.RECUSADO] * 2
    assert registrado.status is StatusLote.RECUSADO
    assert falso.chamadas.count(POST_INCLUSAO) == 2


async def test_recusa_422_com_motivo(adapter, falso):
    falso.falhas = ["422"]
    registrado = await adapter.pagar_lote(lote(qr("q1"), qr("q2")))
    recusado, aceito = registrado.pagamentos
    assert recusado.status is StatusPagamento.RECUSADO
    assert recusado.motivos == ("908",)
    assert recusado.id_banco == ""
    assert aceito.status is StatusPagamento.ACEITO
    assert registrado.status is StatusLote.ACEITO_PARCIAL


async def test_recusa_200_rejeitado_com_motivo(adapter, falso):
    falso.falhas = ["200-rejeitado"]
    registrado = await adapter.pagar_lote(lote(qr()))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.RECUSADO
    assert linha.motivos == ("908",)
    assert "recebedor" not in linha.resposta_bruta


async def test_erro_de_campo_vira_recusa(adapter, falso):
    falso.falhas = ["422-campo"]
    registrado = await adapter.pagar_lote(lote(qr()))
    assert registrado.pagamentos[0].motivos == (
        "Erro na validacao dos campos",
        "chave invalido",
    )


async def test_400_vira_recusa(adapter, falso):
    falso.falhas = [400]
    registrado = await adapter.pagar_lote(lote(qr()))
    assert registrado.pagamentos[0].status is StatusPagamento.RECUSADO
    assert registrado.pagamentos[0].motivos == ("falha",)


@pytest.mark.parametrize(
    "falha", ["timeout", 500, 503, 429, 409, "nao-json", "lista", "sem-codigo"]
)
async def test_sem_resposta_e_indeterminado_sem_retry(adapter, falso, falha):
    falso.falhas = [falha]
    registrado = await adapter.pagar_lote(lote(qr("q1"), qr("q2")))
    assert falso.chamadas.count(POST_INCLUSAO) == 1
    incerto, seguinte = registrado.pagamentos
    assert incerto.status is StatusPagamento.INDETERMINADO
    assert incerto.motivos == ("SEM_RESPOSTA",)
    assert seguinte.status is StatusPagamento.RECUSADO
    assert seguinte.motivos == ("NAO_ENVIADO",)
    assert registrado.status is StatusLote.ACEITO_PARCIAL


async def test_status_de_inclusao_desconhecido_e_indeterminado(adapter, falso):
    falso.status_inclusao = "Outro"
    registrado = await adapter.pagar_lote(lote(qr("q1"), qr("q2")))
    incerto, seguinte = registrado.pagamentos
    assert incerto.status is StatusPagamento.INDETERMINADO
    assert incerto.id_banco in falso.pagamentos
    assert seguinte.motivos == ("NAO_ENVIADO",)


async def test_recusa_nao_para_as_seguintes(adapter, falso):
    falso.falhas = [400]
    registrado = await adapter.pagar_lote(lote(qr("q1"), qr("q2"), qr("q3")))
    assert falso.chamadas.count(POST_INCLUSAO) == 3
    assert [p.status for p in registrado.pagamentos] == [
        StatusPagamento.RECUSADO,
        StatusPagamento.ACEITO,
        StatusPagamento.ACEITO,
    ]


async def test_data_passada_recusada_localmente(adapter, falso):
    registrado = await adapter.pagar_lote(lote(qr(data=HOJE - timedelta(days=1))))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.RECUSADO
    assert "passado" in linha.motivos[0]
    assert falso.corpos == []


async def test_data_futura_vai_pura(adapter, falso):
    amanha = HOJE + timedelta(days=1)
    await adapter.pagar_lote(lote(qr(data=amanha)))
    assert falso.corpos[0]["data_pagamento"] == amanha.isoformat()


async def test_encargos_pelo_banco_recusados(adapter, falso):
    registrado = await adapter.pagar_lote(lote(qr(pagar_com_juros_multa=True)))
    assert registrado.pagamentos[0].status is StatusPagamento.RECUSADO
    assert "encargos" in registrado.pagamentos[0].motivos[0]
    assert falso.corpos == []


async def test_url_de_inclusao_separada(falso, dormir):
    adapter = ItauPagamentoAdapter(
        config(url_inclusao=f"https://gateway.example.com{PREFIXO}"),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: HOJE,
    )
    registrado = await adapter.pagar_lote(lote(qr()))
    await adapter.consultar_pagamento(
        registrado.pagamentos[0].id_banco, TipoPagamento.PIX_QRCODE
    )
    assert falso.hosts == ["gateway.example.com", "sandbox.devportal.itau.com.br"]


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("Efetuado", StatusPagamento.PAGO),
        ("Efetuados", StatusPagamento.PAGO),
        ("Pendente de autorização", StatusPagamento.ACEITO),
        ("Pendente de efetivacao", StatusPagamento.EM_PROCESSAMENTO),
        ("Autorizado", StatusPagamento.EM_PROCESSAMENTO),
        ("Não Efetuado", StatusPagamento.REJEITADO),
        ("Rejeitado em arquivo", StatusPagamento.REJEITADO),
        ("Cancelado", StatusPagamento.CANCELADO),
        ("Estornado", StatusPagamento.DEVOLVIDO),
    ],
)
async def test_consulta_status(adapter, falso, bruto, esperado):
    registrado = await adapter.pagar_lote(lote(qr()))
    falso.status_consulta = bruto
    codigo = registrado.pagamentos[0].id_banco
    consultado = await adapter.consultar_pagamento(codigo, TipoPagamento.PIX_QRCODE)
    assert consultado.id_banco == codigo
    assert consultado.status is esperado
    assert consultado.valor == Decimal("10.00")
    assert consultado.data == HOJE


async def test_consulta_motivo_e_pii_fora_da_bruta(adapter, falso):
    registrado = await adapter.pagar_lote(lote(qr()))
    falso.status_consulta = "Não Efetuado"
    consultado = await adapter.consultar_pagamento(
        registrado.pagamentos[0].id_banco, TipoPagamento.PIX_QRCODE
    )
    assert consultado.motivos == ("Pagamento expirado",)
    bruta = str(consultado.resposta_bruta)
    assert CPF_RECEBEDOR not in bruta
    assert "FULANO" not in bruta
    assert "OPERADOR" not in bruta
    assert "00765432" not in bruta
    assert consultado.resposta_bruta["historico_pagamento"][0]["data"] == "2026-10-29"


async def test_resposta_bruta_da_inclusao_sem_pii(adapter, falso):
    registrado = await adapter.pagar_lote(lote(qr()))
    bruta = registrado.pagamentos[0].resposta_bruta
    assert bruta["numero_lote"] == 500001394
    assert "pagador" not in bruta
    assert "recebedor" not in bruta


@pytest.mark.parametrize("bruto", ["Pendente de alteração/exclusão", "Outro", None])
async def test_consulta_status_nao_mapeado(adapter, falso, bruto):
    registrado = await adapter.pagar_lote(lote(qr()))
    falso.status_consulta = bruto
    with pytest.raises(Validacao, match="nao mapeado"):
        await adapter.consultar_pagamento(
            registrado.pagamentos[0].id_banco, TipoPagamento.PIX_QRCODE
        )


async def test_consulta_valida_id_e_404(adapter):
    with pytest.raises(Validacao, match="exige o codigo"):
        await adapter.consultar_pagamento("", TipoPagamento.PIX)
    with pytest.raises(Validacao, match="404"):
        await adapter.consultar_pagamento("nao-existe", TipoPagamento.PIX)


async def test_consulta_escapa_o_id(falso, dormir):
    caminhos = []

    def responder(request):
        caminhos.append(request.url.raw_path)
        return httpx.Response(404, json={"mensagem": "x"})

    adapter = adapter_com_resposta(falso, dormir, responder)
    with pytest.raises(Validacao):
        await adapter.consultar_pagamento("a/b", TipoPagamento.PIX)
    assert caminhos[0].endswith(b"/pagamentos_sispag/a%2Fb")


@pytest.mark.parametrize(
    "corpo",
    [{"data": {}}, {"data": {"dados_pagamento": "x"}}, {}],
)
async def test_consulta_sem_detalhe(falso, dormir, corpo):
    adapter = adapter_com_resposta(
        falso, dormir, lambda _: httpx.Response(200, json=corpo)
    )
    with pytest.raises(Validacao, match="dados_pagamento"):
        await adapter.consultar_pagamento("x", TipoPagamento.PIX)


@pytest.mark.parametrize(
    ("resposta", "erro"),
    [("<html>", "JSON"), ("[]", "formato")],
)
async def test_consulta_ilegivel(falso, dormir, resposta, erro):
    adapter = adapter_com_resposta(
        falso, dormir, lambda _: httpx.Response(200, text=resposta)
    )
    with pytest.raises(Validacao, match=erro):
        await adapter.consultar_pagamento("x", TipoPagamento.PIX)


async def test_consultar_lote_e_liberar_nao_suportados(adapter):
    with pytest.raises(NaoSuportado, match="LOTE_LOCAL"):
        await adapter.consultar_lote("x", TipoPagamento.PIX)
    with pytest.raises(NaoSuportado, match="LIBERACAO"):
        await adapter.liberar_lote("x")
    with pytest.raises(NaoSuportado):
        await adapter.cancelar_pagamentos(DEBITO, ("x",))
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook_pagamento("{}")


@pytest.mark.parametrize(
    ("tipo", "extra"),
    [
        (
            TipoPagamento.BOLETO,
            {
                "codigo_barras": "00195313800000015000000003051284190526000117",
                "beneficiario_documento": CNPJ_PAGADOR,
            },
        ),
        (
            TipoPagamento.GUIA,
            {"codigo_barras": "83630000000641400052836100812355200812351310"},
        ),
        (
            TipoPagamento.TRANSFERENCIA,
            {
                "conta_credito": ContaBancaria(
                    agencia="1", conta="1", digito="0", compe="001"
                ),
                "favorecido_documento": CPF_RECEBEDOR,
            },
        ),
    ],
)
async def test_tipos_sem_inclusao_na_api(adapter, falso, tipo, extra):
    with pytest.raises(NaoSuportado, match=tipo.value):
        await adapter.pagar_lote(lote(pagamento(tipo, "t1", **extra)))
    assert falso.corpos == []


async def test_lote_usa_numero_reservado(adapter):
    numero = await adapter.reservar_numero_lote()
    pedido = LotePagamento(
        chave_idempotencia="l1",
        tipo=TipoPagamento.PIX_QRCODE,
        conta_debito=DEBITO,
        pagamentos=(qr(),),
        numero=numero,
    )
    assert (await adapter.pagar_lote(pedido)).id_banco == numero
    assert (await adapter.pagar_lote(lote(qr("q9")))).id_banco != numero


async def test_localizar_por_referencia(adapter, falso):
    linha = qr("q1")
    registrado = await adapter.pagar_lote(lote(linha, qr("q2")))
    achados = await adapter.localizar_por_referencia(DEBITO, linha)
    assert [a.id_banco for a in achados] == [registrado.pagamentos[0].id_banco]
    assert achados[0].status is StatusPagamento.PAGO
    assert achados[0].valor == Decimal("10.00")
    assert "nome_favorecido" not in achados[0].resposta_bruta
    assert "cpf_cnpj" not in achados[0].resposta_bruta
    assert falso.consultas[0] == {
        "agencia_operacao": "0001",
        "conta_operacao": "00123456",
        "cnpj_empresa": CNPJ_PAGADOR,
        "referencia_empresa": "q1",
        "data_inicial": "2026-10-29",
        "data_final": "2026-10-29",
        "tipo_lista": "Detalhada",
        "status": "TD",
        "page": "0",
    }


async def test_localizar_vazio(adapter):
    assert await adapter.localizar_por_referencia(DEBITO, qr()) == ()


async def test_localizar_percorre_paginas(adapter, falso):
    falso.tamanho_pagina = 1
    linha = qr("q2")
    await adapter.pagar_lote(lote(qr("q1"), linha, qr("q3")))
    achados = await adapter.localizar_por_referencia(DEBITO, linha)
    assert len(achados) == 1
    assert [c["page"] for c in falso.consultas] == ["0", "1", "2"]


async def test_localizar_le_paginacao_em_camel_case(falso, dormir):
    paginas = []

    def responder(request):
        pagina = int(request.url.params["page"])
        paginas.append(pagina)
        item = {
            "id_pagamento": str(pagina),
            "referencia_empresa": "q1",
            "status": "Efetuado",
        }
        return httpx.Response(
            200,
            json={"data": {"itens": [item]}, "pagination": {"totalPages": 2}},
        )

    adapter = adapter_com_resposta(falso, dormir, responder)
    achados = await adapter.localizar_por_referencia(DEBITO, qr("q1"))
    assert paginas == [0, 1]
    assert [a.id_banco for a in achados] == ["0", "1"]


async def test_localizar_formato_invalido(falso, dormir):
    quebrado = adapter_com_resposta(
        falso, dormir, lambda _: httpx.Response(200, json={"data": {}})
    )
    with pytest.raises(Validacao, match="itens"):
        await quebrado.localizar_por_referencia(DEBITO, qr())


async def test_localizar_ignora_outra_referencia_e_le_motivo(falso, dormir):
    """Se o servidor ignorar o filtro, outra referencia nao conta como achado."""
    itens = [
        {"id_pagamento": "a", "referencia_empresa": "outra", "status": "Efetuado"},
        {
            "id_pagamento": "b",
            "referencia_empresa": " q1 ",
            "status": "Não Efetuado",
            "motivo": "Pagamento expirado",
        },
    ]
    adapter = adapter_com_resposta(
        falso, dormir, lambda _: httpx.Response(200, json={"data": {"itens": itens}})
    )
    (achado,) = await adapter.localizar_por_referencia(DEBITO, qr("q1"))
    assert achado.id_banco == "b"
    assert achado.status is StatusPagamento.REJEITADO
    assert achado.motivos == ("Pagamento expirado",)


async def test_token_invalido_vira_recusa_sem_inclusao(falso, dormir):
    adapter = ItauPagamentoAdapter(
        config(client_secret=""),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: HOJE,
    )
    registrado = await adapter.pagar_lote(lote(qr()))
    assert registrado.pagamentos[0].status is StatusPagamento.RECUSADO
    assert POST_INCLUSAO not in falso.chamadas
