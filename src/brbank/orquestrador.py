"""Orquestrador de emissao (B10): idempotente por chave, fan-out isolado."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from brbank.adapter import BankAdapter
from brbank.erros import Duplicado
from brbank.modelos import Titulo, TituloRegistrado


class RepositorioIdempotencia(Protocol):
    """Guarda o resultado por chave. Em producao vive no banco do chamador."""

    async def obter(self, chave: str) -> TituloRegistrado | None: ...

    async def salvar(self, chave: str, registrado: TituloRegistrado) -> None: ...


class RepositorioEmMemoria:
    def __init__(self) -> None:
        self._dados: dict[str, TituloRegistrado] = {}

    async def obter(self, chave: str) -> TituloRegistrado | None:
        return self._dados.get(chave)

    async def salvar(self, chave: str, registrado: TituloRegistrado) -> None:
        self._dados[chave] = registrado


@dataclass(frozen=True)
class ResultadoEmissao:
    chave: str
    registrado: TituloRegistrado | None = None
    erro: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.erro is None


class Orquestrador:
    """Emite titulos por um adapter (uma conta)."""

    def __init__(
        self,
        adapter: BankAdapter,
        *,
        repositorio: RepositorioIdempotencia | None = None,
        concorrencia: int = 10,
    ) -> None:
        self._adapter = adapter
        self._repo = repositorio or RepositorioEmMemoria()
        self._concorrencia = concorrencia
        self._travas: dict[str, list] = {}

    async def emitir(self, titulo: Titulo) -> TituloRegistrado:
        """Registra o titulo uma unica vez por `chave_idempotencia`.

        Chamadas concorrentes com a mesma chave esperam a primeira. `Duplicado`
        do banco vira consulta quando ele informa o identificador existente.
        """
        chave = titulo.chave_idempotencia
        entrada = self._travas.setdefault(chave, [asyncio.Lock(), 0])
        entrada[1] += 1
        try:
            async with entrada[0]:
                existente = await self._repo.obter(chave)
                if existente is not None:
                    return existente
                try:
                    registrado = await self._adapter.registrar_titulo(titulo)
                except Duplicado as erro:
                    if not erro.id_banco:
                        raise
                    registrado = await self._adapter.consultar_titulo(erro.id_banco)
                await self._repo.salvar(chave, registrado)
                return registrado
        finally:
            entrada[1] -= 1
            if entrada[1] == 0:
                self._travas.pop(chave, None)

    async def emitir_em_varios(
        self, titulos: Sequence[Titulo], *, concorrencia: int | None = None
    ) -> list[ResultadoEmissao]:
        """Emite em paralelo; a falha de um titulo nao derruba os outros.

        O resultado mantem a ordem da entrada.
        """
        limite = asyncio.Semaphore(concorrencia or self._concorrencia)
        resultados: list[ResultadoEmissao | None] = [None] * len(titulos)

        async def um(indice: int, titulo: Titulo) -> None:
            async with limite:
                try:
                    registrado = await self.emitir(titulo)
                except Exception as erro:
                    resultados[indice] = ResultadoEmissao(
                        titulo.chave_idempotencia, erro=erro
                    )
                else:
                    resultados[indice] = ResultadoEmissao(
                        titulo.chave_idempotencia, registrado=registrado
                    )

        # `um` nunca levanta Exception: gather tem o mesmo efeito do TaskGroup
        # (que so existe a partir do Python 3.11).
        await asyncio.gather(
            *(um(indice, titulo) for indice, titulo in enumerate(titulos))
        )
        return [r for r in resultados if r is not None]
