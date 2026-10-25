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
from brbank.recorrencia import (
    AdapterRecorrencia,
    AlteracaoAssinatura,
    Assinatura,
    AssinaturaRegistrada,
    Checkout,
    Ciclo,
    EventoRecorrencia,
    MeioCobranca,
    StatusAssinatura,
    StatusCheckout,
    TipoEventoRecorrencia,
)
from brbank.registro import criar_adapter, listar_adapters, obter_classe
from brbank.status import StatusCanonico, TabelaStatus

__all__ = [
    "AdapterNaoEncontrado",
    "AdapterRecorrencia",
    "AlteracaoAssinatura",
    "Assinatura",
    "AssinaturaRegistrada",
    "BankAdapter",
    "Capacidade",
    "Checkout",
    "Ciclo",
    "Duplicado",
    "Encargos",
    "Endereco",
    "ErroBrbank",
    "EventoCanonico",
    "EventoRecorrencia",
    "Indisponivel",
    "Instrucao",
    "MeioCobranca",
    "NaoSuportado",
    "Orquestrador",
    "Pessoa",
    "ResultadoEmissao",
    "StatusAssinatura",
    "StatusCanonico",
    "StatusCheckout",
    "TabelaStatus",
    "TipoEventoRecorrencia",
    "TipoInstrucao",
    "Titulo",
    "TituloRegistrado",
    "Validacao",
    "criar_adapter",
    "listar_adapters",
    "obter_classe",
]
