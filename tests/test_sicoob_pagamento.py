from datetime import date, timedelta
from decimal import Decimal

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
    obter_classe_pagamento,
)
from brbank.adapters.sicoob_pagamento import (
    ISPB_SICOOB,
    SicoobPagamentoAdapter,
    SicoobPagamentoConfig,
)
from brbank.mascara import MASCARA
from tests.sicoob_pagamento_falso import SicoobPagamentoFalso, brcode

HOJE = date(2026, 10, 29)
DEBITO = ContaBancaria(agencia="4027", conta="12345", digito="6")
CNPJ_PAGADOR = "91.300.766/0001-47"
URL_QR = "qrpix.example.com.br/qr/v2/cobv/9d36b84fc70b478fb95c12729b90ca25"


def config(**extra) -> SicoobPagamentoConfig:
    base = {
        "conta": "c1",
        "client_id": "cliente-xyz",
        "pagador_documento": CNPJ_PAGADOR,
        "pagador_nome": "KMEE Ltda",
        "ambiente": "sandbox",
        "token_sandbox": "token-fixo-xyz",
    }
    base.update(extra)
    return SicoobPagamentoConfig(**base)


@pytest.fixture
def falso():
    return SicoobPagamentoFalso()


def novo(falso, dormir, **extra):
    return SicoobPagamentoAdapter(
        config(**extra), client=falso.cliente(), dormir=dormir, hoje=lambda: HOJE
    )


@pytest.fixture
def adapter(falso, dormir):
    return novo(falso, dormir)


def qr(chave="q1", valor="10.00", texto=None, **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": TipoPagamento.PIX_QRCODE,
        "valor": Decimal(valor),
        "data": HOJE,
        "brcode": texto or brcode(valor),
    }
    return Pagamento(**{**campos, **extra})


def pix(chave, destino, **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": TipoPagamento.PIX,
        "valor": Decimal("1.99"),
        "data": HOJE,
        "pix": destino,
    }
    return Pagamento(**{**campos, **extra})


def lote(*pagamentos, **extra):
    return LotePagamento(
        chave_idempotencia="l1",
        tipo=pagamentos[0].tipo,
        conta_debito=DEBITO,
        pagamentos=pagamentos,
        **extra,
    )


# -- contrato e configuracao ---------------------------------------------------


def test_contrato_capacidades_e_registro():
    assert issubclass(SicoobPagamentoAdapter, AdapterPagamento)
    assert obter_classe_pagamento("sicoob") is SicoobPagamentoAdapter
    caps = SicoobPagamentoAdapter.capacidades_pagamento
    for cap in ("PIX", "PIX_QRCODE", "LOTE_LOCAL", "RESERVA_NUMERO", "WEBHOOK"):
        assert CapacidadePagamento[cap] in caps
    for cap in ("LIBERACAO", "CANCELAMENTO", "BOLETO", "TRANSFERENCIA"):
        assert CapacidadePagamento[cap] not in caps
    assert SicoobPagamentoAdapter.validado_sandbox is False


def test_repr_da_config_sem_dado_pessoal_nem_segredo():
    texto = repr(config())
    assert "91300766000147" not in texto
    assert "KMEE" not in texto
    assert "token-fixo-xyz" not in texto


def test_producao_sem_certificado_e_recusada():
    with pytest.raises(Validacao, match="mTLS"):
        SicoobPagamentoAdapter(config(ambiente="producao", token_sandbox=None))


@pytest.mark.parametrize(
    ("kw", "erro"),
    [
        ({"ambiente": "homologacao"}, "ambiente"),
        ({"ispb": "123"}, "ISPB"),
        ({"tipo_conta": "OUTRA"}, "tipo_conta"),
        ({"codigo_municipio": "53001"}, "IBGE"),
        ({"pagador_documento": "123"}, "CPF"),
    ],
)
def test_config_invalida(kw, erro):
    with pytest.raises(Validacao, match=erro):
        config(**kw)


