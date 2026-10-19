from decimal import Decimal

import pytest
from pydantic import ValidationError

from brbank import Encargos, Pessoa, StatusCanonico, TabelaStatus, Titulo, Validacao
from brbank.documento import cnpj_valido, cpf_valido, normalizar_documento
from brbank.mascara import mascarar
from tests.conftest import CPF_VALIDO, fazer_titulo


def test_cpf_e_cnpj_numericos():
    assert normalizar_documento(CPF_VALIDO) == "52998224725"
    assert normalizar_documento("11.222.333/0001-81") == "11222333000181"
    assert not cpf_valido("11111111111")
    assert not cnpj_valido("11222333000180")


def test_cnpj_alfanumerico_exemplo_da_receita():
    assert normalizar_documento("12.ABC.345/01DE-35") == "12ABC34501DE35"
    assert normalizar_documento("12abc34501de35") == "12ABC34501DE35"


def test_cnpj_alfanumerico_aceita_letras_antes_vedadas():
    # I, O, U, Q e F nao sao vedadas: o DV vale pelo calculo, nao pela letra
    base = "IOUQF0000000"
    from brbank.documento import _PESOS_CNPJ_1, _PESOS_CNPJ_2, _dv_cnpj

    dv1 = _dv_cnpj(base, _PESOS_CNPJ_1)
    dv2 = _dv_cnpj(base + str(dv1), _PESOS_CNPJ_2)
    assert cnpj_valido(f"{base}{dv1}{dv2}")


@pytest.mark.parametrize("ruim", ["", "123", "12ABC34501DE36", "1234567890A"])
def test_documento_invalido(ruim):
    with pytest.raises(ValueError, match="CPF nem um CNPJ"):
        normalizar_documento(ruim)


def test_pessoa_tipo_e_repr_sem_pii():
    p = Pessoa(nome="Maria da Silva", documento=CPF_VALIDO)
    assert p.tipo == "FISICA"
    assert "Maria" not in repr(p)
    assert "52998224725" not in repr(p)


def test_valor_em_decimal_e_centavos():
    assert fazer_titulo(valor="10.005").valor == Decimal("10.01")
    with pytest.raises(ValidationError):
        fazer_titulo(valor=10.5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        fazer_titulo(valor="0")


def test_titulo_e_imutavel():
    with pytest.raises(ValidationError):
        fazer_titulo().valor = Decimal(1)  # type: ignore[misc]
    assert isinstance(fazer_titulo(), Titulo)


def test_desconto_exige_percentual_e_dias():
    with pytest.raises(ValidationError):
        Encargos(desconto_percentual="5")


def test_mascara_dados_pessoais():
    saida = mascarar(
        {
            "pagador": {"nome": "Maria", "cpfCnpj": "52998224725", "uf": "MG"},
            "pdf": "JVBERi0" * 100,
            "msg": "doc 529.982.247-25 e 11222333000181 ok",
            "token": "abc",
        }
    )
    assert saida["pagador"] == {"nome": "***", "cpfCnpj": "***", "uf": "MG"}
    assert saida["pdf"] == "***"
    assert saida["token"] == "***"
    assert "529" not in saida["msg"] and "11222333" not in saida["msg"]
    assert mascarar('{"email": "a@b.c"}') == {"email": "***"}
    assert mascarar(b"abc") == "<bytes:3>"
    assert mascarar("A" * 400) == "<omitido:400>"


def test_erro_traz_resposta_mascarada():
    erro = Validacao("x", resposta_bruta='{"nome": "Maria"}')
    assert erro.resposta_bruta == {"nome": "***"}
    assert not erro.retryable


def test_tabela_status_desconhecido_levanta():
    tabela = TabelaStatus.criar("1", {"OK": StatusCanonico.LIQUIDADO})
    assert tabela.traduzir("ok") is StatusCanonico.LIQUIDADO
    with pytest.raises(Validacao):
        tabela.traduzir("OUTRO")
