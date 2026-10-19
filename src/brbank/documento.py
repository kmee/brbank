"""CPF e CNPJ (numerico e alfanumerico) normalizados e validados.

CNPJ alfanumerico: as 12 primeiras posicoes aceitam 0-9 e as 26 letras A-Z, as
2 ultimas sao digitos verificadores (modulo 11, valor de cada caractere =
codigo ASCII - 48). Nao ha letras vedadas.
"""

import re

_PESOS_CNPJ_1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_PESOS_CNPJ_2 = (6, *_PESOS_CNPJ_1)
_FORMATO_CNPJ = re.compile(r"[A-Z0-9]{12}[0-9]{2}")


def normalizar(valor: str) -> str:
    """Remove pontuacao e deixa em maiusculas, sem validar."""
    return re.sub(r"[^0-9A-Za-z]", "", valor).upper()


def _dv_cnpj(base: str, pesos: tuple[int, ...]) -> int:
    soma = sum((ord(c) - 48) * p for c, p in zip(base, pesos, strict=True))
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def cnpj_valido(valor: str) -> bool:
    cnpj = normalizar(valor)
    if not _FORMATO_CNPJ.fullmatch(cnpj) or len(set(cnpj)) == 1:
        return False
    dv1 = _dv_cnpj(cnpj[:12], _PESOS_CNPJ_1)
    dv2 = _dv_cnpj(cnpj[:12] + str(dv1), _PESOS_CNPJ_2)
    return cnpj[12:] == f"{dv1}{dv2}"


def _dv_cpf(base: str) -> int:
    pesos = range(len(base) + 1, 1, -1)
    soma = sum(int(c) * p for c, p in zip(base, pesos, strict=True))
    resto = (soma * 10) % 11
    return 0 if resto == 10 else resto


def cpf_valido(valor: str) -> bool:
    cpf = normalizar(valor)
    if not re.fullmatch(r"\d{11}", cpf) or len(set(cpf)) == 1:
        return False
    dv1 = _dv_cpf(cpf[:9])
    dv2 = _dv_cpf(cpf[:9] + str(dv1))
    return cpf[9:] == f"{dv1}{dv2}"


def normalizar_documento(valor: str) -> str:
    """Devolve o CPF (11 digitos) ou CNPJ (14 caracteres) valido, normalizado.

    :raises ValueError: se nao for CPF nem CNPJ valido.
    """
    doc = normalizar(valor)
    if len(doc) == 11 and cpf_valido(doc):
        return doc
    if len(doc) == 14 and cnpj_valido(doc):
        return doc
    raise ValueError("documento nao e um CPF nem um CNPJ valido")


def tipo_documento(valor: str) -> str:
    """`FISICA` para CPF, `JURIDICA` para CNPJ (documento ja normalizado)."""
    return "FISICA" if len(valor) == 11 else "JURIDICA"
