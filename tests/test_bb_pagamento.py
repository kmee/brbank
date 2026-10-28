from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

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
from brbank.adapters.bb_pagamento import (
    BancoDoBrasilPagamentoAdapter,
    BBPagamentoConfig,
)
from tests.bb_pagamento_falso import CHAVE_DEV, BBPagamentoFalso

DEBITO = ContaBancaria(agencia="1607", conta="99738672", digito="x")
DIA = date(2026, 10, 9)
BARRAS_BOLETO = "00195313800000015000000003051284190526000117"
BARRAS_GUIA = "83630000000641400052836100812355200812351310"
# CNPJs da massa de homologacao do BB (inexistentes na Receita).
CNPJ_BENEFICIARIO = "95752207000129"
CNPJ_PIX = "95127446000198"


def config(**extra) -> BBPagamentoConfig:
    base = {
        "conta": "c1",
        "client_id": "id",
        "client_secret": "segredo-xyz",
        "chave_desenvolvedor": CHAVE_DEV,
        "contrato": "731030",
        "ambiente": "sandbox",
    }
    base.update(extra)
    return BBPagamentoConfig(**base)


@pytest.fixture
def falso():
    return BBPagamentoFalso()


@pytest.fixture
def adapter(falso, dormir):
    return BancoDoBrasilPagamentoAdapter(
        config(), client=falso.cliente(), dormir=dormir
    )


def boleto(chave="b1", **extra):
    campos = {
        "chave_idempotencia": chave,
        "tipo": TipoPagamento.BOLETO,
        "valor": Decimal("15"),
        "data": DIA,
        "codigo_barras": BARRAS_BOLETO,
        "beneficiario_documento": "95.752.207/0001-29",
    }
    return Pagamento(**{**campos, **extra})


def pix(chave, destino):
    return Pagamento(
        chave_idempotencia=chave,
        tipo=TipoPagamento.PIX,
        valor=Decimal("1.5"),
        data=DIA,
        pix=destino,
    )


def lote(*pagamentos, **extra):
    return LotePagamento(
        chave_idempotencia="l1",
        tipo=pagamentos[0].tipo,
        conta_debito=DEBITO,
        pagamentos=pagamentos,
        **extra,
    )


def test_contrato_e_repr_sem_segredo():
    assert issubclass(BancoDoBrasilPagamentoAdapter, AdapterPagamento)
    assert (
        BancoDoBrasilPagamentoAdapter.capacidades_pagamento
        & CapacidadePagamento.LIBERACAO
    )
    texto = repr(config())
    assert "segredo-xyz" not in texto and CHAVE_DEV not in texto


def test_producao_sem_certificado_e_recusada():
    with pytest.raises(Validacao, match="mTLS"):
        BancoDoBrasilPagamentoAdapter(config(ambiente="producao"))


@pytest.mark.parametrize(
    ("kw", "erro"),
    [
        ({"beneficiario_documento": None}, "beneficiario"),
        ({"codigo_barras": BARRAS_GUIA}, "guia"),
    ],
)
def test_pagamento_de_boleto_invalido(kw, erro):
    with pytest.raises(ValidationError, match=erro):
        boleto(**kw)


def test_lote_exige_tipo_unico_e_chaves_unicas():
    guia = Pagamento(
        chave_idempotencia="g1",
        tipo=TipoPagamento.GUIA,
        valor=Decimal("1"),
        data=DIA,
        codigo_barras=BARRAS_GUIA,
    )
    with pytest.raises(ValidationError, match="um so tipo"):
        lote(boleto(), guia)
    with pytest.raises(ValidationError, match="repetida"):
        lote(boleto("x"), boleto("x"))


