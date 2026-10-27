"""Armazem de token entre processos: fluxo, trava e corrida de renovacao."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from multiprocessing import get_context
from urllib.parse import parse_qs

import httpx
import pytest

from brbank import ArmazemToken, Indisponivel, TokenPersistido
from brbank.adapters.btg import BtgAdapter, BtgConfig
from brbank.auth import OAuth2ClientCredentials
from brbank.simulador import Simulador
from brbank.simulador.dados import CNPJ_DEMO
from tests.conftest import CNPJ_VALIDO
from tests.dda_util import ArmazemArquivo, admin, trabalhar

CHAVE = ("c1", None)


class ArmazemMemoria:
    """Armazem de teste em memoria, com registro do que aconteceu e da ordem."""

    def __init__(self, ao_entrar=None) -> None:
        self.tokens: dict = {}
        self.eventos: list[str] = []
        self._trava = threading.RLock()
        self._ao_entrar = ao_entrar

    def ler(self, chave):
        self.eventos.append("ler")
        return self.tokens.get(chave)

    def gravar(self, chave, token):
        self.eventos.append("gravar")
        self.tokens[chave] = token

    @contextmanager
    def trava(self, chave):
        with self._trava:
            self.eventos.append("entrou")
            if self._ao_entrar:
                self._ao_entrar(self)
            try:
                yield
            finally:
                self.eventos.append("saiu")


class Banco:
    def __init__(self, falhar: bool = False) -> None:
        self.pedidos = 0
        self.formularios: list[dict] = []
        self.falhar = falhar

    def tratar(self, request: httpx.Request) -> httpx.Response:
        self.pedidos += 1
        self.formularios.append(
            {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        )
        if self.falhar:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"access_token": f"tok-{self.pedidos}", "expires_in": 3600}
        )

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))


def _auth(armazem=None, **extra) -> OAuth2ClientCredentials:
    return OAuth2ClientCredentials(
        client_id="a",
        client_secret="b",
        conta="c1",
        url_token="http://banco.test/token",
        armazem=armazem,
        **extra,
    )


def test_protocolo_e_repr():
    assert isinstance(ArmazemMemoria(), ArmazemToken)
    assert "segredo" not in repr(TokenPersistido("segredo", 1.0))
    with pytest.raises(AttributeError):
        TokenPersistido("a", 1.0).valor = "b"  # type: ignore[misc]


def test_relogio_depende_do_armazem():
    assert _auth()._relogio is time.monotonic
    assert _auth(ArmazemMemoria())._relogio is time.time
    assert _auth(ArmazemMemoria(), relogio=lambda: 7.0)._relogio() == 7.0


async def test_token_vai_para_o_armazem_e_outro_processo_reaproveita():
    armazem, banco = ArmazemMemoria(), Banco()
    async with banco.cliente() as client:
        assert (await _auth(armazem).cabecalhos(None, client))[
            "Authorization"
        ] == "Bearer tok-1"
        persistido = armazem.tokens[CHAVE]
        assert persistido.valor == "tok-1"
        assert persistido.expira_em > time.time() + 3000
        # "outro processo": instancia nova, memoria vazia, mesmo armazem
        outra = _auth(armazem)
        assert (await outra.cabecalhos(None, client))["Authorization"] == "Bearer tok-1"
        assert (await outra.cabecalhos(None, client))["Authorization"] == "Bearer tok-1"
    assert banco.pedidos == 1


async def test_token_expirado_no_armazem_renova_e_regrava():
    armazem, banco = ArmazemMemoria(), Banco()
    armazem.tokens[CHAVE] = TokenPersistido("velho", time.time() - 1)
    async with banco.cliente() as client:
        assert (await _auth(armazem).cabecalhos(None, client))[
            "Authorization"
        ] == "Bearer tok-1"
    assert armazem.tokens[CHAVE].valor == "tok-1"


async def test_depois_de_pegar_a_trava_le_de_novo_antes_de_pedir():
    """Outro processo renovou enquanto esperavamos a trava: nao pedimos de novo."""

    def outro_renovou(armazem: ArmazemMemoria) -> None:
        armazem.tokens[CHAVE] = TokenPersistido("do-outro", time.time() + 3600)

    armazem, banco = ArmazemMemoria(outro_renovou), Banco()
    async with banco.cliente() as client:
        resultado = await _auth(armazem).cabecalhos(None, client)
    assert resultado["Authorization"] == "Bearer do-outro"
    assert banco.pedidos == 0
    assert "gravar" not in armazem.eventos
    assert armazem.eventos[-1] == "saiu"


async def test_trava_e_solta_quando_o_pedido_falha():
    armazem, banco = ArmazemMemoria(), Banco(falhar=True)
    async with banco.cliente() as client:
        with pytest.raises(Indisponivel):
            await _auth(armazem).cabecalhos(None, client)
    assert armazem.eventos.count("entrou") == armazem.eventos.count("saiu") == 1
    assert "gravar" not in armazem.eventos
    assert armazem.tokens == {}
    # e a trava do armazem esta livre para o proximo
    assert armazem._trava.acquire(blocking=False)
    armazem._trava.release()


async def test_chamadas_simultaneas_na_mesma_instancia_pedem_uma_vez():
    armazem, banco = ArmazemMemoria(), Banco()
    auth = _auth(armazem)
    async with banco.cliente() as client:
        respostas = await asyncio.gather(
            *(auth.cabecalhos(None, client) for _ in range(10))
        )
    assert {r["Authorization"] for r in respostas} == {"Bearer tok-1"}
    assert banco.pedidos == 1


async def test_token_recusado_pelo_banco_nao_volta_do_armazem():
    armazem, banco = ArmazemMemoria(), Banco()
    auth = _auth(armazem)
    async with banco.cliente() as client:
        await auth.cabecalhos(None, client)
        await auth.invalidar(None)  # o banco respondeu 401
        assert (await auth.cabecalhos(None, client))["Authorization"] == "Bearer tok-2"
    assert armazem.tokens[CHAVE].valor == "tok-2"


async def test_refresh_token_e_lido_dentro_da_trava_e_vai_no_pedido():
    armazem, banco = ArmazemMemoria(), Banco()

    def ler_refresh() -> str:
        armazem.eventos.append("ler_refresh")
        return "rt-do-banco-de-dados"

    config = BtgConfig(
        conta="c1",
        client_id="a",
        client_secret="b",
        refresh_token="rt-velho-em-memoria",
        cnpj_empresa=CNPJ_VALIDO,
        agencia="1",
        conta_corrente="1",
        url_token_override="http://banco.test/token",
        armazem=armazem,
        ler_refresh_token=ler_refresh,
    )

    def tratar(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return banco.tratar(request)
        return httpx.Response(200, json={"data": {"status": "ENABLED"}})

    adapter = BtgAdapter(
        config, client=httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    )
    await adapter.ativar_dda(CNPJ_VALIDO)
    assert banco.formularios[0]["refresh_token"] == "rt-do-banco-de-dados"
    eventos = armazem.eventos
    assert (
        eventos.index("entrou") < eventos.index("ler_refresh") < eventos.index("gravar")
    )
    assert eventos.index("gravar") < eventos.index("saiu")
    await adapter.aclose()


# --- corrida de renovacao com rotacao de refresh token ------------------------------


def _preparar(pasta, sim: Simulador) -> tuple:
    ArmazemArquivo(pasta).gravar_refresh(sim.estado.refresh_inicial)
    return (sim.url, sim.estado.client_id, sim.estado.client_secret, str(pasta))


def test_armazem_em_arquivo_sobrevive_a_outro_adapter(tmp_path):
    with Simulador(
        hoje=date(2026, 10, 26), semente="demo", rotacionar_refresh=True
    ) as sim:
        args = _preparar(tmp_path, sim)
        assert trabalhar(args) == "ok:12"
        assert trabalhar(args) == "ok:12"
        assert trabalhar(args) == "ok:12"
        estado = admin(sim, "GET", "/estado")
        # tres adapters de vida curta, uma unica renovacao
        assert estado["renovacoes_token"] == 1
        assert ArmazemArquivo(tmp_path).ler_refresh() == "sim-refresh-1"


def test_threads_renovando_juntas_nao_perdem_o_refresh_token(tmp_path):
    with Simulador(
        hoje=date(2026, 10, 26), semente="demo", rotacionar_refresh=True
    ) as sim:
        args = _preparar(tmp_path, sim)
        assert trabalhar(args) == "ok:12"
        for rodada in range(1, 4):
            admin(sim, "POST", "/revogar-tokens")
            with ThreadPoolExecutor(max_workers=8) as pool:
                resultados = list(pool.map(trabalhar, [args] * 8))
            assert resultados == ["ok:12"] * 8
            # oito consultas simultaneas com o token morto: UMA renovacao so
            assert admin(sim, "GET", "/estado")["renovacoes_token"] == 1 + rodada
        assert ArmazemArquivo(tmp_path).ler_refresh() == "sim-refresh-4"


def test_processos_renovando_juntos_nao_perdem_o_refresh_token(tmp_path):
    pytest.importorskip("fcntl")
    with Simulador(
        hoje=date(2026, 10, 26), semente="demo", rotacionar_refresh=True
    ) as sim:
        args = _preparar(tmp_path, sim)
        assert trabalhar(args) == "ok:12"
        admin(sim, "POST", "/revogar-tokens")
        with get_context("spawn").Pool(4) as pool:
            resultados = pool.map(trabalhar, [args] * 4)
        assert resultados == ["ok:12"] * 4
        assert admin(sim, "GET", "/estado")["renovacoes_token"] == 2
        assert ArmazemArquivo(tmp_path).ler_refresh() == "sim-refresh-2"
        assert CNPJ_DEMO  # o simulador atendeu o CNPJ da semente


def test_sem_a_trava_a_rotacao_derrubaria_o_refresh_token(tmp_path):
    """Contraprova: o mesmo cenario sem armazem perde o refresh token rotacionado."""
    with Simulador(
        hoje=date(2026, 10, 26), semente="demo", rotacionar_refresh=True
    ) as sim:

        def sem_armazem(_: int) -> str:
            config = BtgConfig(
                conta="sim",
                client_id=sim.estado.client_id,
                client_secret=sim.estado.client_secret,
                refresh_token=sim.estado.refresh_inicial,
                cnpj_empresa=CNPJ_DEMO,
                agencia="1",
                conta_corrente="1",
                url_base_override=sim.url,
                url_token_override=f"{sim.url}/oauth2/token",
            )
            adapter = BtgAdapter(config)

            async def rodar() -> str:
                try:
                    return f"ok:{(await adapter.resumo_debitos(CNPJ_DEMO)).quantidade}"
                except Exception as exc:
                    return f"erro:{type(exc).__name__}"
                finally:
                    await adapter.aclose()

            return asyncio.run(rodar())

        with ThreadPoolExecutor(max_workers=6) as pool:
            resultados = list(pool.map(sem_armazem, range(6)))
        # cada adapter parte do mesmo refresh token: so o primeiro a chegar vale
        assert resultados.count("ok:12") < 6
