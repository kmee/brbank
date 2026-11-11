from datetime import date

import pytest

from brbank import Endereco, Pessoa, Titulo

CPF_VALIDO = "529.982.247-25"
CNPJ_VALIDO = "91.300.766/0001-47"


def endereco() -> Endereco:
    return Endereco(
        logradouro="Rua das Flores",
        numero="10",
        bairro="Centro",
        cidade="Belo Horizonte",
        uf="mg",
        cep="30110-000",
    )


def pagador() -> Pessoa:
    return Pessoa(
        nome="Maria da Silva",
        documento=CPF_VALIDO,
        endereco=endereco(),
        email="maria@example.com",
    )


def fazer_titulo(chave: str = "chave-1", valor: str = "150.00") -> Titulo:
    return Titulo(
        chave_idempotencia=chave,
        seu_numero=f"FAT-{chave}",
        valor=valor,
        vencimento=date(2026, 12, 10),
        pagador=pagador(),
        beneficiario=Pessoa(
            nome="KMEE Ltda", documento=CNPJ_VALIDO, endereco=endereco()
        ),
    )


CAMPOS_DETALHE = (
    "canal_liquidacao",
    "data_credito",
    "valor_tarifa",
    "valor_juros_multa",
    "valor_desconto",
    "valor_abatimento",
    "motivo_baixa",
)


def assert_sem_detalhe(registro) -> None:
    """Resposta sem o detalhe da liquidacao: todos os campos ficam None."""
    preenchidos = {c: getattr(registro, c) for c in CAMPOS_DETALHE}
    assert preenchidos == dict.fromkeys(CAMPOS_DETALHE)


@pytest.fixture
def titulo() -> Titulo:
    return fazer_titulo()


@pytest.fixture
def dormidas() -> list[float]:
    return []


@pytest.fixture
def dormir(dormidas):
    async def _dormir(segundos: float) -> None:
        dormidas.append(segundos)

    return _dormir
