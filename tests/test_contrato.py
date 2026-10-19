"""Suite de contrato comum: registrar -> consultar -> baixar.

Todo adapter entra por uma fabrica em `FABRICAS`. Os passos que dependem de
capacidade sao pulados quando o adapter nao a declara.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from brbank import Capacidade, Duplicado, StatusCanonico, Validacao
from brbank.adapters.inter import InterAdapter, InterConfig
from brbank.adapters.pinbank import PinbankAdapter, PinbankConfig
from brbank.adapters.sicoob import SicoobAdapter, SicoobConfig
from tests.conftest import fazer_titulo
from tests.inter_falso import InterFalso
from tests.pinbank_falso import SENHA, PinbankFalso
from tests.sicoob_falso import SicoobFalso


@dataclass
class Fabrica:
    nome: str
    criar: Callable[[Any], tuple[Any, Any]]  # (dormir) -> (adapter, falso)
    liquidar: Callable[[Any, str], None]
    webhook: Callable[[str, str], bytes]


def _inter(dormir):
    falso = InterFalso()
    config = InterConfig(
        conta="conta-1", client_id="id", client_secret="segredo", conta_corrente="123"
    )
    return InterAdapter(config, client=falso.cliente(), dormir=dormir), falso


def _pinbank(dormir):
    falso = PinbankFalso()
    config = PinbankConfig(
        conta="conta-pb",
        usuario="user",
        senha=SENHA,
        codigo_canal=10,
        codigo_cliente=1000,
        origem_requisicao="7",
        ambiente="sandbox",
    )
    return PinbankAdapter(config, client=falso.cliente(), dormir=dormir), falso


def _sicoob(dormir):
    falso = SicoobFalso()
    config = SicoobConfig(
        conta="conta-sc",
        client_id="cid",
        numero_cliente=25546454,
        numero_conta_corrente="123456",
        codigo_especie_documento=2,
    )
    return SicoobAdapter(config, client=falso.cliente(), dormir=dormir), falso


FABRICAS = [
    Fabrica(
        nome="inter",
        criar=_inter,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: json.dumps(
            [{"codigoSolicitacao": id_banco, "situacao": situacao}]
        ).encode(),
    ),
    Fabrica(
        nome="pinbank",
        criar=_pinbank,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: b"[]",
    ),
    Fabrica(
        nome="sicoob",
        criar=_sicoob,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: b"[]",
    ),
]


@pytest.fixture(params=FABRICAS, ids=lambda f: f.nome)
def ambiente(request, dormir):
    fabrica = request.param
    adapter, falso = fabrica.criar(dormir)
    return fabrica, adapter, falso


async def test_registrar_e_consultar(ambiente):
    _, adapter, _ = ambiente
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.banco == adapter.banco
    assert registrado.id_banco

    consultado = await adapter.consultar_titulo(registrado.id_banco)
    esperados = {StatusCanonico.REGISTRADO}
    if adapter.suporta(Capacidade.EMISSAO_ASSINCRONA):
        esperados.add(StatusCanonico.PENDENTE_REGISTRO)
    assert consultado.status in esperados
    if adapter.suporta(Capacidade.HIBRIDO):
        assert consultado.pix_copia_cola
        assert consultado.linha_digitavel


async def test_baixar(ambiente):
    _, adapter, _ = ambiente
    if not adapter.suporta(Capacidade.BAIXA):
        pytest.skip("adapter sem BAIXA")
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.baixar_titulo(registrado.id_banco)
    assert baixado.status is StatusCanonico.BAIXADO
    assert (await adapter.consultar_titulo(registrado.id_banco)).status is (
        StatusCanonico.BAIXADO
    )


async def test_liquidacao_aparece_na_consulta(ambiente):
    fabrica, adapter, falso = ambiente
    registrado = await adapter.registrar_titulo(fazer_titulo())
    fabrica.liquidar(falso, registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago is not None


async def test_pdf(ambiente):
    _, adapter, _ = ambiente
    if not adapter.suporta(Capacidade.PDF):
        pytest.skip("adapter sem PDF")
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert (await adapter.obter_pdf(registrado.id_banco)).startswith(b"%PDF")


async def test_webhook(ambiente):
    fabrica, adapter, _ = ambiente
    if not adapter.suporta(Capacidade.WEBHOOK):
        pytest.skip("adapter sem WEBHOOK")
    eventos = adapter.parse_webhook(fabrica.webhook("cod-1", "PAGO"))
    assert [e.tipo for e in eventos] == [StatusCanonico.LIQUIDADO]
    assert eventos[0].chave_dedup == (adapter.banco, "cod-1", StatusCanonico.LIQUIDADO)


async def test_titulo_inexistente_e_validacao(ambiente):
    _, adapter, _ = ambiente
    with pytest.raises(Validacao) as erro:
        await adapter.consultar_titulo("nao-existe")
    assert erro.value.status_http == 404
    assert not erro.value.retryable


async def test_erros_canonicos_herdam_da_base():
    assert not issubclass(Duplicado, Validacao)