async def test_reserva_local_e_sem_consulta_de_lote(adapter, falso):
    um = await adapter.reservar_numero_lote()
    dois = await adapter.reservar_numero_lote()
    assert um != dois and len(um) == 32
    with pytest.raises(NaoSuportado, match="LOTE_LOCAL"):
        await adapter.consultar_lote(um, TipoPagamento.PIX_QRCODE)
    assert falso.chamadas == []


# -- Pix copia e cola ------------------------------------------------------------


async def test_qrcode_estatico_monta_corpo_e_le_resposta(adapter, falso):
    registrado = await adapter.pagar_lote(lote(qr(), numero="lote-1"))

    corpo = falso.de_rota("pagamentos/qrcode")[0]
    assert corpo["qrcode"] == brcode("10.00")
    assert corpo["valor"] == "10.00"
    assert corpo["repeticao"] is False
    assert corpo["pagarComJurosMulta"] is False
    assert "dataAgendamento" not in corpo and "codMun" not in corpo
    assert corpo["origem"] == {
        "ispb": ISPB_SICOOB,
        "cpfCnpj": "91300766000147",
        "nome": "KMEE Ltda",
        "conta": "123456",
        "agencia": "4027",
        "tipo": "CORRENTE",
    }
    cabecalho = falso.cabecalhos[0]
    assert cabecalho["client_id"] == "cliente-xyz"
    assert cabecalho["authorization"] == "Bearer token-fixo-xyz"

    assert registrado.id_banco == "lote-1"
    assert registrado.status is StatusLote.EM_PROCESSAMENTO
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.EM_PROCESSAMENTO
    assert linha.id_banco.startswith("E02038232") and len(linha.id_banco) == 32
    assert linha.chave_idempotencia == "q1"
    assert linha.valor == Decimal("10.00")
    assert linha.data == HOJE
    assert linha.resposta_bruta["origem"]["cpfCnpj"] == MASCARA
    assert linha.resposta_bruta["origem"]["nome"] == MASCARA
    assert linha.resposta_bruta["destino"]["cpfCnpj"] == MASCARA


async def test_qrcode_dinamico_com_juros_e_municipio(falso, dormir):
    adapter = novo(falso, dormir, codigo_municipio="5300108", ispb="12345678")
    falso.estado = "FINALIZADO"
    pagamento = qr(
        valor="135.00",
        texto=brcode(url=URL_QR),
        pagar_com_juros_multa=True,
        vencimento=date(2026, 10, 20),
    )
    registrado = await adapter.pagar_lote(
        LotePagamento(
            chave_idempotencia="l1",
            tipo=TipoPagamento.PIX_QRCODE,
            conta_debito=DEBITO.model_copy(update={"ispb": "87654321"}),
            pagamentos=(pagamento,),
        )
    )
    corpo = falso.de_rota("pagamentos/qrcode")[0]
    assert corpo["pagarComJurosMulta"] is True
    assert corpo["codMun"] == "5300108"
    assert corpo["dataVencimento"] == "2026-10-20"
    # ISPB da conta de debito vence o da config.
    assert corpo["origem"]["ispb"] == "87654321"
    assert registrado.status is StatusLote.PROCESSADO
    assert registrado.pagamentos[0].status is StatusPagamento.PAGO
    assert len(registrado.id_banco) == 32  # numero local gerado


async def test_qrcode_agendado_e_data_passada(adapter, falso):
    futuro = HOJE + timedelta(days=5)
    registrado = await adapter.pagar_lote(
        lote(qr("q1", data=futuro), qr("q2", data=HOJE - timedelta(days=1)))
    )
    corpos = falso.de_rota("pagamentos/qrcode")
    assert len(corpos) == 1
    assert corpos[0]["dataAgendamento"] == futuro.isoformat()
    agendado, passado = registrado.pagamentos
    assert agendado.status is StatusPagamento.AGENDADO
    assert agendado.data == futuro
    assert passado.status is StatusPagamento.RECUSADO
    assert "passado" in passado.motivos[0]
    assert registrado.status is StatusLote.ACEITO_PARCIAL


