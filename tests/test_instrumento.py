"""Boleto, guia e Pix copia e cola (funcoes puras de brbank.instrumento)."""

from decimal import Decimal

import pytest

from brbank import (
    TipoCodigo,
    codigo_barras_da_linha,
    ler_brcode,
    linha_digitavel_do_codigo,
    normalizar_codigo_barras,
    normalizar_linha_digitavel,
    tipo_codigo,
)
from brbank.instrumento import _dv_mod11_arrecadacao, crc16_ccitt

# Massa publica da homologacao do Banco do Brasil (docs/bb-homologacao.md).
BOLETO_BB = "00195313800000015000000003051284190526000117"
GUIA_BB = "83630000000641400052836100812355200812351310"
# Exemplo do manual de padroes para iniciacao do Pix (BCB), CRC 1D3D.
BRCODE_BCB = (
    "00020126580014br.gov.bcb.pix0136123e4567-e12b-12d1-a456-426655440000"
    "5204000053039865802BR5913Fulano de Tal6008BRASILIA62070503***63041D3D"
)


def _com_crc(sem_crc: str) -> str:
    return sem_crc + "6304" + crc16_ccitt(sem_crc + "6304")


def _guia_mod11(base_sem_dv: str) -> str:
    """Codigo de guia em modulo 11 (3o digito 8), DV geral na 4a posicao."""
    dv = _dv_mod11_arrecadacao(base_sem_dv)
    return base_sem_dv[:3] + str(dv) + base_sem_dv[3:]


def test_boleto_codigo_e_linha():
    linha = linha_digitavel_do_codigo(BOLETO_BB)
    assert len(linha) == 47
    assert codigo_barras_da_linha(linha) == BOLETO_BB
    assert normalizar_codigo_barras(linha) == BOLETO_BB
    assert tipo_codigo(BOLETO_BB) == TipoCodigo.BOLETO


def test_guia_modulo_10():
    linha = linha_digitavel_do_codigo(GUIA_BB)
    assert len(linha) == 48
    assert normalizar_linha_digitavel(linha) == linha
    assert codigo_barras_da_linha(linha) == GUIA_BB
    assert tipo_codigo(linha) == TipoCodigo.GUIA


def test_guia_modulo_11():
    codigo = _guia_mod11("858" + "0" * 7 + "1234" + "5" * 29)
    linha = linha_digitavel_do_codigo(codigo)
    assert codigo_barras_da_linha(linha) == codigo
    errada = linha[:5] + str((int(linha[5]) + 1) % 10) + linha[6:]
    with pytest.raises(ValueError):
        normalizar_linha_digitavel(errada)


@pytest.mark.parametrize(
    "texto",
    [
        BOLETO_BB[:4] + "0" + BOLETO_BB[5:],  # DV geral do boleto
        GUIA_BB[:3] + "0" + GUIA_BB[4:],  # DV geral da guia
        GUIA_BB[:2] + "5" + GUIA_BB[3:],  # identificador de valor invalido
        "123",
    ],
)
def test_codigo_que_nao_confere(texto):
    with pytest.raises(ValueError):
        normalizar_codigo_barras(texto)


def test_linha_de_guia_com_dv_de_bloco_errado():
    linha = linha_digitavel_do_codigo(GUIA_BB)
    errada = linha[:11] + str((int(linha[11]) + 1) % 10) + linha[12:]
    with pytest.raises(ValueError):
        normalizar_linha_digitavel(errada)
    assert codigo_barras_da_linha(errada) is None
    assert normalizar_linha_digitavel(errada, validar_dv=False) == errada


def test_brcode_estatico_do_bcb():
    brcode = ler_brcode(BRCODE_BCB)
    assert brcode.chave == "123e4567-e12b-12d1-a456-426655440000"
    assert brcode.nome == "Fulano de Tal"
    assert brcode.cidade == "BRASILIA"
    assert brcode.valor is None
    assert brcode.txid is None
    assert not brcode.dinamico


def test_brcode_com_valor_e_txid():
    texto = _com_crc(
        "000201"
        "26360014br.gov.bcb.pix011491500766000147"
        "52040000530398654071234.565802BR"
        "5913EMPRESA TESTE6009SAO PAULO"
        "62090505PED42"
    )
    brcode = ler_brcode(texto)
    assert brcode.chave == "91500766000147"
    assert brcode.valor == Decimal("1234.56")
    assert brcode.txid == "PED42"


def test_brcode_dinamico():
    url = "pix.exemplo.com.br/qr/v2/cobv/abc123"
    conta = f"0014br.gov.bcb.pix25{len(url):02d}{url}"
    texto = _com_crc(
        f"00020126{len(conta):02d}{conta}52040000530398658"
        "02BR5913EMPRESA TESTE6009SAO PAULO62070503***"
    )
    brcode = ler_brcode(texto)
    assert brcode.dinamico
    assert brcode.url == url
    assert brcode.chave is None


@pytest.mark.parametrize(
    "texto",
    [
        BRCODE_BCB[:-4] + "0000",  # CRC
        "texto qualquer",
        _com_crc("000201" + "26180014br.com.outro" + "5802BR"),  # nao e Pix
    ],
)
def test_brcode_invalido(texto):
    with pytest.raises(ValueError):
        ler_brcode(texto)
