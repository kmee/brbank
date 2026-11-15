import asyncio

import httpx
import pytest

from brbank import Duplicado, Indisponivel, Validacao
from brbank.auth import OAuth2ClientCredentials
from brbank.envelope import Envelope
from brbank.transporte import (
    SEM_RETRY,
    LimitadorTaxa,
    PoliticaRetry,
    Transporte,
    erro_de_resposta,
)


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


async def test_retry_trocado_so_na_chamada(dormir):
    chamadas = []

    def tratar(request):
        chamadas.append(1)
        return httpx.Response(503, json={})

    t = transporte(tratar, dormir)
    with pytest.raises(Indisponivel):
        await t.requisitar("POST", "/pagar", operacao="op", retry=SEM_RETRY)
    assert len(chamadas) == 1
    with pytest.raises(Indisponivel):
        await t.requisitar("GET", "/consultar", operacao="op")
    assert len(chamadas) == 1 + PoliticaRetry().tentativas


# Corpos de recusa vistos nos sandboxes em 15/11/2026.
_BB_NOSSO_NUMERO = {
    "erros": [
        {
            "codigo": "4874915",
            "versao": "1",
            "mensagem": "Nosso Número já incluído anteriormente.",
            "providencia": "Informar outro Nosso Número.",
            "ocorrencia": "075103844484",
        }
    ]
}
_BRADESCO_VALIDACAO = {
    "codigo": "1",
    "mensagem": "Nao foi possivel processar as instrucoes contidas na requisicao",
    "errosValidacao": [
        {
            "nomeDoArquivo": "default.json",
            "erros": [
                f"O argumento campo{n} não foi enviado na requisição."
                for n in range(30)
            ],
        }
    ],
}


@pytest.mark.parametrize(
    ("corpo", "esperado"),
    [
        (
            _BB_NOSSO_NUMERO,
            "banco recusou a requisicao (HTTP 400): Nosso Número já incluído "
            "anteriormente.; Informar outro Nosso Número.",
        ),
        (
            {
                "status": "Error",
                "mensagem": "A Request enviada não atende aos cenários",
            },
            "banco recusou a requisicao (HTTP 400): A Request enviada não atende aos "
            "cenários",
        ),
        (
            {
                "title": "Cobranca invalida",
                "detail": "A requisicao nao respeita o schema.",
                "violacoes": [
                    {"razao": "valor.original ausente", "propriedade": "valor"}
                ],
            },
            "banco recusou a requisicao (HTTP 400): A requisicao nao respeita o "
            "schema.; valor.original ausente",
        ),
        ({"codigo": "99"}, "banco recusou a requisicao (HTTP 400)"),
        ("<html>erro</html>", "banco recusou a requisicao (HTTP 400)"),
    ],
)
def test_recusa_traz_o_motivo_do_banco(corpo, esperado):
    kw = {"json": corpo} if isinstance(corpo, dict) else {"text": corpo}
    erro = erro_de_resposta(httpx.Response(400, **kw))
    assert isinstance(erro, Validacao)
    assert str(erro) == esperado
    assert erro.args == (esperado,)


def test_motivo_longo_e_cortado():
    erro = erro_de_resposta(httpx.Response(422, json=_BRADESCO_VALIDACAO))
    motivo = str(erro).split(": ", 1)[1]
    assert motivo.startswith("Nao foi possivel processar")
    assert motivo.count(";") == 3  # a mensagem e 3 erros, nunca os 30
    assert len(motivo) <= 400


def test_motivo_nao_leva_segredo_ecoado():
    corpo = {
        "message": "invalid request: client_id=abc&client_secret=s3gr3d0 token: xyz"
    }
    texto = str(erro_de_resposta(httpx.Response(400, json=corpo)))
    assert "s3gr3d0" not in texto
    assert "abc" not in texto
    assert "xyz" not in texto
    assert "client_secret=***" in texto


def test_indisponivel_e_duplicado_mantem_a_mensagem():
    erro = erro_de_resposta(httpx.Response(503, json={"message": "fora"}))
    assert str(erro) == "banco respondeu HTTP 503"
    assert isinstance(erro_de_resposta(httpx.Response(409, json={})), Duplicado)
