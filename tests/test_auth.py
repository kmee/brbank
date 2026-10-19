import asyncio

import httpx

from brbank.auth import OAuth2ClientCredentials


def montar(relogio):
    pedidos = []

    async def tratar(request):
        await asyncio.sleep(0)
        corpo = dict(p.split("=") for p in request.content.decode().split("&"))
        pedidos.append(corpo)
        return httpx.Response(
            200, json={"access_token": f"t{len(pedidos)}", "expires_in": 100}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    auth = OAuth2ClientCredentials(
        conta="c",
        url_token="https://banco.test/token",
        client_id="i",
        client_secret="SEGREDO-X",
        escopos={"leitura": "x.read"},
        margem_s=10,
        relogio=relogio,
    )
    return auth, client, pedidos


async def test_chamadas_paralelas_pedem_um_token_so():
    auth, client, pedidos = montar(lambda: 0.0)
    resultados = await asyncio.gather(
        *(auth.cabecalhos("leitura", client) for _ in range(20))
    )
    assert len(pedidos) == 1
    assert pedidos[0]["scope"] == "x.read"
    assert {r["Authorization"] for r in resultados} == {"Bearer t1"}


async def test_cache_por_escopo_e_expiracao():
    agora = [0.0]
    auth, client, pedidos = montar(lambda: agora[0])
    await auth.cabecalhos("leitura", client)
    await auth.cabecalhos("escrita", client)
    assert len(pedidos) == 2
    agora[0] = 89.0  # vida = 100 - 10 = 90
    await auth.cabecalhos("leitura", client)
    assert len(pedidos) == 2
    agora[0] = 91.0
    await auth.cabecalhos("leitura", client)
    assert len(pedidos) == 3


async def test_repr_nao_vaza_segredo():
    auth, _, _ = montar(lambda: 0.0)
    assert "SEGREDO-X" not in repr(auth)
