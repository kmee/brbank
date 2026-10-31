"""Data corrente em Brasilia, nao a do servidor.

Odoo costuma rodar em UTC: entre 21h e meia-noite de Brasilia a data do servidor ja e a
do dia seguinte, e um pagamento ou boleto de hoje pareceria estar no passado.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")


def hoje_brasil(agora: datetime | None = None) -> date:
    return (agora or datetime.now(timezone.utc)).astimezone(FUSO_BRASIL).date()
