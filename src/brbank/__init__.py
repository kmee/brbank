"""brbank: cobranca bancaria unificada, independente do Odoo."""

from brbank.adapter import BankAdapter
from brbank.auth import ArmazemToken, TokenPersistido
from brbank.capacidades import Capacidade
from brbank.dda import (
    AdapterDda,
    CapacidadeDda,
    DebitoDda,
    EventoDda,
    PaginaDebitos,
    ResumoDda,
    StatusDda,
    TipoEventoDda,
    normalizar_linha_digitavel,
)
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
    "AdapterDda",
    "AdapterNaoEncontrado",
    "AdapterRecorrencia",
    "AlteracaoAssinatura",
    "ArmazemToken",
    "Assinatura",
    "AssinaturaRegistrada",
    "BankAdapter",
    "Capacidade",
    "CapacidadeDda",
    "Checkout",
    "Ciclo",
    "DebitoDda",
    "Duplicado",
    "Encargos",
    "Endereco",
    "ErroBrbank",
    "EventoCanonico",
    "EventoDda",
    "EventoRecorrencia",
    "Indisponivel",
    "Instrucao",
    "MeioCobranca",
    "NaoSuportado",
    "Orquestrador",
    "PaginaDebitos",
    "Pessoa",
    "ResultadoEmissao",
    "ResumoDda",
    "StatusAssinatura",
    "StatusCanonico",
    "StatusCheckout",
    "StatusDda",
    "TabelaStatus",
    "TipoEventoDda",
    "TipoEventoRecorrencia",
    "TipoInstrucao",
    "Titulo",
    "TituloRegistrado",
    "TokenPersistido",
    "Validacao",
    "criar_adapter",
    "listar_adapters",
    "normalizar_linha_digitavel",
    "obter_classe",
]