async def test_qrcode_recusado_nao_para_as_outras_linhas(adapter, falso):
    falso.falhas["pagamentos/qrcode"] = [400]
    registrado = await adapter.pagar_lote(lote(qr("q1"), qr("q2")))
    recusado, pago = registrado.pagamentos
    assert recusado.status is StatusPagamento.RECUSADO
    assert recusado.id_banco == ""
    assert pago.status is StatusPagamento.EM_PROCESSAMENTO
    assert registrado.status is StatusLote.ACEITO_PARCIAL


async def test_motivos_vem_do_problema(adapter, falso):
    falso.estado = "EM_PROCESSAMENTO"
    pagamento = qr()
    # Origem incompleta forca o 400 do servidor (formato Problema).
    adapter._config.pagador_nome = ""
    registrado = await adapter.pagar_lote(lote(pagamento))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.RECUSADO
    assert linha.motivos[0] == "RequisicaoInvalida"
    assert "origem" in linha.motivos[1]
    assert registrado.status is StatusLote.RECUSADO


@pytest.mark.parametrize("falha", ["timeout", 503])
async def test_qrcode_sem_resposta_e_indeterminado_sem_retry(adapter, falso, falha):
    falso.falhas["pagamentos/qrcode"] = [falha]
    registrado = await adapter.pagar_lote(lote(qr("q1"), qr("q2"), qr("q3")))
    # Uma chamada so: repetir um pagamento que executa na hora pode pagar duas vezes.
    assert len([c for c in falso.chamadas if c[1].endswith("qrcode")]) == 1
    incerto, segundo, terceiro = registrado.pagamentos
    assert incerto.status is StatusPagamento.INDETERMINADO
    assert incerto.motivos == ("SEM_RESPOSTA",)
    assert segundo.status is terceiro.status is StatusPagamento.RECUSADO
    assert segundo.motivos == ("NAO_ENVIADO",)
    assert registrado.status is StatusLote.ACEITO_PARCIAL


async def test_tipo_nao_declarado_e_recusado(adapter):
    boleto = Pagamento(
        chave_idempotencia="b1",
        tipo=TipoPagamento.BOLETO,
        valor=Decimal("15"),
        data=HOJE,
        codigo_barras="00195313800000015000000003051284190526000117",
        beneficiario_documento="95752207000129",
    )
    with pytest.raises(NaoSuportado, match="BOLETO"):
        await adapter.pagar_lote(lote(boleto))


async def test_agencia_longa_e_recusada(adapter):
    conta = ContaBancaria(agencia="40271", conta="1", digito="0")
    with pytest.raises(Validacao, match="4 digitos"):
        await adapter.pagar_lote(
            LotePagamento(
                chave_idempotencia="l1",
                tipo=TipoPagamento.PIX_QRCODE,
                conta_debito=conta,
                pagamentos=(qr(),),
            )
        )


async def test_conta_sem_digito_quando_configurado(falso, dormir):
    adapter = novo(falso, dormir, conta_com_digito=False, tipo_conta="SALARIO")
    await adapter.pagar_lote(lote(qr()))
    origem = falso.de_rota("pagamentos/qrcode")[0]["origem"]
    assert origem["conta"] == "12345"
    assert origem["tipo"] == "SALARIO"


# -- Pix por chave e por dados bancarios -----------------------------------------


