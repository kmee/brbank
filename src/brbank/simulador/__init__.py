"""Simulador HTTP da API de DDA do BTG Empresas (so stdlib + brbank).

Para testes e e2e: `python -m brbank.simulador --porta 8899 --semente demo`.
Ver `docs/simulador.md`.
"""

from brbank.simulador.servidor import Estado, Simulador

__all__ = ["Estado", "Simulador"]
