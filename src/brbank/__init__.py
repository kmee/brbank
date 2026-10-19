"""brbank: cobranca bancaria unificada, independente do Odoo."""

from brbank.adapter import BankAdapter
from brbank.capacidades import Capacidade
from brbank.erros import (
    AdapterNaoEncontrado,
    Duplicado,
    ErroBrbank,
    Indisponivel,
    NaoSuportado,
    Validacao,
)
from brbank.modelos import (
    Encargos,
    Endereco,
    EventoCanonico,
    Instrucao,
    Pessoa,
    TipoInstrucao,
    Titulo,
    TituloRegistrado,
)
from brbank.orquestrador import Orquestrador, ResultadoEmissao
from brbank.registro import criar_adapter, listar_adapters, obter_classe
from brbank.status import StatusCanonico, TabelaStatus

__all__ = [
    "AdapterNaoEncontrado",
    "BankAdapter",
    "Capacidade",
    "Duplicado",
    "Encargos",
    "Endereco",
    "ErroBrbank",
    "EventoCanonico",
    "Indisponivel",
    "Instrucao",
    "NaoSuportado",
    "Orquestrador",
    "Pessoa",
    "ResultadoEmissao",
    "StatusCanonico",
    "TabelaStatus",
    "TipoInstrucao",
    "Titulo",
    "TituloRegistrado",
    "Validacao",
    "criar_adapter",
    "listar_adapters",
    "obter_classe",
]
