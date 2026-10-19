"""Autenticacao plugavel (B6) com cache de token por (conta, escopo) e lock."""

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from brbank.erros import Indisponivel, Validacao


class Autenticador(ABC):
    @abstractmethod
    async def cabecalhos(
        self, escopo: str | None, client: httpx.AsyncClient
    ) -> dict[str, str]: ...

    async def invalidar(self, escopo: str | None) -> None:
        return None


@dataclass(frozen=True)
class _Token:
    valor: str
    expira_em: float

    def __repr__(self) -> str:
        return "_Token(***)"


class _OAuth2Cacheado(Autenticador):
    """Base do OAuth2 com cache de token por (conta, escopo) e lock.

    O token e emitido por escopo e por conta: guardar um so token para tudo
    faz uma leitura reaproveitar o token de escrita de outra conta.
    `escopos` traduz a chave logica usada pelo adapter para o escopo OAuth.
    Subclasses definem so o formulario do pedido (`_formulario`).
    `cabecalhos_token` vai so no pedido de token (ex.: `x-api-key` do Sicredi).
    """

    def __init__(
        self,
        *,
        conta: str,
        url_token: str,
        escopos: Mapping[str, str] | None = None,
        margem_s: float = 60.0,
        relogio: Callable[[], float] = time.monotonic,
        cabecalhos_token: Mapping[str, str] | None = None,
    ) -> None:
        self.conta = conta
        self._cabecalhos_token = dict(cabecalhos_token or {})
        self._url_token = url_token
        self._escopos = dict(escopos or {})
        self._margem = margem_s
        self._relogio = relogio
        self._tokens: dict[tuple[str, str | None], _Token] = {}
        self._locks: dict[tuple[str, str | None], asyncio.Lock] = {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(conta={self.conta!r})"

    @abstractmethod
    def _formulario(self) -> dict[str, str]:
        """Campos do pedido de token, sem o escopo."""

    async def cabecalhos(
        self, escopo: str | None, client: httpx.AsyncClient
    ) -> dict[str, str]:
        token = await self._token(escopo, client)
        return {"Authorization": f"Bearer {token}"}

    async def invalidar(self, escopo: str | None) -> None:
        self._tokens.pop((self.conta, escopo), None)

    async def _token(self, escopo: str | None, client: httpx.AsyncClient) -> str:
        chave = (self.conta, escopo)
        atual = self._tokens.get(chave)
        if atual and atual.expira_em > self._relogio():
            return atual.valor
        lock = self._locks.setdefault(chave, asyncio.Lock())
        async with lock:
            atual = self._tokens.get(chave)
            if atual and atual.expira_em > self._relogio():
                return atual.valor
            novo = await self._pedir(escopo, client)
            self._tokens[chave] = novo
            return novo.valor

    async def _pedir(self, escopo: str | None, client: httpx.AsyncClient) -> _Token:
        dados = self._formulario()
        if escopo is not None:
            dados["scope"] = self._escopos.get(escopo, escopo)
        try:
            resp = await client.post(
                self._url_token, data=dados, headers=self._cabecalhos_token
            )
        except httpx.TransportError as exc:
            raise Indisponivel(
                f"erro de transporte ao obter token: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise Indisponivel(
                "banco indisponivel ao obter token",
                status_http=resp.status_code,
                resposta_bruta=resp.text,
            )
        if resp.status_code >= 400:
            raise Validacao(
                "credenciais recusadas ao obter token",
                status_http=resp.status_code,
                resposta_bruta=resp.text,
            )
        try:
            corpo = resp.json()
            valor = corpo["access_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise Validacao(
                "resposta de token sem access_token", status_http=resp.status_code
            ) from exc
        # O PinBank devolve `expire_in` (sem o s); os demais, `expires_in`.
        duracao = int(corpo.get("expires_in") or corpo.get("expire_in") or 3600)
        vida = max(duracao - self._margem, self._margem)
        return _Token(valor, self._relogio() + vida)


class OAuth2ClientCredentials(_OAuth2Cacheado):
    """OAuth2 `client_credentials`."""

    def __init__(self, *, client_id: str, client_secret: str, **comum: Any) -> None:
        super().__init__(**comum)
        self._client_id = client_id
        self._client_secret = client_secret

    def _formulario(self) -> dict[str, str]:
        return {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "client_credentials",
        }


class OAuth2Password(_OAuth2Cacheado):
    """OAuth2 `password` (usuario e senha), como o PinBank e o Sicredi."""

    def __init__(self, *, usuario: str, senha: str, **comum: Any) -> None:
        super().__init__(**comum)
        self._usuario = usuario
        self._senha = senha

    def _formulario(self) -> dict[str, str]:
        return {
            "username": self._usuario,
            "password": self._senha,
            "grant_type": "password",
        }
