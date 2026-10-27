"""Autenticacao plugavel (B6) com cache de token por (conta, escopo) e lock."""

import asyncio
import base64
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

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


@dataclass(frozen=True)
class TokenPersistido:
    """Access token guardado fora do processo. `expira_em` e epoch (`time.time()`):
    o relogio monotonico nao atravessa processos."""

    valor: str
    expira_em: float

    def __repr__(self) -> str:
        return "TokenPersistido(***)"


@runtime_checkable
class ArmazemToken(Protocol):
    """Onde o token vive entre processos (no Odoo, uma tabela e uma trava de banco).

    A `trava` serializa a renovacao: e sincrona, espera quem a segura e vale
    entre processos. Quem a pega le o token de novo antes de renovar.
    """

    def ler(self, chave: tuple[str, str | None]) -> TokenPersistido | None: ...

    def gravar(self, chave: tuple[str, str | None], token: TokenPersistido) -> None: ...

    def trava(self, chave: tuple[str, str | None]) -> AbstractContextManager[None]: ...


class _OAuth2Cacheado(Autenticador):
    """Base do OAuth2 com cache de token por (conta, escopo) e lock.

    O token e emitido por escopo e por conta: guardar um so token para tudo
    faz uma leitura reaproveitar o token de escrita de outra conta.
    `escopos` traduz a chave logica usada pelo adapter para o escopo OAuth.
    Subclasses definem so o formulario do pedido (`_formulario`).
    `cabecalhos_token` vai so no pedido de token (ex.: `x-api-key` do Sicredi).

    Com `armazem`, o token tambem vive fora do processo: a memoria vale, depois
    o armazem, e so entao um pedido novo, feito sob a trava do armazem (outro
    processo pode ter renovado enquanto esperavamos). O relogio passa a ser
    `time.time`, que atravessa processos.
    """

    def __init__(
        self,
        *,
        conta: str,
        url_token: str,
        escopos: Mapping[str, str] | None = None,
        margem_s: float = 60.0,
        relogio: Callable[[], float] | None = None,
        cabecalhos_token: Mapping[str, str] | None = None,
        armazem: ArmazemToken | None = None,
    ) -> None:
        self.conta = conta
        self._cabecalhos_token = dict(cabecalhos_token or {})
        self._url_token = url_token
        self._escopos = dict(escopos or {})
        self._margem = margem_s
        self._armazem = armazem
        if relogio is None:
            relogio = time.time if armazem is not None else time.monotonic
        self._relogio = relogio
        self._tokens: dict[tuple[str, str | None], _Token] = {}
        self._locks: dict[tuple[str, str | None], asyncio.Lock] = {}
        # Token que o banco recusou (401): o armazem pode ainda te-lo como valido.
        self._recusados: dict[tuple[str, str | None], str] = {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(conta={self.conta!r})"

    @abstractmethod
    def _formulario(self) -> dict[str, str]:
        """Campos do pedido de token, sem o escopo."""

    def _cabecalhos_pedido(self) -> dict[str, str]:
        """Cabecalhos do pedido de token (ex.: Basic): os de `cabecalhos_token`."""
        return dict(self._cabecalhos_token)

    def _argumentos_post(self, dados: dict[str, str]) -> dict[str, Any]:
        """Como o pedido de token vai ao banco. Padrao: formulario."""
        return {"data": dados, "headers": self._cabecalhos_pedido()}

    async def cabecalhos(
        self, escopo: str | None, client: httpx.AsyncClient
    ) -> dict[str, str]:
        token = await self._token(escopo, client)
        return {"Authorization": f"Bearer {token}"}

    async def invalidar(self, escopo: str | None) -> None:
        chave = (self.conta, escopo)
        antigo = self._tokens.pop(chave, None)
        if antigo is not None and self._armazem is not None:
            self._recusados[chave] = antigo.valor

    async def _token(self, escopo: str | None, client: httpx.AsyncClient) -> str:
        chave = (self.conta, escopo)
        atual = self._tokens.get(chave)
        if atual and atual.expira_em > self._relogio():
            return atual.valor
        if self._armazem is not None:
            return await self._token_com_armazem(chave, escopo, client)
        lock = self._locks.setdefault(chave, asyncio.Lock())
        async with lock:
            atual = self._tokens.get(chave)
            if atual and atual.expira_em > self._relogio():
                return atual.valor
            novo = await self._pedir(escopo, client)
            self._tokens[chave] = novo
            return novo.valor

    def _do_armazem(self, chave: tuple[str, str | None]) -> _Token | None:
        """Token do armazem se ainda vale e o banco nao o recusou."""
        assert self._armazem is not None
        persistido = self._armazem.ler(chave)
        if (
            persistido is None
            or persistido.expira_em <= self._relogio()
            or persistido.valor == self._recusados.get(chave)
        ):
            return None
        return _Token(persistido.valor, persistido.expira_em)

    async def _token_com_armazem(
        self,
        chave: tuple[str, str | None],
        escopo: str | None,
        client: httpx.AsyncClient,
    ) -> str:
        armazem = self._armazem
        assert armazem is not None
        lido = await asyncio.to_thread(self._do_armazem, chave)
        if lido is not None:
            self._tokens[chave] = lido
            return lido.valor
        # Trava do processo primeiro: so uma corrotina por chave espera a trava
        # do armazem (que ocupa uma thread enquanto espera).
        lock = self._locks.setdefault(chave, asyncio.Lock())
        async with lock:
            atual = self._tokens.get(chave)
            if atual and atual.expira_em > self._relogio():
                return atual.valor
            trava = armazem.trava(chave)
            # Uma thread so para entrar, ler, gravar e sair: travas presas a thread
            # (RLock, cursor de banco) exigem que quem entra seja quem sai.
            laco = asyncio.get_running_loop()
            fio = ThreadPoolExecutor(max_workers=1)
            try:
                await laco.run_in_executor(fio, trava.__enter__)
                try:
                    # Outro processo pode ter renovado enquanto esperavamos.
                    lido = await laco.run_in_executor(fio, self._do_armazem, chave)
                    if lido is None:
                        novo = await self._pedir(escopo, client)
                        await laco.run_in_executor(
                            fio,
                            armazem.gravar,
                            chave,
                            TokenPersistido(novo.valor, novo.expira_em),
                        )
                        lido = novo
                except BaseException as exc:
                    await laco.run_in_executor(
                        fio, trava.__exit__, type(exc), exc, exc.__traceback__
                    )
                    raise
                else:
                    await laco.run_in_executor(fio, trava.__exit__, None, None, None)
            finally:
                fio.shutdown(wait=False)
            self._tokens[chave] = lido
            return lido.valor

    async def _pedir(self, escopo: str | None, client: httpx.AsyncClient) -> _Token:
        dados = self._formulario()
        if escopo is not None:
            dados["scope"] = self._escopos.get(escopo, escopo)
        try:
            resp = await client.post(self._url_token, **self._argumentos_post(dados))
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


class OAuth2ClientCredentialsBasic(_OAuth2Cacheado):
    """OAuth2 `client_credentials` com `Authorization: Basic` (BB, Sicredi,
    Bradesco, Efi). `corpo_json` manda o grant em JSON em vez de formulario."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        corpo_json: bool = False,
        **comum: Any,
    ) -> None:
        super().__init__(**comum)
        self._client_id = client_id
        self._client_secret = client_secret
        self._corpo_json = corpo_json

    def _formulario(self) -> dict[str, str]:
        return {"grant_type": "client_credentials"}

    def _argumentos_post(self, dados: dict[str, str]) -> dict[str, Any]:
        basico = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        argumentos: dict[str, Any] = {
            "headers": {**self._cabecalhos_pedido(), "Authorization": f"Basic {basico}"}
        }
        argumentos["json" if self._corpo_json else "data"] = dados
        return argumentos


class OAuth2ClientCredentialsSemSegredo(_OAuth2Cacheado):
    """`client_credentials` so com `client_id` (Sicoob: a identidade e o mTLS)."""

    def __init__(self, *, client_id: str, **comum: Any) -> None:
        super().__init__(**comum)
        self._client_id = client_id

    def _formulario(self) -> dict[str, str]:
        return {"client_id": self._client_id, "grant_type": "client_credentials"}


class TokenEstatico(Autenticador):
    """Token fixo, para sandbox que o entrega pronto no portal (Sicoob)."""

    def __init__(self, token: str) -> None:
        self._token = token

    def __repr__(self) -> str:
        return "TokenEstatico(***)"

    async def cabecalhos(
        self, escopo: str | None, client: httpx.AsyncClient
    ) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


class ChaveApi(Autenticador):
    """Chave fixa num cabecalho proprio (Asaas: `access_token`)."""

    def __init__(self, cabecalho: str, valor: str) -> None:
        self._cabecalho = cabecalho
        self._valor = valor

    def __repr__(self) -> str:
        return f"ChaveApi({self._cabecalho!r}, ***)"

    async def cabecalhos(
        self, escopo: str | None, client: httpx.AsyncClient
    ) -> dict[str, str]:
        return {self._cabecalho: self._valor}