async def test_boleto_reserva_numero_e_monta_o_corpo(adapter, falso):
    registrado = await adapter.pagar_lote(
        lote(boleto(nosso_numero="00030512841905260001"))
    )

    assert ("GET", "/pagamentos-lote/v1/proximos-numeros-requisicao") in falso.chamadas
    corpo = falso.corpos[-1]
    assert corpo["numeroRequisicao"] == 26545
    assert corpo["codigoContrato"] == 731030
    assert corpo["digitoVerificadorContaCorrenteDebito"] == "X"
    linha = corpo["lancamentos"][0]
    assert linha["dataPagamento"] == 9102026
    assert linha["valorPagamento"] == 15.0
    assert linha["codigoTipoBeneficiario"] == 2
    assert linha["documentoBeneficiario"] == CNPJ_BENEFICIARIO
    assert linha["codigoSeuDocumento"] == "b1"
    assert "gw-dev-app-key" not in str(falso.ultimo_cabecalho).lower()

    assert registrado.id_banco == "26545"
    assert registrado.status is StatusLote.ACEITO
    (linha_registrada,) = registrado.pagamentos
    assert linha_registrada.chave_idempotencia == "b1"
    assert linha_registrada.status is StatusPagamento.ACEITO
    assert linha_registrada.data == DIA


async def test_numero_reservado_nao_e_pedido_de_novo(adapter, falso):
    registrado = await adapter.pagar_lote(lote(boleto(), numero="777"))
    assert registrado.id_banco == "777"
    assert (
        "GET",
        "/pagamentos-lote/v1/proximos-numeros-requisicao",
    ) not in falso.chamadas


async def test_linha_recusada_traz_os_motivos(adapter, falso):
    falso.recusar = {2}
    registrado = await adapter.pagar_lote(lote(boleto("a"), boleto("b")))
    assert registrado.status is StatusLote.ACEITO_PARCIAL
    assert [p.status for p in registrado.pagamentos] == [
        StatusPagamento.ACEITO,
        StatusPagamento.RECUSADO,
    ]
    assert registrado.pagamentos[1].motivos == ("7",)
    assert [p.chave_idempotencia for p in registrado.aceitos] == ["a"]


async def test_pix_por_cada_forma(adapter, falso):
    destinos = [
        DestinoPix(tipo_chave=TipoChavePix.EMAIL, chave="hmtestes2@bb.com.br"),
        DestinoPix(tipo_chave=TipoChavePix.TELEFONE, chave="+55 (11) 98573-2102"),
        DestinoPix(tipo_chave=TipoChavePix.DOCUMENTO, chave="28779295827"),
        DestinoPix(
            tipo_chave=TipoChavePix.ALEATORIA,
            chave="9e881f18-cc66-4fc7-8f2c-a795dbb2bfc1",
        ),
        DestinoPix(
            conta=ContaBancaria(agencia="551", conta="43814", digito="6", compe="001"),
            documento=CNPJ_PIX,
        ),
    ]
    await adapter.pagar_lote(lote(*(pix(f"p{i}", d) for i, d in enumerate(destinos))))

    corpo = falso.corpos[-1]
    assert corpo["numeroContrato"] == 731030
    assert corpo["tipoPagamento"] == 126
    email, fone, cpf, evp, conta = corpo["listaTransferencias"]
    assert (email["formaIdentificacao"], email["email"]) == (2, "hmtestes2@bb.com.br")
    assert (fone["dddTelefone"], fone["telefone"]) == (11, 985732102)
    assert (cpf["formaIdentificacao"], cpf["cpf"]) == (3, 28779295827)
    assert evp["formaIdentificacao"] == 4
    assert conta["formaIdentificacao"] == 5
    assert (conta["numeroCOMPE"], conta["cnpj"], conta["tipoConta"]) == (1, CNPJ_PIX, 1)


def test_destino_pix_exige_chave_ou_conta():
    with pytest.raises(ValidationError, match="um dos dois"):
        DestinoPix()
    with pytest.raises(ValidationError, match="tipo_chave"):
        DestinoPix(chave="x@y.com")


async def test_guia_le_o_id_da_consulta(adapter):
    guia = Pagamento(
        chave_idempotencia="g1",
        tipo=TipoPagamento.GUIA,
        valor=Decimal("64.14"),
        data=DIA,
        codigo_barras=BARRAS_GUIA,
    )
    registrado = await adapter.pagar_lote(lote(guia))
    consultado = await adapter.consultar_lote(registrado.id_banco, TipoPagamento.GUIA)
    assert consultado.pagamentos[0].id_banco == registrado.pagamentos[0].id_banco


