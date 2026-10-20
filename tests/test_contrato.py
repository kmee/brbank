"""Suite de contrato comum: registrar -> consultar -> baixar.

Todo adapter entra por uma fabrica em `FABRICAS`. Os passos que dependem de
capacidade sao pulados quando o adapter nao a declara.
"""

import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from brbank import Capacidade, Duplicado, StatusCanonico, Validacao
from brbank.adapters.banrisul import BanrisulAdapter, BanrisulConfig
from brbank.adapters.bb import BancoDoBrasilAdapter, BBConfig
from brbank.adapters.c6 import C6Adapter, C6Config
from brbank.adapters.inter import InterAdapter, InterConfig
from brbank.adapters.itau import ItauAdapter, ItauConfig
from brbank.adapters.pinbank import PinbankAdapter, PinbankConfig
from brbank.adapters.pix_bacen import PERFIS, PixBacenAdapter, PixBacenConfig
from brbank.adapters.santander import SantanderAdapter, SantanderConfig
from brbank.adapters.safra import SafraAdapter, SafraConfig
from brbank.adapters.sicoob import SicoobAdapter, SicoobConfig
from brbank.adapters.sicredi import SicrediAdapter, SicrediConfig
from brbank.adapters.unicred import UnicredAdapter, UnicredConfig
from tests import unicred_falso
from tests.banrisul_falso import BENEFICIARIO, BanrisulFalso
from tests.bb_falso import CHAVE_DEV, BBFalso
from tests.c6_falso import C6Falso
from tests.conftest import fazer_titulo
from tests.inter_falso import InterFalso
from tests.itau_falso import ItauFalso
from tests.pinbank_falso import SENHA, PinbankFalso
from tests.pix_falso import PixFalso
from tests.santander_falso import WORKSPACE, SantanderFalso
from tests.safra_falso import SafraFalso
from tests.sicoob_falso import SicoobFalso
from tests.sicredi_falso import CHAVE_API, SicrediFalso


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


def _itau(dormir):
    falso = ItauFalso()
    sequencia = itertools.count(1)
    config = ItauConfig(
        conta="conta-itau",
        client_id="id",
        client_secret="segredo",
        agencia="1500",
        conta_corrente="5000",
        digito_conta="5",
        carteira=109,
        chave_pix="chave@kmee.com.br",
        gerar_nosso_numero=lambda _titulo: next(sequencia),
    )
    return ItauAdapter(config, client=falso.cliente(), dormir=dormir), falso


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


def _banrisul(dormir):
    falso = BanrisulFalso()
    config = BanrisulConfig(
        conta="conta-banrisul",
        client_id="id",
        client_secret="segredo",
        codigo_beneficiario=BENEFICIARIO,
        especie="02",
        ambiente="sandbox",
    )
    return BanrisulAdapter(config, client=falso.cliente(), dormir=dormir), falso


def _bb(dormir):
    falso = BBFalso()
    sequencia = itertools.count(1)
    config = BBConfig(
        conta="conta-bb",
        client_id="id",
        client_secret="segredo",
        chave_desenvolvedor=CHAVE_DEV,
        convenio="1234567",
        carteira=17,
        variacao_carteira=35,
        ambiente="sandbox",
        gerar_nosso_numero=lambda _titulo: next(sequencia),
    )
    return BancoDoBrasilAdapter(config, client=falso.cliente(), dormir=dormir), falso


def _c6(dormir):
    falso = C6Falso()
    config = C6Config(
        conta="conta-c6",
        client_id="id",
        client_secret="segredo",
        chave_pix="123e4567-e89b-12d3-a456-426614174000",
        ambiente="sandbox",
    )
    return C6Adapter(config, client=falso.cliente(), dormir=dormir), falso


def _webhook_c6(id_banco, situacao):
    info = {"external_reference_id": id_banco, "amount": "150.00"}
    return json.dumps(
        {"service": "BANK_SLIP_PIX", "status": "PAID", "information": json.dumps(info)}
    ).encode()


def _sicredi(dormir):
    falso = SicrediFalso("/sb")
    config = SicrediConfig(
        conta="conta-sc",
        usuario="123456789",
        senha="senha",
        chave_api=CHAVE_API,
        codigo_beneficiario="12345",
        cooperativa="0101",
        posto="02",
        ambiente="sandbox",
    )
    return SicrediAdapter(config, client=falso.cliente(), dormir=dormir), falso


