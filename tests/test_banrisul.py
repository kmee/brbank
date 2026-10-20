import itertools
import json
from datetime import date
from decimal import Decimal

import pytest

from brbank import (
    Capacidade,
    Duplicado,
    Encargos,
    Instrucao,
    NaoSuportado,
    Orquestrador,
    StatusCanonico,
    TipoInstrucao,
    Validacao,
    obter_classe,
)
from brbank.adapters.banrisul import (
    TABELA_STATUS,
    BanrisulAdapter,
    BanrisulConfig,
    dv_nosso_numero,
)
from tests.banrisul_falso import BENEFICIARIO, PDF, BanrisulFalso
from tests.conftest import fazer_titulo


def _config(**extra) -> BanrisulConfig:
    base = {
        "conta": "c1",
        "client_id": "cid",
        "client_secret": "segredo",
        "codigo_beneficiario": BENEFICIARIO,
        "especie": "02",
        "ambiente": "sandbox",
    }
    return BanrisulConfig(**{**base, **extra})


@pytest.fixture
def falso():
    return BanrisulFalso()


def _adapter(falso, dormir, **extra) -> BanrisulAdapter:
    return BanrisulAdapter(
        _config(**extra),
        client=falso.cliente(),
        dormir=dormir,
        hoje=lambda: date(2026, 11, 1),
    )


@pytest.fixture
def adapter(falso, dormir):
    return _adapter(falso, dormir)


def test_entry_point_e_marca_de_validacao():
    assert obter_classe("banrisul") is BanrisulAdapter
    assert BanrisulAdapter.validado_sandbox is False
    caps = BanrisulAdapter.capacidades
    for cap in (Capacidade.HIBRIDO, Capacidade.ALTERACAO, Capacidade.BAIXA):
        assert cap in caps
    assert Capacidade.PDF in caps
    assert Capacidade.WEBHOOK not in caps


@pytest.mark.parametrize(
    ("livre", "dv"),
    [
        # Exemplo do calculo na doc oficial e nossos numeros dos exemplos do portal.
        ("00189274", "46"),
        ("00000002", "64"),
        ("00007409", "69"),
        ("00000077", "81"),
        ("58854915", "47"),
        ("00064452", "68"),
        # Resto 1 no modulo 11: DV do modulo 10 sobe 1 (6 -> 7).
        ("00000016", "78"),
        # Resto 1 com DV 9 no modulo 10: vira 0.
        ("00000265", "06"),
    ],
)
def test_dv_nosso_numero(livre, dv):
    assert dv_nosso_numero(livre) == dv


def test_dv_recusa_parte_livre_invalida():
    with pytest.raises(Validacao):
        dv_nosso_numero("123")


@pytest.mark.parametrize(
    "extra",
    [
        {"codigo_beneficiario": "123"},
        {"especie": "DM"},
        {"ambiente": "homologacao"},
    ],
)
def test_config_invalida(extra):
    with pytest.raises(Validacao):
        _config(**extra)


def test_urls_por_ambiente():
    assert "apidev" in _config().url_base
    assert "apidev" in _config().url_token
    producao = _config(ambiente="producao")
    assert producao.url_base == "https://api.banrisul.com.br/cobranca/v1"
    assert producao.url_token == "https://api.banrisul.com.br/auth/oauth/v2/token"


async def test_token_e_cabecalho_beneficiario(adapter, falso):
    await adapter.registrar_titulo(fazer_titulo())
    form = falso.ultimo_form
    assert form["client_id"] == ["cid"]
    assert form["client_secret"] == ["segredo"]
    assert form["grant_type"] == ["client_credentials"]
    assert form["scope"] == ["boletos"]
    cab = falso.ultimo_cabecalho
    assert cab["bergs-beneficiario"] == BENEFICIARIO
    assert cab["authorization"] == "Bearer tok-1"
    assert "bergs-ambiente" not in cab