async def test_pix_por_chave_inicia_e_confirma(adapter, falso):
    destino = DestinoPix(tipo_chave=TipoChavePix.TELEFONE, chave="(61) 99999-9999")
    registrado = await adapter.pagar_lote(
        lote(pix("p1", destino, descricao="Fornecedor"))
    )
    assert falso.de_rota("pagamentos") == [{"chave": "+5561999999999"}]
    confirmacao = falso.de_rota("pagamentos/confirmacao")[0]
    e2e = next(iter(falso.iniciados))
    assert confirmacao["endToEndId"] == e2e
    assert confirmacao["valor"] == "1,99"
    assert confirmacao["meioIniciacao"] == "CHAVE"
    assert confirmacao["descricao"] == "Fornecedor"
    assert confirmacao["repeticao"] is False
    assert confirmacao["origem"]["cpfCnpj"] == "91300766000147"
    (linha,) = registrado.pagamentos
    assert linha.id_banco == e2e
    assert linha.valor == Decimal("1.99")
    assert linha.status is StatusPagamento.EM_PROCESSAMENTO


@pytest.mark.parametrize(
    ("tipo", "chave", "enviada"),
    [
        (TipoChavePix.EMAIL, "fulano@example.com", "fulano@example.com"),
        (TipoChavePix.DOCUMENTO, "281.338.470-44", "28133847044"),
        (TipoChavePix.TELEFONE, "+55 61 99999-9999", "+5561999999999"),
        (
            TipoChavePix.ALEATORIA,
            "123E4567-E12B-12D1-A456-426655440000",
            "123e4567-e12b-12d1-a456-426655440000",
        ),
    ],
)
async def test_formato_da_chave(adapter, falso, tipo, chave, enviada):
    await adapter.pagar_lote(lote(pix("p1", DestinoPix(tipo_chave=tipo, chave=chave))))
    assert falso.de_rota("pagamentos")[0]["chave"] == enviada


async def test_chave_de_outro_dono_nao_confirma(adapter, falso):
    destino = DestinoPix(
        tipo_chave=TipoChavePix.EMAIL,
        chave="fulano@example.com",
        documento="91300766000147",
    )
    registrado = await adapter.pagar_lote(lote(pix("p1", destino)))
    assert falso.de_rota("pagamentos/confirmacao") == []
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.RECUSADO
    assert linha.motivos == ("DOCUMENTO_DIVERGENTE",)
    assert linha.id_banco  # e2e da iniciacao, para auditoria


async def test_chave_do_dono_certo_confirma(adapter, falso):
    destino = DestinoPix(
        tipo_chave=TipoChavePix.EMAIL,
        chave="fulano@example.com",
        documento="281.338.470-44",
    )
    registrado = await adapter.pagar_lote(lote(pix("p1", destino)))
    assert registrado.pagamentos[0].status is StatusPagamento.EM_PROCESSAMENTO


async def test_chave_inexistente_e_telefone_invalido(adapter, falso):
    falso.chaves_inexistentes.add("nao@ex.com")
    registrado = await adapter.pagar_lote(
        lote(
            pix("p1", DestinoPix(tipo_chave=TipoChavePix.EMAIL, chave="nao@ex.com")),
            pix("p2", DestinoPix(tipo_chave=TipoChavePix.TELEFONE, chave="999")),
        )
    )
    inexistente, telefone = registrado.pagamentos
    assert inexistente.status is StatusPagamento.RECUSADO
    assert inexistente.motivos[0] == "NaoEncontrado"
    assert telefone.status is StatusPagamento.RECUSADO
    assert "telefone" in telefone.motivos[0]
    assert registrado.status is StatusLote.RECUSADO