def _santander(dormir):
    falso = SantanderFalso()
    sequencia = itertools.count(1)
    config = SantanderConfig(
        conta="conta-st",
        client_id="cid",
        client_secret="segredo",
        workspace_id=WORKSPACE,
        convenio="3567206",
        chave_pix="11222333000181",
        tipo_chave_pix="CNPJ",
        gerar_nosso_numero=lambda _titulo: next(sequencia),
    )
    return SantanderAdapter(config, client=falso.cliente(), dormir=dormir), falso
def _safra(dormir):
    falso = SafraFalso()
    sequencia = itertools.count(1)
    config = SafraConfig(
        conta="conta-sf",
        client_id="cid",
        usuario="usuario",
        senha="senha",
        agencia="0290",
        conta_corrente="0058604",
        especie="01",
        ambiente="sandbox",
        gerar_nosso_numero=lambda _titulo: next(sequencia),
    )
    return SafraAdapter(config, client=falso.cliente(), dormir=dormir), falso
def _unicred(dormir):
    falso = unicred_falso.UnicredFalso("/homolog")
    config = UnicredConfig(
        conta="conta-un",
        api_key=unicred_falso.API_KEY,
        usuario="usuario",
        senha="senha",
        cooperativa="0001",
        id_beneficiario=unicred_falso.BENEFICIARIO,
        variacao_carteira=43,
        ambiente="sandbox",
        chave_autenticacao_webhook=unicred_falso.CHAVE_AUT,
        chave_criptografia_webhook=unicred_falso.CHAVE_CRIPTO,
    )
    return UnicredAdapter(config, client=falso.cliente(), dormir=dormir), falso


def _pix(nome: str, **extra):
    def criar(dormir):
        falso = PixFalso(PERFIS[nome])
        config = PixBacenConfig(
            perfil=nome,
            conta=f"conta-{nome}",
            chave_pix="chave@kmee.com.br",
            client_id="id",
            client_secret="segredo",
            **extra,
        )
        return PixBacenAdapter(config, client=falso.cliente(), dormir=dormir), falso

    return criar


def _webhook_pix(id_banco, situacao):
    pix = {"endToEndId": "E1", "txid": id_banco, "valor": "150.00"}
    return json.dumps({"pix": [pix]}).encode()


FABRICAS = [
    Fabrica(
        nome="banrisul",
        criar=_banrisul,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: b"[]",
    ),
    Fabrica(
        nome="bb",
        criar=_bb,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: b"[]",
    ),
    Fabrica(
        nome="c6",
        criar=_c6,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=_webhook_c6,
    ),
    Fabrica(
        nome="inter",
        criar=_inter,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: json.dumps(
            [{"codigoSolicitacao": id_banco, "situacao": situacao}]
        ).encode(),
    ),
    Fabrica(
        nome="itau",
        criar=_itau,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: b"[]",
    ),
    Fabrica(
        nome="pinbank",
        criar=_pinbank,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: b"[]",
    ),
    Fabrica(
        nome="safra",
        criar=_safra,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: b"[]",
    ),
    Fabrica(
        nome="sicoob",
        criar=_sicoob,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: b"[]",
    ),
    Fabrica(
        nome="sicredi",
        criar=_sicredi,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: json.dumps(
            {"nossoNumero": id_banco, "movimento": "LIQUIDACAO_PIX"}
        ).encode(),
    ),
    Fabrica(
        nome="santander",
        criar=_santander,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: b"[]",
    ),
    Fabrica(
        nome="unicred",
        criar=_unicred,
        liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
        webhook=lambda id_banco, situacao: unicred_falso.webhook_liquidacao(id_banco),
    ),
    *(
        Fabrica(
            nome=f"pix_{nome}",
            criar=_pix(nome, **extra),
            liquidar=lambda falso, id_banco: falso.liquidar(id_banco),
            webhook=_webhook_pix,
        )
        for nome, extra in (
            ("inter", {}),
            ("sicoob", {}),
            ("bb", {"app_key": "k1"}),
        )
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
