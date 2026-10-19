import asyncio
import base64
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from brbank import (
    Capacidade,
    Encargos,
    Indisponivel,
    NaoSuportado,
    Orquestrador,
    StatusCanonico,
    Validacao,
    obter_classe,
)
from brbank.adapters.pinbank import PinbankAdapter, PinbankConfig
from brbank.auth import OAuth2Password
from brbank.envelope import EnvelopeAesCbc
from tests.conftest import fazer_titulo
from tests.pinbank_falso import SENHA, PinbankFalso


def config(**extra) -> PinbankConfig:
    base = {
        "conta": "c1",
        "usuario": "user",
        "senha": SENHA,
        "codigo_canal": 10,
        "codigo_cliente": 1000,
        "origem_requisicao": "7",
        "ambiente": "sandbox",
    }
    return PinbankConfig(**{**base, **extra})


@pytest.fixture
def falso():
    return PinbankFalso()


@pytest.fixture
def adapter(falso, dormir):
    return PinbankAdapter(config(), client=falso.cliente(), dormir=dormir)


def adapter_com(falso, dormir, resposta):
    """Adapter cujo servidor responde `resposta(request)` fora do token."""

    def tratar(request):
        if request.url.path.endswith("/token"):
            return falso.tratar(request)
        return resposta(request)

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    return PinbankAdapter(config(), client=cliente, dormir=dormir)


def cifrada(falso, corpo) -> httpx.Response:
    return httpx.Response(
        200, content=falso.envelope.cifrar(json.dumps(corpo).encode())
    )


def test_registrado_no_entry_point():
    assert obter_classe("pinbank") is PinbankAdapter


def test_capacidades_declaradas(adapter):
    assert adapter.suporta(Capacidade.BAIXA)
    assert adapter.suporta(Capacidade.EMISSAO_ASSINCRONA)
    for ausente in (Capacidade.WEBHOOK, Capacidade.PDF, Capacidade.HIBRIDO):
        assert not adapter.suporta(ausente)


async def test_payload_cifrado_e_cabecalhos(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo(valor="258.75"))
    dados = falso.ultimo_corpo["Data"]
    assert dados["Valor"] == 25875
    assert dados["DataVencimento"] == "20261210"
    assert dados["CodigoCanal"] == 10
    assert dados["CodigoCliente"] == 1000
    assert dados["DadosSacado"]["CpfCnpj"] == 52998224725
    assert dados["DadosSacado"]["Cep"] == 30110000
    assert dados["Email"] == "maria@example.com"
    assert dados["RetornarBase64"] is True
    # O que trafega nao tem nenhum dado em claro.
    assert b"Maria" not in falso.ultimo_corpo_bruto
    assert set(json.loads(falso.ultimo_corpo_bruto)["Data"]) == {"Json"}
    cab = falso.ultimo_cabecalho
    assert cab["requestorigin"] == "7"
    assert cab["username"] == "user"
    assert cab["authorization"] == "Bearer tok-1"


@pytest.mark.parametrize(
    ("valor", "centavos"),
    [("0.29", 29), ("1.15", 115), ("19.99", 1999), ("0.57", 57), ("1234.56", 123456)],
)
async def test_centavos_sem_perda_de_float(adapter, falso, valor, centavos):
    # int(float * 100) erra em varios destes (ex.: 0.29 -> 28, 1.15 -> 114).
    await adapter.registrar_titulo(fazer_titulo(chave=valor, valor=valor))
    assert falso.ultimo_corpo["Data"]["Valor"] == centavos


async def test_email_ausente_nao_vira_dado_ficticio(adapter, falso):
    base = fazer_titulo()
    sem_email = base.model_copy(
        update={"pagador": base.pagador.model_copy(update={"email": None})}
    )
    await adapter.registrar_titulo(sem_email)
    assert "Email" not in falso.ultimo_corpo["Data"]


async def test_referencia_e_o_nosso_numero(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo(chave="abc"))
    assert registrado.id_banco == registrado.nosso_numero == "1000"
    assert registrado.seu_numero == "FAT-abc"
    await adapter.consultar_titulo(registrado.id_banco)
    assert falso.ultimo_corpo["Data"]["NossoNumero"] == "1000"
    await adapter.baixar_titulo(registrado.id_banco)
    assert falso.chamadas[-2][1].endswith("SolicitarBaixaBoletoEncrypted")
    assert falso.ultimo_corpo["Data"]["NossoNumero"] == "1000"


async def test_idempotencia_nao_gera_boleto_em_dobro(adapter, falso):
    titulo = fazer_titulo(chave="parcela-1")
    paralelos = await asyncio.gather(*(adapter.registrar_titulo(titulo) for _ in "abc"))
    de_novo = await adapter.registrar_titulo(titulo)
    assert {r.id_banco for r in paralelos} == {de_novo.id_banco}
    assert len(falso.boletos) == 1
    gerar = [c for c in falso.chamadas if c[1].endswith("GerarBoletoEncrypted")]
    assert len(gerar) == 1


