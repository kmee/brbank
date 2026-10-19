"""Transporte httpx async: mTLS, HTTP/2, retry e rate limit por conta (B5)."""

import asyncio
import json as jsonlib
import logging
import os
import random
import ssl
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from brbank.auth import Autenticador
from brbank.envelope import Envelope, EnvelopeNulo
from brbank.erros import Duplicado, ErroBrbank, Indisponivel, Validacao

log = logging.getLogger("brbank.transporte")


def contexto_tls(cert_pem: bytes, key_pem: bytes | None = None) -> ssl.SSLContext:
    """Monta o contexto mTLS sem deixar a chave privada no disco.

    Os arquivos existem so durante `load_cert_chain` (tmpfs quando houver) e
    o diretorio some ao sair.
    """
    base = "/dev/shm" if os.path.isdir("/dev/shm") else None
    contexto = ssl.create_default_context()
    with tempfile.TemporaryDirectory(dir=base) as pasta:
        caminho_cert = os.path.join(pasta, "cert.pem")
        caminho_chave = os.path.join(pasta, "key.pem") if key_pem else None
        for caminho, conteudo in ((caminho_cert, cert_pem), (caminho_chave, key_pem)):
            if caminho and conteudo:
                fd = os.open(caminho, os.O_WRONLY | os.O_CREAT, 0o600)
                with os.fdopen(fd, "wb") as arquivo:
                    arquivo.write(conteudo)
        contexto.load_cert_chain(caminho_cert, caminho_chave)
    return contexto


class LimitadorTaxa:
    """Espacamento minimo entre requisicoes de uma conta."""

    def __init__(
        self,
        requisicoes_por_segundo: float | None,
        *,
        relogio: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._intervalo = (
            1.0 / requisicoes_por_segundo if requisicoes_por_segundo else 0
        )
        self._proximo = 0.0
        self._relogio = relogio
        self._dormir = dormir
        self._lock = asyncio.Lock()

    async def aguardar(self) -> None:
        if not self._intervalo:
            return
        async with self._lock:
            agora = self._relogio()
            inicio = max(agora, self._proximo)
            self._proximo = inicio + self._intervalo
        if inicio > agora:
            await self._dormir(inicio - agora)


@dataclass(frozen=True)
class PoliticaRetry:
    tentativas: int = 4
    base_s: float = 0.5
    maximo_s: float = 8.0
    jitter: float = 0.1

    def atraso(self, tentativa: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(retry_after, self.maximo_s)
        espera = min(self.maximo_s, self.base_s * 2**tentativa)
        return espera * (1 + random.random() * self.jitter)


def erro_de_resposta(resp: httpx.Response) -> ErroBrbank:
    """Traduz status HTTP de falha para o erro canonico."""
    status = resp.status_code
    comum = {"status_http": status, "resposta_bruta": resp.text}
    if status == 429 or status >= 500:
        return Indisponivel(f"banco respondeu HTTP {status}", **comum)
    if status == 409:
        return Duplicado("titulo ja existe no banco", **comum)
    return Validacao(f"banco recusou a requisicao (HTTP {status})", **comum)


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


class Transporte:
    """Cliente HTTP de uma conta. Um por (banco, conta)."""

    def __init__(
        self,
        *,
        banco: str,
        conta: str,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        ssl_context: ssl.SSLContext | None = None,
        autenticador: Autenticador | None = None,
        envelope: Envelope | None = None,
        limitador: LimitadorTaxa | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        timeout_s: float = 30.0,
    ) -> None:
        self.banco = banco
        self.conta = conta
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            http2=True,
            timeout=timeout_s,
            verify=ssl_context if ssl_context is not None else True,
        )
        self._auth = autenticador
        self._envelope = envelope or EnvelopeNulo()
        self._limitador = limitador or LimitadorTaxa(None)
        self._retry = retry or PoliticaRetry()
        self._dormir = dormir

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "Transporte":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def requisitar(
        self,
        metodo: str,
        caminho: str,
        *,
        operacao: str,
        escopo: str | None = None,
        json: Any = None,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        url = f"{self._base}/{caminho.lstrip('/')}"
        corpo = None
        base = {"Accept": "application/json"}
        if json is not None:
            corpo = self._envelope.cifrar(jsonlib.dumps(json).encode())
            base["Content-Type"] = "application/json"
        base.update(cabecalhos or {})

        tentativa = 0
        renovou_token = False
        while True:
            await self._limitador.aguardar()
            headers = dict(base)
            if self._auth:
                headers.update(await self._auth.cabecalhos(escopo, self._client))
            inicio = time.perf_counter()
            retry_after = None
            try:
                resp = await self._client.request(
                    metodo, url, content=corpo, headers=headers
                )
            except httpx.TransportError as exc:
                erro: ErroBrbank = Indisponivel(
                    f"erro de transporte: {type(exc).__name__}"
                )
                self._registrar(operacao, inicio, None, erro)
            else:
                if resp.status_code < 400:
                    self._registrar(operacao, inicio, resp.status_code, None)
                    return self._decifrar(resp)
                erro = erro_de_resposta(resp)
                self._registrar(operacao, inicio, resp.status_code, erro)
                if resp.status_code == 401 and self._auth and not renovou_token:
                    renovou_token = True
                    await self._auth.invalidar(escopo)
                    continue
                retry_after = _retry_after(resp)
            if not erro.retryable or tentativa + 1 >= self._retry.tentativas:
                raise erro
            await self._dormir(self._retry.atraso(tentativa, retry_after))
            tentativa += 1

    def _decifrar(self, resp: httpx.Response) -> httpx.Response:
        if not self._envelope.ativo:
            return resp
        return httpx.Response(
            resp.status_code,
            headers=resp.headers,
            content=self._envelope.decifrar(resp.content),
            request=resp.request,
        )

    def _registrar(
        self,
        operacao: str,
        inicio: float,
        status: int | None,
        erro: ErroBrbank | None,
    ) -> None:
        log.info(
            "banco=%s conta=%s operacao=%s latencia_ms=%d http=%s canonico=%s",
            self.banco,
            self.conta,
            operacao,
            (time.perf_counter() - inicio) * 1000,
            status,
            type(erro).__name__ if erro else "OK",
        )
