"""Dados ficticios do simulador: linha digitavel valida, CNPJ valido e a semente demo.

Tudo aqui e inventado. Os CNPJs tem DV correto (para passar na validacao dos
consumidores) mas pertencem a empresas imaginarias; os valores e vencimentos da
semente sao descritos em `docs/simulador.md` porque o modulo Odoo de demo cria
faturas que casam com eles.
"""

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from brbank.dda import _dv_mod10, _dv_mod11_barras

# CNPJ da empresa pagadora fictícia (o mesmo dos testes da biblioteca).
CNPJ_DEMO = "11222333000181"
NOME_DEMO = "EMPRESA DEMO LTDA"

_BANCOS = {
    "001": "BANCO DO BRASIL S.A.",
    "033": "BANCO SANTANDER (BRASIL) S.A.",
    "104": "CAIXA ECONOMICA FEDERAL",
    "208": "BANCO BTG PACTUAL S.A.",
    "237": "BANCO BRADESCO S.A.",
    "341": "ITAU UNIBANCO S.A.",
    "756": "SICOOB",
}


def nome_do_banco(codigo: str) -> str:
    return _BANCOS.get(codigo, f"BANCO {codigo}")


def cnpj_com_dv(base12: str) -> str:
    """Completa 12 digitos com os dois DVs (modulo 11)."""

    def dv(base: str, pesos: tuple[int, ...]) -> int:
        resto = sum(int(c) * p for c, p in zip(base, pesos, strict=True)) % 11
        return 0 if resto < 2 else 11 - resto

    p1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    d1 = dv(base12, p1)
    d2 = dv(base12 + str(d1), (6, *p1))
    return f"{base12}{d1}{d2}"


def fator_vencimento(vencimento: date) -> int:
    """Fator de vencimento do boleto (ciclo reiniciado em 22/02/2025, base 1000)."""
    return 1000 + (vencimento - date(2025, 2, 22)).days


def montar_linha_digitavel(
    *, banco: str, valor: Decimal, vencimento: date, semente: str
) -> str:
    """Linha digitavel de boleto bancario (47 digitos) com todos os DVs corretos.

    O campo livre (25 digitos) sai de um hash de `semente`: estavel entre
    execucoes e diferente de um boleto para o outro.
    """
    centavos = int((valor * 100).to_integral_value())
    if not 0 < centavos < 10**10:
        raise ValueError("valor fora do que cabe na linha digitavel")
    fator = fator_vencimento(vencimento)
    if not 1000 <= fator <= 9999:
        raise ValueError("vencimento fora do fator de vencimento")
    livre = "".join(str(b % 10) for b in hashlib.sha256(semente.encode()).digest()[:25])
    cabeca = f"{banco}9"
    valor_campo = f"{fator:04d}{centavos:010d}"
    dv_geral = _dv_mod11_barras(cabeca + valor_campo + livre)
    campo1 = cabeca + livre[:5]
    campo2 = livre[5:15]
    campo3 = livre[15:25]
    return (
        f"{campo1}{_dv_mod10(campo1)}"
        f"{campo2}{_dv_mod10(campo2)}"
        f"{campo3}{_dv_mod10(campo3)}"
        f"{dv_geral}{valor_campo}"
    )


@dataclass(frozen=True)
class Beneficiario:
    chave: str
    razao_social: str
    fantasia: str
    cnpj: str
    banco: str


def _beneficiario(chave: str, base12: str, razao: str, fantasia: str, banco: str):
    return Beneficiario(chave, razao, fantasia, cnpj_com_dv(base12), banco)


# Beneficiarios fictícios da semente (ASCII, sem acento: vao em JSON e em log).
BENEFICIARIOS = {
    b.chave: b
    for b in (
        _beneficiario(
            "alfa", "450000010001", "ALFA EMBALAGENS LTDA", "ALFA EMBALAGENS", "341"
        ),
        _beneficiario("beta", "450000020001", "BETA PAPEIS S.A.", "BETA PAPEIS", "001"),
        _beneficiario(
            "gama", "450000030001", "GAMA LOGISTICA LTDA", "GAMA LOGISTICA", "237"
        ),
        _beneficiario(
            "delta", "450000040001", "DELTA LOCACOES LTDA", "DELTA LOCACOES", "104"
        ),
        _beneficiario(
            "omega",
            "450000050001",
            "OMEGA SECURITIZADORA DE CREDITOS S.A.",
            "OMEGA CREDITOS",
            "208",
        ),
        _beneficiario(
            "epsilon",
            "450000060001",
            "EPSILON INDUSTRIA LTDA",
            "EPSILON INDUSTRIA",
            "033",
        ),
        _beneficiario(
            "sigma", "450000070001", "SIGMA SERVICOS LTDA", "SIGMA SERVICOS", "756"
        ),
        _beneficiario(
            "zeta", "450000080001", "ZETA METALURGICA LTDA", "ZETA METALURGICA", "001"
        ),
        _beneficiario(
            "eta", "450000090001", "ETA SUPRIMENTOS LTDA", "ETA SUPRIMENTOS", "341"
        ),
        _beneficiario(
            "teta", "450000100001", "TETA MANUTENCAO LTDA", "TETA MANUTENCAO", "237"
        ),
        _beneficiario(
            "iota", "450000110001", "IOTA GRAFICA LTDA", "IOTA GRAFICA", "104"
        ),
    )
}


@dataclass(frozen=True)
class DebitoSemente:
    """Um boleto da semente: vencimento em dias a partir de `hoje`."""

    ordem: int
    beneficiario: str
    valor: Decimal
    dias: int
    caso: str


SEMENTE_DEMO: tuple[DebitoSemente, ...] = (
    DebitoSemente(
        1, "alfa", Decimal("1240.00"), 5, "valor e vencimento iguais ao da fatura"
    ),
    DebitoSemente(
        2, "beta", Decimal("3587.50"), 8, "divergencia de valor (fatura de 3500.00)"
    ),
    DebitoSemente(
        3,
        "gama",
        Decimal("890.00"),
        10,
        "divergencia de vencimento (fatura vence 3 dias antes)",
    ),
    DebitoSemente(4, "delta", Decimal("2500.00"), 15, "duas parcelas iguais (1 de 2)"),
    DebitoSemente(5, "delta", Decimal("2500.00"), 15, "duas parcelas iguais (2 de 2)"),
    DebitoSemente(
        6, "omega", Decimal("4200.00"), 9, "cessao: emitido pela securitizadora"
    ),
    DebitoSemente(7, "sigma", Decimal("760.00"), 6, "CNPJ desconhecido, sem fatura"),
    DebitoSemente(
        8, "zeta", Decimal("3000.00"), 11, "agrupa duas NFs (1800.00 e 1200.00)"
    ),
    DebitoSemente(9, "eta", Decimal("540.00"), 4, "fornecedor conhecido sem fatura"),
    DebitoSemente(
        10, "teta", Decimal("1100.00"), -3, "boleto vencido com fatura vencida"
    ),
    DebitoSemente(11, "iota", Decimal("680.00"), 7, "possivel duplicidade (1 de 2)"),
    DebitoSemente(12, "iota", Decimal("680.00"), 7, "possivel duplicidade (2 de 2)"),
)


def vencimento_da_semente(item: DebitoSemente, hoje: date) -> date:
    return hoje + timedelta(days=item.dias)
