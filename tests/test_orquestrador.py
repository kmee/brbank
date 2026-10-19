import asyncio
from collections.abc import Mapping

import pytest

from brbank import (
    BankAdapter,
    Capacidade,
    Duplicado,
    Indisponivel,
    Orquestrador,
    StatusCanonico,
    TabelaStatus,
    TituloRegistrado,
    Validacao,
    registro,
)
from tests.conftest import fazer_titulo


class Falso(BankAdapter):
    banco = "falso"
    capacidades = Capacidade.BAIXA
    tabela_status = TabelaStatus.criar("1", {})

    def __init__(self):
        self.registros = 0
        self.em_voo = 0
        self.pico = 0
        self.falhar: dict[str, Exception] = {}

    async def registrar_titulo(self, titulo):
        self.em_voo += 1
        self.pico = max(self.pico, self.em_voo)
        await asyncio.sleep(0.01)
        self.em_voo -= 1
        if titulo.chave_idempotencia in self.falhar:
            raise self.falhar[titulo.chave_idempotencia]
        self.registros += 1
        return TituloRegistrado(
            banco="falso",
            id_banco=f"id-{titulo.chave_idempotencia}",
            status=StatusCanonico.REGISTRADO,
        )

    async def consultar_titulo(self, id_banco):
        return TituloRegistrado(
            banco="falso", id_banco=id_banco, status=StatusCanonico.REGISTRADO
        )

    async def enviar_instrucao(self, id_banco, instrucao):
        raise NotImplementedError

    async def baixar_titulo(self, id_banco):
        raise NotImplementedError

    def parse_webhook(self, corpo, cabecalhos: Mapping[str, str] | None = None):
        return []


async def test_emitir_e_idempotente_inclusive_em_paralelo():
    adapter = Falso()
    orq = Orquestrador(adapter)
    titulo = fazer_titulo("k")
    resultados = await asyncio.gather(*(orq.emitir(titulo) for _ in range(5)))
    assert adapter.registros == 1
    assert len({r.id_banco for r in resultados}) == 1
    assert orq._travas == {}


async def test_duplicado_com_id_vira_consulta():
    adapter = Falso()
    adapter.falhar["k"] = Duplicado("ja existe", id_banco="existente")
    registrado = await Orquestrador(adapter).emitir(fazer_titulo("k"))
    assert registrado.id_banco == "existente"


async def test_duplicado_sem_id_propaga():
    adapter = Falso()
    adapter.falhar["k"] = Duplicado("ja existe")
    with pytest.raises(Duplicado):
        await Orquestrador(adapter).emitir(fazer_titulo("k"))


async def test_falha_nao_e_guardada_e_pode_repetir():
    adapter = Falso()
    adapter.falhar["k"] = Indisponivel("fora")
    orq = Orquestrador(adapter)
    with pytest.raises(Indisponivel):
        await orq.emitir(fazer_titulo("k"))
    del adapter.falhar["k"]
    assert (await orq.emitir(fazer_titulo("k"))).id_banco == "id-k"


async def test_lote_isola_falhas_e_limita_concorrencia():
    adapter = Falso()
    adapter.falhar["t3"] = Validacao("recusado")
    titulos = [fazer_titulo(f"t{i}") for i in range(12)]
    resultados = await Orquestrador(adapter, concorrencia=4).emitir_em_varios(titulos)
    assert [r.chave for r in resultados] == [t.chave_idempotencia for t in titulos]
    assert [r.chave for r in resultados if not r.ok] == ["t3"]
    assert isinstance(resultados[3].erro, Validacao)
    assert sum(r.ok for r in resultados) == 11
    assert adapter.pico == 4


def test_registro_manual_e_inexistente():
    registro.registrar_adapter("falso", Falso)
    assert "falso" in registro.listar_adapters()
    assert isinstance(registro.criar_adapter("falso"), Falso)
    with pytest.raises(registro.AdapterNaoEncontrado):
        registro.obter_classe("nao-existe")


async def test_capacidades_em_runtime():
    adapter = Falso()
    assert adapter.suporta(Capacidade.BAIXA)
    assert not adapter.suporta(Capacidade.PDF)
    with pytest.raises(Validacao):
        await adapter.obter_pdf("x")