async def test_iniciacao_indisponivel_para_o_lote_sem_pagar(adapter, falso):
    falso.falhas["pagamentos"] = [503, 503, 503, 503]
    destino = DestinoPix(tipo_chave=TipoChavePix.EMAIL, chave="a@example.com")
    registrado = await adapter.pagar_lote(lote(pix("p1", destino), pix("p2", destino)))
    # Consultar o DICT nao move dinheiro: o retry padrao (4 tentativas) vale.
    rota = ("POST", "/sicoob/sandbox/pix-pagamentos/v2/pagamentos")
    assert falso.chamadas.count(rota) == 4
    primeiro, segundo = registrado.pagamentos
    assert primeiro.status is segundo.status is StatusPagamento.RECUSADO
    assert primeiro.motivos[0] == "NAO_ENVIADO"
    assert segundo.motivos == ("NAO_ENVIADO",)
    assert falso.de_rota("pagamentos/confirmacao") == []


async def test_confirmacao_sem_resposta_guarda_o_e2e(adapter, falso):
    falso.falhas["pagamentos/confirmacao"] = ["timeout"]
    destino = DestinoPix(tipo_chave=TipoChavePix.EMAIL, chave="a@example.com")
    registrado = await adapter.pagar_lote(lote(pix("p1", destino)))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.INDETERMINADO
    # Com o e2e o chamador resolve pela consulta, sem reenviar.
    assert linha.id_banco == next(iter(falso.iniciados))


async def test_pix_por_dados_bancarios(adapter, falso):
    conta = ContaBancaria(
        agencia="1", conta="1234567", digito="8", ispb="18236120", poupanca=True
    )
    destino = DestinoPix(conta=conta, documento="281.338.470-44", nome="Fulano")
    registrado = await adapter.pagar_lote(lote(pix("p1", destino)))
    assert falso.de_rota("pagamentos") == []
    corpo = falso.de_rota("pagamentos/confirmacao")[0]
    assert corpo["meioIniciacao"] == "MANUAL"
    assert "endToEndId" not in corpo
    assert corpo["destino"] == {
        "ispb": "18236120",
        "cpfCnpj": "28133847044",
        "conta": "12345678",
        "agencia": "1",
        "tipo": "POUPANCA",
        "nome": "Fulano",
    }
    assert registrado.pagamentos[0].status is StatusPagamento.EM_PROCESSAMENTO


@pytest.mark.parametrize(
    ("destino", "erro"),
    [
        (
            DestinoPix(
                conta=ContaBancaria(agencia="1", conta="1", digito="0", compe="237"),
                documento="28133847044",
            ),
            "ISPB",
        ),
        (
            DestinoPix(
                conta=ContaBancaria(agencia="1", conta="1", digito="0", ispb="60746948")
            ),
            "documento",
        ),
    ],
)
async def test_pix_por_dados_bancarios_incompleto(adapter, falso, destino, erro):
    registrado = await adapter.pagar_lote(lote(pix("p1", destino)))
    (linha,) = registrado.pagamentos
    assert linha.status is StatusPagamento.RECUSADO
    assert erro in linha.motivos[0]
    assert falso.chamadas == []


# -- consulta --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("estado", "status"),
    [
        ("EM_PROCESSAMENTO", StatusPagamento.EM_PROCESSAMENTO),
        ("FINALIZADO", StatusPagamento.PAGO),
        ("REJEITADO", StatusPagamento.REJEITADO),
        ("NAO_REALIZADO", StatusPagamento.REJEITADO),
    ],
)
async def test_consulta_traduz_estado(adapter, falso, estado, status):
    registrado = await adapter.pagar_lote(lote(qr()))
    e2e = registrado.pagamentos[0].id_banco
    falso.estado = estado
    consultado = await adapter.consultar_pagamento(e2e, TipoPagamento.PIX_QRCODE)
    assert consultado.id_banco == e2e
    assert consultado.status is status
    if status is StatusPagamento.REJEITADO:
        assert consultado.motivos == ("Saldo insuficiente",)
    else:
        assert consultado.motivos == ()


