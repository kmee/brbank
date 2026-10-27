"""Apoio dos testes de DDA: config apontada para o simulador, armazem de token em
arquivo (com `fcntl`), receptor de webhook e o trabalhador de varios processos."""

import asyncio
import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from brbank.adapters.btg import BtgAdapter, BtgConfig
from brbank.auth import TokenPersistido
from brbank.simulador.dados import CNPJ_DEMO
from brbank.simulador.servidor import Simulador

Chave = tuple[str, str | None]


def config_para(sim: Simulador, **extra: Any) -> BtgConfig:
    base: dict[str, Any] = {
        "conta": "sim",
        "client_id": sim.estado.client_id,
        "client_secret": sim.estado.client_secret,
        "refresh_token": sim.estado.refresh_inicial,
        "cnpj_empresa": CNPJ_DEMO,
        "agencia": "1",
        "conta_corrente": "123456",
        "url_base_override": sim.url,
        "url_token_override": f"{sim.url}/oauth2/token",
    }
    return BtgConfig(**{**base, **extra})


def admin(sim: Simulador, metodo: str, caminho: str, corpo: dict | None = None) -> Any:
    resp = httpx.request(metodo, f"{sim.url}/_sim{caminho}", json=corpo, timeout=10)
    resp.raise_for_status()
    return resp.json()


class ArmazemArquivo:
    """`ArmazemToken` em arquivo com trava `flock`: vale entre threads e processos.

    Guarda tambem o refresh token (`ler_refresh`/`gravar_refresh`), como o Odoo
    guardaria na conexao.
    """

    def __init__(self, pasta: Path) -> None:
        self.pasta = Path(pasta)
        self.lido = 0

    def _arquivo(self, chave: Chave, sufixo: str) -> Path:
        return self.pasta / f"{chave[0]}-{chave[1] or 'sem-escopo'}.{sufixo}"

    def ler(self, chave: Chave) -> TokenPersistido | None:
        self.lido += 1
        try:
            dados = json.loads(self._arquivo(chave, "json").read_text())
        except (FileNotFoundError, ValueError):
            return None
        return TokenPersistido(dados["valor"], dados["expira_em"])

    def gravar(self, chave: Chave, token: TokenPersistido) -> None:
        temporario = self._arquivo(chave, f"tmp{os.getpid()}-{threading.get_ident()}")
        temporario.write_text(
            json.dumps({"valor": token.valor, "expira_em": token.expira_em})
        )
        os.replace(temporario, self._arquivo(chave, "json"))

    @contextmanager
    def trava(self, chave: Chave) -> Iterator[None]:
        with open(self._arquivo(chave, "lock"), "a+") as arquivo:
            fcntl.flock(arquivo, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(arquivo, fcntl.LOCK_UN)

    def ler_refresh(self) -> str:
        return (self.pasta / "refresh.txt").read_text().strip()

    def gravar_refresh(self, valor: str) -> None:
        temporario = self.pasta / f"refresh.tmp{os.getpid()}-{threading.get_ident()}"
        temporario.write_text(valor)
        os.replace(temporario, self.pasta / "refresh.txt")


def adapter_com_armazem(
    url: str, client_id: str, client_secret: str, pasta: str, **extra: Any
) -> BtgAdapter:
    armazem = ArmazemArquivo(Path(pasta))
    config = BtgConfig(
        conta="sim",
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=armazem.ler_refresh(),
        cnpj_empresa=CNPJ_DEMO,
        agencia="1",
        conta_corrente="123456",
        url_base_override=url,
        url_token_override=f"{url}/oauth2/token",
        armazem=armazem,
        ler_refresh_token=armazem.ler_refresh,
        ao_renovar_refresh_token=armazem.gravar_refresh,
        **extra,
    )
    return BtgAdapter(config)


async def _resumir(adapter: BtgAdapter) -> int:
    try:
        return (await adapter.resumo_debitos(CNPJ_DEMO)).quantidade
    finally:
        await adapter.aclose()


def trabalhar(args: tuple[str, str, str, str]) -> str:
    """Um processo (ou thread): adapter proprio, mesmo armazem, uma consulta."""
    url, client_id, client_secret, pasta = args
    try:
        adapter = adapter_com_armazem(url, client_id, client_secret, pasta)
        return f"ok:{asyncio.run(_resumir(adapter))}"
    except Exception as exc:  # o teste confere que ninguem falhou
        return f"erro:{type(exc).__name__}"


class Receptor:
    """Servidor que guarda os webhooks recebidos (o Odoo, nos e2e)."""

    def __init__(self, status: int = 200) -> None:
        self.recebidos: list[tuple[dict[str, str], bytes]] = []
        self.status = status
        receptor = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                corpo = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                receptor.recebidos.append((dict(self.headers), corpo))
                self.send_response(receptor.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                return

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._http.daemon_threads = True
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._http.server_address[1]}/webhook"

    def parar(self) -> None:
        self._http.shutdown()
        self._http.server_close()