async def test_payload_de_registro(adapter, falso):
    titulo = fazer_titulo().model_copy(
        update={
            "encargos": Encargos(
                multa_percentual="2",
                juros_mensal_percentual="1",
                desconto_percentual="5",
                desconto_dias_antes=3,
                abatimento="1.50",
            ),
            "mensagens": ("linha um", "linha dois"),
            "dias_baixa_automatica": 30,
        }
    )
    await adapter.registrar_titulo(titulo)
    corpo = falso.ultimo_corpo
    assert corpo["ambiente"] == "P"
    t = corpo["titulo"]
    assert "nosso_numero" not in t
    assert t["seu_numero"] == "FAT-chave-1"
    assert t["valor_nominal"] == "150.00"
    assert t["especie"] == "02"
    assert t["data_emissao"] == "2026-11-01"
    assert t["data_vencimento"] == "2026-12-10"
    assert t["pagador"] == {
        "tipo_pessoa": "F",
        "cpf_cnpj": "52998224725",
        "nome": "Maria da Silva",
        "endereco": "Rua das Flores 10",
        "cep": "30110000",
        "cidade": "Belo Horizonte",
        "uf": "MG",
        "aceite": "N",
    }
    assert t["sacador"]["tipo_pessoa"] == "J"
    assert t["sacador"]["cpf_cnpj"] == "11222333000181"
    assert t["instrucoes"] == {
        "juros": {"codigo": 2, "taxa": "1.00"},
        "multa": {"codigo": 2, "taxa": "2.0"},
        "desconto": {"codigo": 2, "data": "2026-12-07", "taxa": "5.00"},
        "abatimento": {"valor": "1.50"},
        "baixa": {"codigo": 1, "prazo": 30},
    }
    assert t["pag_parcial"] == {"autoriza": 1, "codigo": 3}
    assert t["mensagens"] == [
        {"linha": 1, "texto": "linha um"},
        {"linha": 2, "texto": "linha dois"},
    ]
    assert t["hibrido"] == {"autoriza": "S"}


async def test_sem_encargos_juros_isento_e_sem_hibrido(falso, dormir):
    adapter = _adapter(falso, dormir, hibrido=False, aceite=True)
    await adapter.registrar_titulo(fazer_titulo())
    t = falso.ultimo_corpo["titulo"]
    assert t["instrucoes"] == {"juros": {"codigo": 3}}
    assert "hibrido" not in t
    assert t["pagador"]["aceite"] == "A"


async def test_registro_devolve_hibrido(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.id_banco == "0006445269"
    assert registrado.pix_copia_cola.startswith("000201")
    assert registrado.txid == "001000000108859458645500095851146"
    assert registrado.linha_digitavel


async def test_qr_pendente_nao_inventa_pix(adapter, falso):
    falso.qr_pendente = True
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.pix_copia_cola is None
    assert registrado.txid is None


async def test_retorno_01_tambem_e_registrado(dormir):
    falso = BanrisulFalso(centralizado=False)
    adapter = _adapter(falso, dormir)
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert registrado.status is StatusCanonico.REGISTRADO
    assert registrado.resposta_bruta["retorno"] == "01"


async def test_resposta_bruta_mascarada(adapter):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    pagador = registrado.resposta_bruta["pagador"]
    assert pagador["nome"] == "***"
    assert pagador["cpf_cnpj"] == "***"
    assert pagador["endereco"] == "***"
    assert "Maria" not in repr(registrado)


async def test_nosso_numero_gerado_com_dv_e_duplicado(falso, dormir):
    sequencia = itertools.count(189274)
    adapter = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: next(sequencia))
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["titulo"]["nosso_numero"] == "0018927446"
    assert registrado.id_banco == "0018927446"

    repetido = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: 189274)
    with pytest.raises(Duplicado) as erro:
        await repetido.registrar_titulo(fazer_titulo(chave="outra"))
    assert erro.value.id_banco == "0018927446"
    # O orquestrador transforma o Duplicado em consulta.
    consultado = await Orquestrador(repetido).emitir(fazer_titulo(chave="outra"))
    assert consultado.id_banco == "0018927446"


async def test_gerador_invalido(falso, dormir):
    adapter = _adapter(falso, dormir, gerar_nosso_numero=lambda _t: "123456789")
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(fazer_titulo())


async def test_homologacao_nao_finge_registro(falso, dormir):
    adapter = _adapter(falso, dormir, homologacao=True)
    with pytest.raises(Validacao, match="homologacao"):
        await adapter.registrar_titulo(fazer_titulo())
    assert falso.ultimo_corpo["ambiente"] == "T"
    assert falso.boletos == {}


@pytest.mark.parametrize(
    "update",
    [
        {"seu_numero": "NUMERO-LONGO-DEMAIS"},
        {"seu_numero": "FAT_1"},
        {"mensagens": tuple(f"m{i}" for i in range(10))},
        {"mensagens": ("x" * 76,)},
        {"dias_baixa_automatica": 100},
        {"encargos": Encargos(multa_percentual="2.25")},
        {"encargos": Encargos(juros_mensal_percentual="1.005")},
    ],
)
async def test_titulo_fora_do_contrato_e_recusado(adapter, falso, update):
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(fazer_titulo().model_copy(update=update))
    assert falso.boletos == {}


