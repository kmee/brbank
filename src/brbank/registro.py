"""Registro de adapters por entry points (B4).

`brbank.adapters`: cobranca (`BankAdapter`). `brbank.adapters_pagamento`: pagamento
(`AdapterPagamento`), por banco; o Odoo descobre o suporte a Pix copia e cola por
`obter_classe_pagamento(nome).capacidades_pagamento`, sem instanciar.
"""

from importlib.metadata import entry_points
from typing import Any

from brbank.adapter import BankAdapter
from brbank.erros import AdapterNaoEncontrado
from brbank.pagamento import AdapterPagamento

GRUPO = "brbank.adapters"
GRUPO_PAGAMENTO = "brbank.adapters_pagamento"
_manuais: dict[str, type[BankAdapter]] = {}


def registrar_adapter(nome: str, classe: type[BankAdapter]) -> None:
    """Registro em processo, util em testes e em plugins sem pacote."""
    _manuais[nome] = classe


def listar_adapters() -> list[str]:
    nomes = {ep.name for ep in entry_points(group=GRUPO)}
    return sorted(nomes | set(_manuais))


def obter_classe(nome: str) -> type[BankAdapter]:
    if nome in _manuais:
        return _manuais[nome]
    for ep in entry_points(group=GRUPO):
        if ep.name == nome:
            return ep.load()
    raise AdapterNaoEncontrado(
        f"adapter {nome!r} nao instalado (disponiveis: {listar_adapters()})"
    )


def criar_adapter(nome: str, *args: Any, **kwargs: Any) -> BankAdapter:
    return obter_classe(nome)(*args, **kwargs)


def listar_adapters_pagamento() -> list[str]:
    return sorted({ep.name for ep in entry_points(group=GRUPO_PAGAMENTO)})


def obter_classe_pagamento(nome: str) -> type[AdapterPagamento]:
    for ep in entry_points(group=GRUPO_PAGAMENTO):
        if ep.name == nome:
            return ep.load()
    raise AdapterNaoEncontrado(
        f"adapter de pagamento {nome!r} nao instalado "
        f"(disponiveis: {listar_adapters_pagamento()})"
    )


def criar_adapter_pagamento(nome: str, *args: Any, **kwargs: Any) -> AdapterPagamento:
    return obter_classe_pagamento(nome)(*args, **kwargs)
