from datetime import date
from decimal import Decimal

import httpx
import pytest

from brbank import (
    AdapterExtrato,
    ContaBancaria,
    Sinal,
    TipoSaldo,
    Validacao,
)
from brbank.adapters.bb_extrato import BancoDoBrasilExtratoAdapter, BBExtratoConfig

CHAVE_DEV = "chave-dev-falsa"
CONTA = ContaBancaria(agencia="0452", conta="123873", digito="0")
HOJE = date(2026, 10, 28)
CAMPO_CONTRAPARTIDA = (
    "numeroCadastroPessoaFisicaCadastroNacPessoasJuridicasContrapartida"
)


def linha(**campos):
    base = {
        "indicadorTipoLancamento": "1",
        "dataLancamento": 9102026,
        "dataMovimento": 9102026,
        "codigoAgenciaOrigem": 452,
        "numeroLote": 14000,
        "numeroDocumento": 123,
        "codigoHistorico": 830,
        "textoDescricaoSubHistorico": "PIX RECEBIDO",
        "codigoSubHistorico": 1,
        "valorLancamento": 150.5,
        "indicadorSinalLancamento": "C",
        "textoInformacaoComplementar": "fulano",
        CAMPO_CONTRAPARTIDA: "95127446000198",
        "codigoBancoContrapartida": 1,
        "codigoAgenciaContrapartida": 551,
        "numeroContaContrapartida": "43814",
        "textoIdentificadorUnicoTransacao": "",
    }
    base.update(campos)
    return base


class Falso:
    def __init__(self, paginas):
        self.paginas = paginas
        self.pedidos: list[httpx.Request] = []

    def tratar(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
        self.pedidos.append(request)
        pagina = int(request.url.params.get("numeroPaginaSolicitacao", "1"))
        corpo = self.paginas.get(pagina)
        if corpo is None:
            return httpx.Response(404, json={"erros": [{"codigo": "404"}]})
        return httpx.Response(200, json=corpo)


def adapter(falso, **extra):
    config = BBExtratoConfig(
        conta="c1",
        client_id="id",
        client_secret="segredo-xyz",
        chave_desenvolvedor=CHAVE_DEV,
        ambiente="sandbox",
        mci_teste="704950857",
        **extra,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(falso.tratar))
    return BancoDoBrasilExtratoAdapter(config, client=client, hoje=lambda: HOJE)


def test_exige_certificado_e_nao_vaza_segredo():
    config = BBExtratoConfig(
        conta="c1",
        client_id="id",
        client_secret="segredo-xyz",
        chave_desenvolvedor=CHAVE_DEV,
        ambiente="sandbox",
    )
    assert "segredo-xyz" not in repr(config) and CHAVE_DEV not in repr(config)
    assert issubclass(BancoDoBrasilExtratoAdapter, AdapterExtrato)
    with pytest.raises(Validacao, match="mTLS"):
        BancoDoBrasilExtratoAdapter(config)
    with pytest.raises(Validacao, match="homologacao"):
        BBExtratoConfig(
            conta="c1",
            client_id="id",
            client_secret="s",
            chave_desenvolvedor="k",
            mci_teste="1",
        )


async def test_separa_saldo_de_lancamento_e_pagina():
    falso = Falso(
        {
            1: {
                "numeroPaginaProximo": 2,
                "listaLancamento": [
                    linha(
                        codigoHistorico=0,
                        valorLancamento=1000,
                        indicadorSinalLancamento="C",
                    ),
                    linha(),
                    linha(
                        numeroDocumento=124,
                        valorLancamento=20,
                        indicadorSinalLancamento="D",
                    ),
                ],
            },
            2: {
                "numeroPaginaProximo": 0,
                "listaLancamento": [
                    linha(
                        codigoHistorico=999,
                        valorLancamento=30,
                        indicadorSinalLancamento="D",
                    )
                ],
            },
        }
    )
    ad = adapter(falso)
    primeira = await ad.listar_lancamentos(CONTA, date(2026, 10, 1), date(2026, 10, 9))

    pedido = falso.pedidos[0]
    assert pedido.url.path.endswith("/conta-corrente/agencia/452/conta/123873")
    assert pedido.url.params["dataInicioSolicitacao"] == "1102026"
    assert pedido.url.params["dataFimSolicitacao"] == "9102026"
    assert pedido.url.params["quantidadeRegistroPaginaSolicitacao"] == "120"
    assert pedido.headers["x-br-com-bb-ipa-mciteste"] == "704950857"
    assert "gw-dev-app-key" not in str(pedido.headers).lower()

    assert [s.tipo for s in primeira.saldos] == [TipoSaldo.ANTERIOR]
    credito, debito = primeira.lancamentos
    assert credito.sinal is Sinal.CREDITO and credito.valor == Decimal("150.50")
    assert debito.valor_assinado == Decimal("-20.00")
    assert credito.descricao == "PIX RECEBIDO"
    assert credito.data == date(2026, 10, 9)
    assert credito.id_lancamento != debito.id_lancamento
    assert primeira.cursor_proximo == "2"

    segunda = await ad.listar_lancamentos(
        CONTA, date(2026, 10, 1), date(2026, 10, 9), primeira.cursor_proximo
    )
    assert segunda.lancamentos == ()
    assert segunda.saldos[0].tipo is TipoSaldo.FINAL
    assert segunda.saldos[0].valor == Decimal("-30")
    assert segunda.cursor_proximo is None


async def test_id_do_lancamento_nao_muda_quando_o_banco_gera_o_id_da_transacao():
    antes = Falso({1: {"listaLancamento": [linha()]}})
    depois = Falso(
        {1: {"listaLancamento": [linha(textoIdentificadorUnicoTransacao="E1")]}}
    )
    periodo = (CONTA, date(2026, 10, 9), date(2026, 10, 9))
    (a,) = (await adapter(antes).listar_lancamentos(*periodo)).lancamentos
    (b,) = (await adapter(depois).listar_lancamentos(*periodo)).lancamentos
    assert a.id_lancamento == b.id_lancamento
    assert (a.id_transacao, b.id_transacao) == (None, "E1")


async def test_sem_lancamentos_e_pagina_vazia():
    pagina = await adapter(Falso({})).listar_lancamentos(
        CONTA, date(2026, 10, 9), date(2026, 10, 9)
    )
    assert pagina.lancamentos == () and pagina.cursor_proximo is None


@pytest.mark.parametrize(
    ("desde", "ate"),
    [
        (date(2026, 10, 9), date(2026, 10, 1)),
        (date(2026, 9, 1), date(2026, 10, 9)),
        (date(2026, 10, 27), date(2026, 10, 29)),
    ],
)
async def test_periodo_invalido_nao_chama_a_rede(desde, ate):
    falso = Falso({})
    with pytest.raises(Validacao):
        await adapter(falso).listar_lancamentos(CONTA, desde, ate)
    assert falso.pedidos == []


async def test_lancamento_sem_sinal_e_recusado():
    falso = Falso({1: {"listaLancamento": [linha(indicadorSinalLancamento="")]}})
    with pytest.raises(Validacao, match="sinal"):
        await adapter(falso).listar_lancamentos(
            CONTA, date(2026, 10, 9), date(2026, 10, 9)
        )