async def test_transferencia_para_outro_banco_exige_finalidade(falso, dormir):
    ted = Pagamento(
        chave_idempotencia="t1",
        tipo=TipoPagamento.TRANSFERENCIA,
        valor=Decimal("4.44"),
        data=DIA,
        conta_credito=ContaBancaria(agencia="1", conta="2", digito="3", compe="341"),
        favorecido_documento="84526081000158",
    )
    sem = BancoDoBrasilPagamentoAdapter(config(), client=falso.cliente(), dormir=dormir)
    with pytest.raises(Validacao, match="finalidade_ted"):
        await sem.pagar_lote(lote(ted, numero="1"))

    com = BancoDoBrasilPagamentoAdapter(
        config(finalidade_ted=10), client=falso.cliente(), dormir=dormir
    )
    await com.pagar_lote(lote(ted, numero="2"))
    linha = falso.corpos[-1]["listaTransferencias"][0]
    assert linha["codigoFinalidadeTED"] == 10
    assert linha["cnpjBeneficiario"] == "84526081000158"
    assert linha["numeroCOMPE"] == 341


async def test_poupanca_nao_confirmada_e_recusada(adapter):
    destino = DestinoPix(
        conta=ContaBancaria(
            agencia="1", conta="2", digito="3", compe="001", poupanca=True
        ),
        documento=CNPJ_PIX,
    )
    with pytest.raises(NaoSuportado):
        await adapter.pagar_lote(lote(pix("p", destino), numero="1"))


async def test_liberar_e_consultar_pagamento(adapter, falso):
    registrado = await adapter.pagar_lote(lote(boleto()))
    await adapter.liberar_lote(registrado.id_banco)
    assert falso.corpos[-1] == {"numeroRequisicao": 26545, "indicadorFloat": "N"}

    consultado = await adapter.consultar_lote(registrado.id_banco, TipoPagamento.BOLETO)
    assert consultado.status is StatusLote.EM_PROCESSAMENTO

    pagamento = await adapter.consultar_pagamento(
        registrado.pagamentos[0].id_banco, TipoPagamento.BOLETO
    )
    assert pagamento.status is StatusPagamento.ACEITO
    assert pagamento.valor == Decimal("15.00")
    assert pagamento.autenticacao is None


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("PAGO", StatusPagamento.PAGO),
        ("Aguardando débito", StatusPagamento.EM_PROCESSAMENTO),
        ("REJEITADO", StatusPagamento.REJEITADO),
    ],
)
async def test_estados_de_pagamento(adapter, falso, bruto, esperado):
    registrado = await adapter.pagar_lote(lote(boleto()))
    falso.estado_pagamento = bruto
    pagamento = await adapter.consultar_pagamento(
        registrado.pagamentos[0].id_banco, TipoPagamento.BOLETO
    )
    assert pagamento.status is esperado
    if esperado is StatusPagamento.REJEITADO:
        assert pagamento.motivos == ("295",)


async def test_estado_desconhecido_nao_e_adivinhado(adapter, falso):
    registrado = await adapter.pagar_lote(lote(boleto()))
    falso.estado_pagamento = "BLOQUEADO"
    with pytest.raises(Validacao, match="nao mapeado"):
        await adapter.consultar_pagamento(
            registrado.pagamentos[0].id_banco, TipoPagamento.BOLETO
        )


async def test_cancelar_pagamentos(adapter, falso):
    await adapter.cancelar_pagamentos(DEBITO, ("90026545731030001",))
    assert falso.corpos[-1] == {
        "agenciaDebito": 1607,
        "contaCorrenteDebito": 99738672,
        "digitoVerificadorContaCorrente": "X",
        "listaPagamentos": [{"codigoPagamento": "90026545731030001"}],
    }
    with pytest.raises(Validacao):
        await adapter.cancelar_pagamentos(DEBITO, ())


async def test_sandbox_manda_credenciais_e_escopos_no_corpo(adapter, falso):
    await adapter.reservar_numero_lote()
    assert falso.pedido_token["client_secret"] == "segredo-xyz"
    assert "pagamentos-lote.boletos-requisicao" in falso.pedido_token["scope"].replace(
        "+", " "
    )
