"""Flags de capacidade declaradas por adapter (B3)."""

from enum import Flag, auto


class Capacidade(Flag):
    NENHUMA = 0
    HIBRIDO = auto()
    SO_HIBRIDO = auto()
    ALTERACAO = auto()
    BAIXA = auto()
    PDF = auto()
    WEBHOOK = auto()
    PIX_COB = auto()
    PIX_COBV = auto()
    EMISSAO_ASSINCRONA = auto()
    INSTRUCAO_ASSINCRONA = auto()
    CNAB_REMESSA = auto()
    # Assinaturas geridas pelo gateway (brbank.recorrencia).
    RECORRENCIA = auto()
    # Pagina hospedada para o pagador informar o cartao.
    CHECKOUT = auto()
