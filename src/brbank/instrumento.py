"""Instrumentos de pagamento: boleto, guia de arrecadacao e Pix copia e cola.

Funcoes puras, sem rede: conferem digito verificador, convertem linha digitavel em
codigo de barras e leem o BR Code do Pix. Nunca inventam um codigo: entrada que nao
confere levanta ``ValueError``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from brbank._compat import StrEnum
from brbank.tempo import hoje_brasil

# --- boleto e guia -----------------------------------------------------------


class TipoCodigo(StrEnum):
    BOLETO = "BOLETO"
    GUIA = "GUIA"


def _dv_mod10(campo: str) -> int:
    soma = 0
    for posicao, digito in enumerate(reversed(campo)):
        produto = int(digito) * (2 if posicao % 2 == 0 else 1)
        soma += produto - 9 if produto > 9 else produto
    return (10 - soma % 10) % 10


def _dv_mod11_barras(base: str) -> int:
    """DV geral do codigo de barras de boleto bancario (43 digitos, sem o DV)."""
    soma = sum(int(d) * (2 + i % 8) for i, d in enumerate(reversed(base)))
    dv = 11 - soma % 11
    return 1 if dv in (0, 10, 11) else dv


def _dv_mod11_arrecadacao(base: str) -> int:
    """DV de modulo 11 da arrecadacao (FEBRABAN): resto 0 ou 1 da DV 0."""
    soma = sum(int(d) * (2 + i % 8) for i, d in enumerate(reversed(base)))
    resto = soma % 11
    return 0 if resto in (0, 1) else 11 - resto


def _dv_arrecadacao(codigo: str):
    """Funcao de DV da guia, escolhida pelo identificador de valor (3o digito)."""
    if codigo[2] in "67":
        return _dv_mod10
    if codigo[2] in "89":
        return _dv_mod11_arrecadacao
    raise ValueError("guia com identificador de valor invalido")


def _barras_de_47(linha: str) -> str:
    return (
        linha[0:4] + linha[32] + linha[33:47] + linha[4:9] + linha[10:20] + linha[21:31]
    )


def _dvs_da_linha_47_conferem(linha: str) -> bool:
    for inicio, fim in ((0, 9), (10, 20), (21, 31)):
        if _dv_mod10(linha[inicio:fim]) != int(linha[fim]):
            return False
    return _codigo_boleto_confere(_barras_de_47(linha))


def _codigo_boleto_confere(codigo: str) -> bool:
    return _dv_mod11_barras(codigo[:4] + codigo[5:]) == int(codigo[4])


def _codigo_guia_confere(codigo: str) -> bool:
    dv = _dv_arrecadacao(codigo)
    return dv(codigo[:3] + codigo[4:]) == int(codigo[3])


def _dvs_da_linha_48_conferem(linha: str) -> bool:
    if linha[0] != "8":
        return False
    dv = _dv_arrecadacao(linha)
    for inicio in range(0, 48, 12):
        if dv(linha[inicio : inicio + 11]) != int(linha[inicio + 11]):
            return False
    return _codigo_guia_confere(_barras_de_48(linha))


def _barras_de_48(linha: str) -> str:
    return "".join(linha[i : i + 11] for i in range(0, 48, 12))


def _so_digitos(texto: str) -> str:
    if not isinstance(texto, str):
        raise ValueError("codigo deve ser texto")
    return re.sub(r"[\s.\-]", "", texto)


def normalizar_linha_digitavel(texto: str, *, validar_dv: bool = True) -> str:
    """So digitos; confere tamanho (47 ou 48) e, por padrao, os DVs.

    Boleto bancario (47): DV modulo 10 dos tres primeiros campos e DV geral
    (modulo 11) do codigo de barras. Guia de arrecadacao (48, comeca com 8): DV de
    cada bloco e DV geral, em modulo 10 ou 11 conforme o identificador de valor.

    :raises ValueError: se nao for uma linha digitavel valida.
    """
    digitos = _so_digitos(texto)
    if not digitos.isdigit() or len(digitos) not in (47, 48):
        raise ValueError("linha digitavel deve ter 47 ou 48 digitos")
    if validar_dv:
        confere = (
            _dvs_da_linha_47_conferem(digitos)
            if len(digitos) == 47
            else _dvs_da_linha_48_conferem(digitos)
        )
        if not confere:
            raise ValueError("linha digitavel com digito verificador invalido")
    return digitos


def codigo_barras_da_linha(linha: str) -> str | None:
    """Codigo de barras (44 digitos) de uma linha digitavel que confere os DVs.

    Devolve None se a linha nao for valida: nunca inventa um codigo de barras.
    """
    try:
        digitos = normalizar_linha_digitavel(linha)
    except ValueError:
        return None
    if len(digitos) == 47:
        return _barras_de_47(digitos)
    return _barras_de_48(digitos)


def tipo_codigo(codigo: str) -> TipoCodigo:
    """Boleto ou guia, pelo primeiro digito do codigo de barras (ou da linha)."""
    return TipoCodigo.GUIA if _so_digitos(codigo).startswith("8") else TipoCodigo.BOLETO


def normalizar_codigo_barras(texto: str) -> str:
    """Codigo de barras de 44 digitos a partir dele mesmo ou da linha digitavel.

    Confere o DV geral (boleto) ou o DV geral da arrecadacao (guia).

    :raises ValueError: se o codigo nao conferir.
    """
    digitos = _so_digitos(texto)
    if len(digitos) in (47, 48):
        return codigo_barras_da_linha(digitos) or _falha_dv()
    if not digitos.isdigit() or len(digitos) != 44:
        raise ValueError("codigo de barras deve ter 44 digitos")
    confere = (
        _codigo_guia_confere(digitos)
        if digitos[0] == "8"
        else _codigo_boleto_confere(digitos)
    )
    return digitos if confere else _falha_dv()


_BASE_FATOR = date(1997, 10, 7)
# FEBRABAN: o fator chegou a 9999 em 21/02/2025 e voltou a 1000 no dia seguinte.
_BASE_FATOR_2025 = date(2025, 2, 22)


def vencimento_do_boleto(codigo: str, *, hoje: date | None = None) -> date | None:
    """Vencimento pelo fator (posicoes 6 a 9) do codigo de barras ou da linha.

    O fator repete a cada 9000 dias (reinicio de 2025): vale a data mais perto de
    `hoje`. Fator 0000 (sem vencimento) e guia de arrecadacao devolvem None.

    :raises ValueError: se o codigo nao conferir.
    """
    codigo = normalizar_codigo_barras(codigo)
    if codigo[0] == "8":
        return None
    fator = int(codigo[5:9])
    if fator == 0:
        return None
    referencia = hoje or hoje_brasil()
    candidatas = [_BASE_FATOR + timedelta(days=fator)]
    if fator >= 1000:
        candidatas.append(_BASE_FATOR_2025 + timedelta(days=fator - 1000))
    return min(candidatas, key=lambda d: abs((d - referencia).days))


def _falha_dv():
    raise ValueError("codigo com digito verificador invalido")


def linha_digitavel_do_codigo(codigo: str) -> str:
    """Linha digitavel (47 ou 48 digitos) de um codigo de barras que confere."""
    codigo = normalizar_codigo_barras(codigo)
    if codigo[0] == "8":
        dv = _dv_arrecadacao(codigo)
        return "".join(
            codigo[i : i + 11] + str(dv(codigo[i : i + 11])) for i in range(0, 44, 11)
        )
    livre = codigo[19:44]
    campo1 = codigo[0:4] + livre[0:5]
    campo2 = livre[5:15]
    campo3 = livre[15:25]
    return (
        f"{campo1}{_dv_mod10(campo1)}"
        f"{campo2}{_dv_mod10(campo2)}"
        f"{campo3}{_dv_mod10(campo3)}"
        f"{codigo[4]}{codigo[5:19]}"
    )


# --- Pix copia e cola (BR Code) ----------------------------------------------

_GUI_PIX = "br.gov.bcb.pix"


@dataclass(frozen=True)
class BrCode:
    """Dados de um BR Code do Pix (QR estatico ou dinamico)."""

    payload: str
    chave: str | None = None
    url: str | None = None
    valor: Decimal | None = None
    nome: str | None = None
    cidade: str | None = None
    txid: str | None = None

    @property
    def dinamico(self) -> bool:
        """QR dinamico: o recebedor e o valor vem da URL, no PSP do recebedor."""
        return bool(self.url)


def crc16_ccitt(dados: str) -> str:
    """CRC16-CCITT-FALSE (polinomio 0x1021, inicial 0xFFFF), em 4 hexadecimais."""
    crc = 0xFFFF
    for byte in dados.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def _tlv(texto: str) -> dict[str, str]:
    campos: dict[str, str] = {}
    posicao = 0
    while posicao < len(texto):
        if posicao + 4 > len(texto) or not texto[posicao + 2 : posicao + 4].isdigit():
            raise ValueError("BR Code mal formado")
        ident = texto[posicao : posicao + 2]
        tamanho = int(texto[posicao + 2 : posicao + 4])
        valor = texto[posicao + 4 : posicao + 4 + tamanho]
        if len(valor) != tamanho:
            raise ValueError("BR Code mal formado")
        campos[ident] = valor
        posicao += 4 + tamanho
    return campos


def ler_brcode(texto: str) -> BrCode:
    """Le o Pix copia e cola: confere o CRC e extrai chave, URL, valor e recebedor.

    :raises ValueError: se o texto nao for um BR Code do Pix ou o CRC nao conferir.
    """
    if not isinstance(texto, str):
        raise ValueError("BR Code deve ser texto")
    payload = texto.strip()
    if len(payload) < 8 or payload[-8:-4] != "6304":
        raise ValueError("BR Code sem CRC")
    if crc16_ccitt(payload[:-4]) != payload[-4:].upper():
        raise ValueError("BR Code com CRC invalido")
    campos = _tlv(payload)
    if campos.get("00") != "01":
        raise ValueError("BR Code com formato desconhecido")
    conta = next(
        (
            _tlv(valor)
            for ident, valor in campos.items()
            if "26" <= ident <= "51" and _tlv(valor).get("00", "").lower() == _GUI_PIX
        ),
        None,
    )
    if conta is None:
        raise ValueError("BR Code nao e de Pix")
    valor = None
    if campos.get("54"):
        try:
            valor = Decimal(campos["54"])
        except InvalidOperation as erro:
            raise ValueError("BR Code com valor invalido") from erro
    adicionais = _tlv(campos["62"]) if campos.get("62") else {}
    txid = adicionais.get("05")
    return BrCode(
        payload=payload,
        chave=conta.get("01") or None,
        url=conta.get("25") or None,
        valor=valor,
        nome=campos.get("59") or None,
        cidade=campos.get("60") or None,
        txid=None if txid in (None, "***") else txid,
    )
