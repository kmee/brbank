"""Autenticacao plugavel (B6) com cache de token por (conta, escopo) e lock."""

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass

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


class OAuth2ClientCredentials(Autenticador):
    """OAuth2 `client_credentials`.

    O token e emitido por escopo e por conta: guardar um so token para tudo
    faz uma leitura reaproveitar o token de escrita de outra conta.
    `escopos` traduz a chave logica usada pelo adapter para o escopo OAuth.
    """

    def __init__(
        self,
        *,
        conta: str,
        url_token: str,
        client_id: str,
        client_secret: str,
        escopos: Mapping[str, str] | None = None,
        margem_s: float = 60.0,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self.conta = conta
        self._url_token = url_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._escopos = dict(escopos or {})
        self._margem = margem_s
        self._relogio = relogio
        self._tokens: dict[tuple[str, str | None], _Token] = {}
        self._locks: dict[tuple[str, str | None], asyncio.Lock] = {}

    def __repr__(self) -> str:
        return f"OAuth2ClientCredentials(conta={self.conta!r})"

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
        dados = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "client_credentials",
        }
        if escopo is not None:
            dados["scope"] = self._escopos.get(escopo, escopo)
        try:
            resp = await client.post(self._url_token, data=dados)
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
        duracao = int(corpo.get("expires_in") or 3600)
        vida = max(duracao - self._margem, self._margem)
        return _Token(valor, self._relogio() + vida)