async def test_pagador_sem_endereco_e_recusado(adapter):
    titulo = fazer_titulo()
    sem = titulo.pagador.model_copy(update={"endereco": None})
    with pytest.raises(Validacao):
        await adapter.registrar_titulo(titulo.model_copy(update={"pagador": sem}))


async def test_cortes_de_tamanho_oficiais(adapter, falso):
    titulo = fazer_titulo()
    pagador = titulo.pagador.model_copy(
        update={
            "nome": "N" * 50,
            "endereco": titulo.pagador.endereco.model_copy(
                update={"logradouro": "L" * 45, "cidade": "C" * 20}
            ),
        }
    )
    await adapter.registrar_titulo(titulo.model_copy(update={"pagador": pagador}))
    enviado = falso.ultimo_corpo["titulo"]["pagador"]
    assert len(enviado["nome"]) == 40
    assert len(enviado["endereco"]) == 40
    assert len(enviado["cidade"]) == 15


async def test_consulta_liquidada(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert falso.chamadas[-1] == ("GET", f"/cobranca/v1/boletos/{registrado.id_banco}")
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago == Decimal("160.00")
    assert consultado.data_pagamento == date(2026, 12, 10)


async def test_liquidado_sem_valor_cobrado_usa_valor_pagamento(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.liquidar(registrado.id_banco)
    del falso.boletos[registrado.id_banco]["operacoes"]["valor_cobrado"]
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.valor_pago == Decimal("150.00")


async def test_pago_no_dia_liquida_sem_valor(adapter, falso):
    # Situacao A + pagamento 2: as operacoes so aparecem no dia seguinte.
    registrado = await adapter.registrar_titulo(fazer_titulo())
    falso.boletos[registrado.id_banco]["situacao_pagamento"] = 2
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.LIQUIDADO
    assert consultado.valor_pago is None


@pytest.mark.parametrize(
    ("situacao", "pagamento", "esperado"),
    [
        ("A", 1, StatusCanonico.REGISTRADO),
        ("A", 3, StatusCanonico.REGISTRADO),
        ("A", 4, StatusCanonico.BAIXADO),
        ("A", None, StatusCanonico.REGISTRADO),
        ("B", None, StatusCanonico.LIQUIDADO),
        ("D", None, StatusCanonico.BAIXADO),
        ("L", None, StatusCanonico.LIQUIDADO),
    ],
)
async def test_tabela_de_status(adapter, falso, situacao, pagamento, esperado):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    boleto = falso.boletos[registrado.id_banco]
    boleto["situacao_banrisul"] = situacao
    boleto["situacao_pagamento"] = pagamento
    assert (await adapter.consultar_titulo(registrado.id_banco)).status is esperado


@pytest.mark.parametrize(
    ("situacao", "pagamento"),
    [("R", None), ("T", None), ("P", None), ("A", 9), ("", None)],
)
async def test_status_sem_mapeamento_vira_validacao(
    adapter, falso, situacao, pagamento
):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    boleto = falso.boletos[registrado.id_banco]
    boleto["situacao_banrisul"] = situacao
    boleto["situacao_pagamento"] = pagamento
    with pytest.raises(Validacao, match="desconhecido"):
        await adapter.consultar_titulo(registrado.id_banco)


def test_tabela_versionada():
    assert TABELA_STATUS.versao == "2026.10"


async def test_baixa_leva_ambiente_e_fica_baixado(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    baixado = await adapter.baixar_titulo(registrado.id_banco)
    assert falso.chamadas[-1] == (
        "POST",
        f"/cobranca/v1/boletos/{registrado.id_banco}/baixar",
    )
    assert falso.ultimo_cabecalho["bergs-ambiente"] == "P"
    assert baixado.status is StatusCanonico.BAIXADO
    consultado = await adapter.consultar_titulo(registrado.id_banco)
    assert consultado.status is StatusCanonico.BAIXADO


async def test_prorrogacao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(
            chave_idempotencia="p1",
            tipo=TipoInstrucao.PRORROGACAO,
            nova_data_vencimento=date(2027, 1, 15),
        ),
    )
    assert ("PATCH", f"/cobranca/v1/boletos/{registrado.id_banco}") in falso.chamadas
    patch = [c for c in falso.chamadas if c[0] == "PATCH"]
    assert len(patch) == 1
    assert falso.boletos[registrado.id_banco]["data_vencimento"] == "2027-01-15"


async def test_abatimento_e_baixa_por_instrucao(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    id_banco = registrado.id_banco
    resultado = await adapter.enviar_instrucao(
        id_banco,
        Instrucao(chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor="2.5"),
    )
    assert resultado.status is StatusCanonico.REGISTRADO
    instrucoes = falso.boletos[id_banco]["instrucoes"]
    assert instrucoes["abatimento"] == {"valor": "2.50"}
    baixado = await adapter.enviar_instrucao(
        id_banco, Instrucao(chave_idempotencia="b", tipo=TipoInstrucao.BAIXA)
    )
    assert baixado.status is StatusCanonico.BAIXADO


async def test_patch_manda_tipo_alteracao_e_ambiente(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    await adapter.enviar_instrucao(
        registrado.id_banco,
        Instrucao(chave_idempotencia="a", tipo=TipoInstrucao.ABATIMENTO, valor="1"),
    )
    # A ultima chamada e a reconsulta; o PATCH veio antes.
    assert falso.chamadas[-2][0] == "PATCH"


async def test_instrucoes_invalidas(adapter):
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.PRORROGACAO)
        )
    with pytest.raises(Validacao):
        await adapter.enviar_instrucao(
            "1", Instrucao(chave_idempotencia="x", tipo=TipoInstrucao.ABATIMENTO)
        )
    for tipo in (TipoInstrucao.PROTESTO, TipoInstrucao.NEGATIVACAO):
        with pytest.raises(NaoSuportado):
            await adapter.enviar_instrucao(
                "1", Instrucao(chave_idempotencia="x", tipo=tipo)
            )


async def test_alteracao_em_homologacao_e_validacao(falso, dormir):
    registrado = await _adapter(falso, dormir).registrar_titulo(fazer_titulo())
    homologa = _adapter(falso, dormir, homologacao=True)
    with pytest.raises(Validacao, match="homologacao"):
        await homologa.baixar_titulo(registrado.id_banco)
    assert falso.ultimo_cabecalho["bergs-ambiente"] == "T"
    assert falso.boletos[registrado.id_banco]["situacao_pagamento"] == 1


async def test_pdf(adapter, falso):
    registrado = await adapter.registrar_titulo(fazer_titulo())
    assert await adapter.obter_pdf(registrado.id_banco) == PDF
    assert falso.chamadas[-1][1].endswith("/emitir")
    assert falso.ultimo_cabecalho["bergs-ambiente"] == "P"


async def test_beneficiario_recusado_vira_validacao(falso, dormir):
    adapter = _adapter(falso, dormir, codigo_beneficiario="0010000027079")
    with pytest.raises(Validacao) as erro:
        await adapter.registrar_titulo(fazer_titulo())
    assert erro.value.status_http == 404


async def test_id_com_barra_nao_escapa_da_rota(adapter, falso):
    with pytest.raises(Validacao):
        await adapter.consultar_titulo("../boletos")
    assert falso.caminhos_brutos[-1] == b"/cobranca/v1/boletos/..%2Fboletos"


def test_webhook_so_devolve_referencias(adapter):
    corpo = json.dumps(
        {
            "nosso_numero": "0000000264",
            "codigo_barras": "04191808600000500002100100000010588549074094",
            "linha_digitavel": "04192100180000001058785490740942180860000050000",
            "beneficiario": {"codigo": BENEFICIARIO},
        }
    ).encode()
    assert adapter.referencias_webhook(corpo) == ["0000000264"]
    with pytest.raises(NaoSuportado):
        adapter.parse_webhook(corpo)


@pytest.mark.parametrize(
    "corpo",
    [
        b"nao e json",
        json.dumps({"beneficiario": {"codigo": BENEFICIARIO}}),
        {"nosso_numero": "1", "beneficiario": {"codigo": "0010000027079"}},
    ],
)
def test_webhook_invalido(adapter, corpo):
    with pytest.raises(Validacao):
        adapter.referencias_webhook(corpo)


def test_repr_da_config_nao_vaza_segredos():
    texto = repr(_config(client_secret="segredo-x", certificado_pem=b"cert"))
    assert "segredo-x" not in texto
    assert "cert" not in texto