async def test_chaves_diferentes_geram_boletos_diferentes(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo(chave="p1"))
    await adapter.registrar_titulo(fazer_titulo(chave="p2"))
    assert len(falso.boletos) == 2


async def test_falha_nao_fica_em_cache(falso, dormir):
    falhas = [True]

    def resposta(request):
        if falhas:
            falhas.clear()
            return httpx.Response(400, json={"Message": "x"})
        return falso.tratar(request)

    adapter = adapter_com(falso, dormir, resposta)
    titulo = fazer_titulo()
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(titulo)
    assert (await adapter.registrar_titulo(titulo)).id_banco == "1000"


async def test_orquestrador_com_pinbank(adapter, falso):
    orq = Orquestrador(adapter)
    a = await orq.emitir(fazer_titulo(chave="k"))
    b = await orq.emitir(fazer_titulo(chave="k"))
    assert a == b
    assert len(falso.boletos) == 1


async def test_consulta_liquidado_traz_valor_em_decimal_e_data(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo(valor="258.75"))
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("258.75")
    assert consultado.data_pagamento == date(2026, 11, 10)


async def test_valor_pago_so_quando_liquidado(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.valor_pago is None
    assert consultado.data_pagamento is None


async def test_resposta_bruta_mascarada(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.resposta_bruta["DadosPagador"] == "***"
    assert "Maria" not in repr(consultado) + str(consultado.resposta_bruta)
    assert registrado.resposta_bruta["Base64"] == "***"


async def test_baixa_pendente_devolve_estado_consultado(falso, dormir):
    falso.baixa_pendente = True
    adapter = PinbankAdapter(config(), client=falso.cliente(), dormir=dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.baixar_titulo(registrado.id_banco)
    assert baixado.status is StatusCanonico.PENDENTE_REGISTRO


@pytest.mark.parametrize("corpo", [{}, {"Data": {}}, {"Data": None}, []])
async def test_payload_sem_chave_e_validacao_nunca_keyerror(falso, dormir, corpo):
    adapter = adapter_com(falso, dormir, lambda r: cifrada(falso, corpo))
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(fazer_titulo())
    with pytest.raises(Validacao):
        await adapter.consultar_titulo("1")


async def test_registro_sem_nosso_numero_e_validacao(falso, dormir):
    corpo = {"Data": {"LinhaDigitavel": "1"}}
    adapter = adapter_com(falso, dormir, lambda r: cifrada(falso, corpo))
    with pytest.raises(Validacao, match="NossoNumero"):
        await adapter.registrar_titulo(fazer_titulo())


async def test_consulta_sem_status_e_validacao(falso, dormir):
    corpo = {"Data": {"NossoNumero": "1"}}
    adapter = adapter_com(falso, dormir, lambda r: cifrada(falso, corpo))
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo("1")


async def test_status_desconhecido_nao_vira_pago(falso, dormir):
    corpo = {"Data": {"Status": "ESTORNADO", "NossoNumero": "1"}}
    adapter = adapter_com(falso, dormir, lambda r: cifrada(falso, corpo))
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo("1")


async def test_resposta_sem_data_json_traz_mensagem(falso, dormir):
    adapter = adapter_com(
        falso,
        dormir,
        lambda r: httpx.Response(200, json={"Message": "erro do banco", "Data": None}),
    )
    with pytest.raises(Validacao) as erro:
        await adapter.consultar_titulo("1")
    assert erro.value.resposta_bruta == {"Message": "erro do banco"}


async def test_5xx_e_retentado(falso, dormir, dormidas):
    chamadas = []

    def resposta(request):
        chamadas.append(request)
        if len(chamadas) == 1:
            return httpx.Response(503)
        return cifrada(falso, {"Data": {"Status": "REGISTRADO", "NossoNumero": "1"}})

    adapter = adapter_com(falso, dormir, resposta)
    consultado = await adapter.consultar_titulo("1")
    assert consultado.status is StatusCanonico.REGISTRADO
    assert len(chamadas) == 2
    assert dormidas


async def test_indisponivel_apos_esgotar_tentativas(falso, dormir):
    adapter = adapter_com(falso, dormir, lambda r: httpx.Response(503))
    with pytest.raises(Indisponivel):
        await adapter.consultar_titulo("1")


@pytest.mark.parametrize(
    "encargos",
    [
        Encargos(multa_percentual="2"),
        Encargos(juros_mensal_percentual="1"),
        Encargos(desconto_percentual="5", desconto_dias_antes=3),
        Encargos(abatimento="1"),
    ],
)
async def test_encargos_de_contrato_incerto_sao_recusados(adapter, falso, encargos):
    titulo = fazer_titulo().model_copy(update={"encargos": encargos})
    with pytest.raises(Validacao, match="PinBank"):
        await adapter.registrar_titulo(titulo)
    assert not falso.boletos


async def test_documento_nao_numerico_e_recusado(adapter):
    base = fazer_titulo()
    pagador = base.pagador.model_copy(update={"documento": "12ABC345000195"})
    with pytest.raises(Validacao, match="numerico"):
        await adapter.registrar_titulo(base.model_copy(update={"pagador": pagador}))


async def test_pagador_sem_endereco_e_recusado(adapter):
    base = fazer_titulo()
    pagador = base.pagador.model_copy(update={"endereco": None})
    with pytest.raises(Validacao, match="endereco"):
        await adapter.registrar_titulo(base.model_copy(update={"pagador": pagador}))


async def test_webhook_instrucao_e_pdf_nao_suportados(adapter):
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook(b"{}")
    with pytest.raises(NaoSuportado):
        await adapter.enviar_instrucao("1", None)  # type: ignore[arg-type]
    with pytest.raises(NaoSuportado):
        await adapter.obter_pdf("1")


def test_ambiente_invalido():
    with pytest.raises(Validacao):
        _ = config(ambiente="homolog").url_base


def test_url_por_ambiente():
    assert config(ambiente="producao").url_base == "https://pinbank.com.br/services/api"
    assert config().url_base == "https://dev.pinbank.com.br/services/api"


def test_repr_nao_vaza_segredo():
    assert SENHA not in repr(config())
    assert SENHA not in repr(EnvelopeAesCbc(SENHA.encode()))
    auth = OAuth2Password(conta="c", url_token="u", usuario="x", senha=SENHA)
    assert SENHA not in repr(auth)


# --- envelope ---------------------------------------------------------------


def test_envelope_ida_e_volta_com_acento():
    env = EnvelopeAesCbc(SENHA.encode())
    corpo = json.dumps({"Data": {"Cidade": "São Paulo"}}).encode()
    cifrado = json.loads(env.cifrar(corpo))
    assert list(cifrado) == ["Data"]
    esperado = json.dumps({"Data": {"Cidade": "São Paulo"}}, ensure_ascii=False)
    assert env.decifrar(json.dumps(cifrado).encode()) == esperado.encode()


def test_envelope_e_deterministico_com_iv_fixo():
    env = EnvelopeAesCbc(b"0123456789abcdef")
    um = json.loads(env.cifrar(b'{"a": 1}'))["Data"]["Json"]
    dois = json.loads(env.cifrar(b'{"a":1}'))["Data"]["Json"]
    assert um == dois
    assert len(base64.b64decode(um)) % 16 == 0


@pytest.mark.parametrize("tamanho", [0, 15, 17, 33])
def test_envelope_recusa_chave_de_tamanho_invalido(tamanho):
    with pytest.raises(Validacao):
        EnvelopeAesCbc(b"x" * tamanho)


def test_envelope_chave_errada_e_validacao():
    cifrado = EnvelopeAesCbc(b"a" * 16).cifrar(b'{"Data": {"x": "y"}}')
    with pytest.raises(Validacao):
        EnvelopeAesCbc(b"b" * 16).decifrar(cifrado)


@pytest.mark.parametrize("corpo", [b"nao-json", b"[]", b'{"Data": {"Json": "!!"}}'])
def test_envelope_resposta_fora_do_formato(corpo):
    with pytest.raises(Validacao):
        EnvelopeAesCbc(b"a" * 16).decifrar(corpo)


def test_envelope_recusa_corpo_nao_json():
    with pytest.raises(Validacao):
        EnvelopeAesCbc(b"a" * 16).cifrar(b"xxx")


# --- autenticacao -----------------------------------------------------------


async def test_token_password_com_cache_e_invalidacao(falso):
    cliente = falso.cliente()
    auth = OAuth2Password(
        conta="c",
        url_token="https://x.test/services/api/token",
        usuario="u",
        senha="p",
    )
    cab = await asyncio.gather(*(auth.cabecalhos(None, cliente) for _ in range(10)))
    assert falso.tokens_emitidos == 1
    assert {c["Authorization"] for c in cab} == {"Bearer tok-1"}
    await auth.invalidar(None)
    await auth.cabecalhos(None, cliente)
    assert falso.tokens_emitidos == 2


async def test_token_recusado_e_validacao(dormir):
    cliente = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={}))
    )
    adapter = PinbankAdapter(config(), client=cliente, dormir=dormir)
    with pytest.raises(Validacao, match="credenciais"):
        await adapter.consultar_titulo("1")
