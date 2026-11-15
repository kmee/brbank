import contextlib
import re
import zlib
from datetime import date

import pytest

from brbank import StatusCanonico, Validacao
from brbank.instrumento import linha_digitavel_do_codigo
from brbank.modelos import Endereco, Pessoa, Titulo, TituloRegistrado
from brbank.pdf import BoletoPdf, Carteira, ConfigBoleto, codigos, gerar_boleto_pdf
from tests.conftest import CPF_VALIDO

# Boleto da massa de homologacao do BB: R$ 15,00.
CODIGO_BB = "00195313800000015000000003051284190526000117"
PIX = (
    "00020101021226830014br.gov.bcb.pix2561qrpix-h.bb.com.br/pix/v2/cobv/"
    "9e881f18cc664fc78f2ca795dbb2bfc15204000053039865406015.005802BR5925"
    "BENEFICIARIO HOMOLOGACAO6008BRASILIA62070503***6304ABCD"
)
ENDERECO = Endereco(
    logradouro="SBS Quadra 1",
    numero="1",
    bairro="Asa Sul",
    cidade="Brasília",
    uf="DF",
    cep="70073900",
)
BENEFICIARIO = Pessoa(
    nome="Empresa Lucro Presumido", documento="81583054000129", endereco=ENDERECO
)


def boleto(valor="15.00", **registro) -> BoletoPdf:
    titulo = Titulo(
        chave_idempotencia="c1",
        seu_numero="FAT/2026/00005-1",
        valor=valor,
        vencimento=date(2026, 12, 15),
        pagador=Pessoa(
            nome="Cliente Homologação", documento=CPF_VALIDO, endereco=ENDERECO
        ),
        beneficiario=BENEFICIARIO,
        mensagens=("Não receber após 30 dias do vencimento.",),
    )
    registro = {"codigo_barras": CODIGO_BB, **registro}
    registrado = TituloRegistrado(
        banco="bb",
        id_banco="00031285570000000001",
        status=StatusCanonico.REGISTRADO,
        nosso_numero="00031285570000000001",
        **registro,
    )
    return BoletoPdf(
        titulo,
        registrado,
        Carteira(banco="001", agencia_codigo="1607 / 3128557", carteira="17"),
    )


def texto_do_pdf(pdf: bytes) -> str:
    """Texto dos fluxos do PDF (descomprimidos quando precisa)."""
    partes = []
    for fluxo in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        with contextlib.suppress(zlib.error):
            fluxo = zlib.decompress(fluxo)
        partes.append(fluxo.decode("latin-1"))
    return "".join(partes)


def test_linha_derivada_do_codigo_confere():
    codigo, linha = codigos(boleto())
    assert codigo == CODIGO_BB
    assert linha == linha_digitavel_do_codigo(CODIGO_BB)


def test_so_a_linha_tambem_serve():
    linha = linha_digitavel_do_codigo(CODIGO_BB)
    assert codigos(boleto(codigo_barras=None, linha_digitavel=linha)) == (
        CODIGO_BB,
        linha,
    )


def test_linha_de_outro_titulo_e_recusada():
    outra = linha_digitavel_do_codigo(CODIGO_BB)
    outra = outra[:33] + "31390000001500"  # outro fator de vencimento, DV geral errado
    with pytest.raises(Validacao):
        codigos(boleto(linha_digitavel=outra))


def test_valor_diferente_do_titulo_e_recusado():
    with pytest.raises(Validacao, match="valor"):
        codigos(boleto(valor="16.00"))


def test_sem_codigo_e_recusado():
    with pytest.raises(Validacao):
        codigos(boleto(codigo_barras=None))


def test_pdf_tem_uma_pagina_por_boleto_e_os_dados():
    pdf = gerar_boleto_pdf([boleto(), boleto()], ConfigBoleto(comprimir=False))
    assert pdf.startswith(b"%PDF")
    assert len(re.findall(rb"/Type /Page\b", pdf)) == 2
    texto = texto_do_pdf(pdf)
    linha = linha_digitavel_do_codigo(CODIGO_BB)
    assert f"{linha[0:5]}.{linha[5:10]}" in texto
    assert "00031285570000000001" in texto
    assert "001-9" in texto
    assert "Ficha de Compensa" in texto
    assert "Pague com Pix" not in texto


def test_boleto_hibrido_traz_o_pix():
    pdf = gerar_boleto_pdf([boleto(pix_copia_cola=PIX)], ConfigBoleto(comprimir=False))
    texto = texto_do_pdf(pdf)
    assert texto.count("Pague com Pix") == 2  # recibo e ficha
    assert "br.gov.bcb.pix" in texto  # copia e cola no recibo


def test_sem_beneficiario_e_recusado():
    b = boleto()
    sem = BoletoPdf(
        b.titulo.model_copy(update={"beneficiario": None}), b.registrado, b.carteira
    )
    with pytest.raises(Validacao, match="beneficiario"):
        gerar_boleto_pdf([sem])


def test_lista_vazia_e_recusada():
    with pytest.raises(Validacao):
        gerar_boleto_pdf([])
