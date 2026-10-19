"""Registro de adapters por entry points `brbank.adapters` (B4)."""

from importlib.metadata import entry_points
from typing import Any

from brbank.adapter import BankAdapter
from brbank.erros import AdapterNaoEncontrado

GRUPO = "brbank.adapters"
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
