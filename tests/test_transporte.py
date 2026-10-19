import asyncio

import httpx
import pytest

from brbank import Duplicado, Indisponivel, Validacao
from brbank.auth import OAuth2ClientCredentials
from brbank.envelope import Envelope
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte


def transporte(tratar, dormir, **kw):
    return Transporte(
        banco="x",
        conta="c",
        base_url="https://banco.test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(tratar)),
        dormir=dormir,
        **kw,
    )


async def test_retry_so_para_retentaveis(dormir, dormidas):
    respostas = iter([429, 503, 200])
    chamadas = []

    def tratar(request):
        chamadas.append(1)
        return httpx.Response(next(respostas), json={"ok": True})

    t = transporte(tratar, dormir)
    resp = await t.requisitar("GET", "/a", operacao="op")
    assert resp.status_code == 200
    assert len(chamadas) == 3
    assert dormidas[1] > dormidas[0]  # backoff exponencial


async def test_400_nao_e_repetido(dormir):
    chamadas = []

    def tratar(request):
        chamadas.append(1)
        return httpx.Response(400, json={"nome": "Maria", "erro": "x"})

    with pytest.raises(Validacao) as erro:
        await transporte(tratar, dormir).requisitar("GET", "/a", operacao="op")
    assert len(chamadas) == 1
    assert erro.value.resposta_bruta["nome"] == "***"


async def test_409_vira_duplicado(dormir):
    def tratar(request):
        return httpx.Response(409, json={})

    with pytest.raises(Duplicado):
        await transporte(tratar, dormir).requisitar("POST", "/a", operacao="op")


async def test_erro_de_transporte_e_retentado(dormir):
    chamadas = []

    def tratar(request):
        chamadas.append(1)
        if len(chamadas) < 3:
            raise httpx.ConnectError("falhou", request=request)
        return httpx.Response(200)

    resp = await transporte(tratar, dormir).requisitar("GET", "/a", operacao="op")
    assert resp.status_code == 200 and len(chamadas) == 3


async def test_retry_esgotado(dormir):
    t = transporte(
        lambda r: httpx.Response(500),
        dormir,
        retry=PoliticaRetry(tentativas=2),
    )
    with pytest.raises(Indisponivel):
        await t.requisitar("GET", "/a", operacao="op")


async def test_retry_after_respeitado(dormir, dormidas):
    respostas = iter([429, 200])

    def tratar(request):
        return httpx.Response(next(respostas), headers={"Retry-After": "2"})

    await transporte(tratar, dormir).requisitar("GET", "/a", operacao="op")
    assert dormidas == [2.0]


async def test_limitador_espaca_requisicoes():
    esperas = []
    agora = [100.0]

    async def dormir(s):
        esperas.append(s)

    limitador = LimitadorTaxa(2, relogio=lambda: agora[0], dormir=dormir)
    await asyncio.gather(*(limitador.aguardar() for _ in range(3)))
    assert esperas == pytest.approx([0.5, 1.0])


async def test_401_renova_token_uma_vez(dormir):
    emitidos = []

    def tratar(request):
        if request.url.path == "/token":
            emitidos.append(1)
            return httpx.Response(
                200, json={"access_token": f"t{len(emitidos)}", "expires_in": 3600}
            )
        ok = request.headers["authorization"] == "Bearer t2"
        return httpx.Response(200 if ok else 401)

    auth = OAuth2ClientCredentials(
        conta="c",
        url_token="https://banco.test/token",
        client_id="i",
        client_secret="s",
    )
    resp = await transporte(tratar, dormir, autenticador=auth).requisitar(
        "GET", "/a", operacao="op", escopo="e"
    )
    assert resp.status_code == 200
    assert len(emitidos) == 2


class Inverte(Envelope):
    def cifrar(self, corpo):
        return corpo[::-1]

    def decifrar(self, corpo):
        return corpo[::-1]


async def test_envelope_cifra_e_decifra(dormir):
    vistos = []

    def tratar(request):
        vistos.append(request.content)
        return httpx.Response(200, content=b'{"b":2}'[::-1])

    t = transporte(tratar, dormir, envelope=Inverte())
    resp = await t.requisitar("POST", "/a", operacao="op", json={"a": 1})
    assert vistos == [b'{"a": 1}'[::-1]]
    assert resp.json() == {"b": 2}