async def test_consulta_estado_desconhecido_e_inexistente(adapter, falso):
    registrado = await adapter.pagar_lote(lote(qr()))
    e2e = registrado.pagamentos[0].id_banco
    falso.estado = "BLOQUEADO"
    with pytest.raises(Validacao, match="nao mapeado"):
        await adapter.consultar_pagamento(e2e, TipoPagamento.PIX_QRCODE)
    with pytest.raises(Validacao, match="404"):
        await adapter.consultar_pagamento("E" + "0" * 31, TipoPagamento.PIX)
    with pytest.raises(Validacao, match="endToEndId"):
        await adapter.consultar_pagamento("", TipoPagamento.PIX)


async def test_producao_usa_oauth_com_escopos_do_swagger(falso, dormir):
    adapter = novo(falso, dormir, ambiente="producao", token_sandbox=None)
    await adapter.pagar_lote(lote(qr()))
    assert falso.pedido_token["client_id"] == "cliente-xyz"
    assert "client_secret" not in falso.pedido_token
    assert falso.pedido_token["scope"] == "pixpagamentos_escrita"
    assert falso.chamadas[-1][1] == "/pix-pagamentos/v2/pagamentos/qrcode"


# -- webhook ---------------------------------------------------------------------


async def test_webhook_configurar_consultar_remover(adapter, falso):
    assert await adapter.consultar_webhook_pagamento() is None
    await adapter.configurar_webhook_pagamento("https://erp.example.com/sicoob")
    assert await adapter.consultar_webhook_pagamento() == (
        "https://erp.example.com/sicoob"
    )
    await adapter.remover_webhook_pagamento()
    assert falso.webhook is None
    with pytest.raises(Validacao, match="https"):
        await adapter.configurar_webhook_pagamento("http://erp.example.com")


@pytest.mark.parametrize(
    "corpo",
    [
        {
            "payload": {
                "id": "E12345678202009091221kkkkkkkkkkk",
                "estado": "FINALIZADO_SUCESSO",
                "valor": "110.00",
            }
        },
        # Exemplo do swagger: `status` no lugar de `estado`, em lista.
        '{"payload": [{"id": "E12345678202009091221kkkkkkkkkkk",'
        ' "status": "FINALIZADO_SUCESSO", "valor": "110.00"}]}',
    ],
)
def test_webhook_sucesso(adapter, corpo):
    (evento,) = adapter.parse_webhook_pagamento(corpo)
    assert evento.id_banco == "E12345678202009091221kkkkkkkkkkk"
    assert evento.status is StatusPagamento.PAGO
    assert evento.valor == Decimal("110.00")


def test_webhook_rejeicao(adapter):
    (evento,) = adapter.parse_webhook_pagamento(
        b'{"payload": {"id": "E1", "status": "FINALIZADO_REJEICAO",'
        b' "valor": "110.00", "detalheRejeicao": "Rejeitado pelo PSP"}}'
    )
    assert evento.status is StatusPagamento.REJEITADO
    assert evento.motivos == ("Rejeitado pelo PSP",)


@pytest.mark.parametrize(
    ("corpo", "erro"),
    [
        ("nao e json", "JSON"),
        ({"payload": {"estado": "FINALIZADO_SUCESSO"}}, "sem id"),
        ({"payload": {"id": "E1", "estado": "OUTRO"}}, "nao mapeado"),
        ({"payload": "x"}, "formato"),
    ],
)
def test_webhook_invalido(adapter, corpo, erro):
    with pytest.raises(Validacao, match=erro):
        adapter.parse_webhook_pagamento(corpo)


async def test_resposta_ilegivel_de_pagamento_e_indeterminado(adapter, falso):
    # 200 com estado desconhecido: o banco pode ter pago, nunca vira RECUSADO.
    falso.estado = "BLOQUEADO"
    registrado = await adapter.pagar_lote(lote(qr("q1"), qr("q2")))
    incerto, seguinte = registrado.pagamentos
    assert incerto.status is StatusPagamento.INDETERMINADO
    assert incerto.id_banco in falso.pagamentos
    assert seguinte.motivos == ("NAO_ENVIADO",)
