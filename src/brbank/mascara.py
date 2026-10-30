"""Mascaramento de dados pessoais e segredos para log e respostas brutas.

Generaliza o `_pinbank_redact` do modulo Odoo: tudo que sai da biblioteca
para log, excecao ou campo tecnico passa por aqui antes.
"""

import json
import re
from typing import Any

MASCARA = "***"

_CHAVES_SENSIVEIS = frozenset(
    {
        "nome",
        "nomepagador",
        "identificacaochave",
        "identificacaorecebedor",
        "contarecebedor",
        "cpfcnpjfavorecido",
        "nomefavorecido",
        "numerocontafavorecido",
        "cnpjdebito",
        "nomeempresadebito",
        "numerocontadebito",
        "cpfoperador",
        "nomeoperador",
        "razaosocial",
        "cpfcnpj",
        "cpf",
        "cnpj",
        "documento",
        "endereco",
        "logradouro",
        "numero",
        "complemento",
        "bairro",
        "cidade",
        "cep",
        "email",
        "telefone",
        "ddd",
        "dadossacado",
        "dadospagador",
        "base64",
        "pdf",
        "pdfbase64",
        "pixcopiaecola",
        "emv",
        "chave",
        "infopagador",
        "solicitacaopagador",
        "infoadicionais",
        "nomefantasia",
        "nomerazaosocial",
        "numerodocumento",
        "nomesacado",
        "cnpjsacado",
        "endsacado",
        # APIs em ingles (BTG, C6, Santander).
        "payer",
        "payee",
        "name",
        "taxid",
        "documentnumber",
        "address",
        "street",
        "zipcode",
        "neighborhood",
        "city",
        "phone",
        "phonenumber",
        "pixkey",
        "refreshtoken",
        "accesstoken",
        "token",
        "clientsecret",
        "authorization",
        "senha",
        "password",
        # Asaas (cliente, cartao, QR e webhook).
        "postalcode",
        "addressnumber",
        "addresscomplement",
        "complement",
        "province",
        "mobilephone",
        "holdername",
        "ccv",
        "creditcardnumber",
        "creditcardtoken",
        "asaasaccesstoken",
        "payload",
        "encodedimage",
        # Pagamento de Pix copia e cola (Sicoob, Inter).
        "qrcode",
        "brcode",
        "chavedict",
    }
)

# Apenas CPF/CNPJ numericos soltos ou formatados: um CNPJ alfanumerico sem
# formatacao e indistinguivel de uma palavra de 14 letras em texto livre.
_DOCUMENTO_EM_TEXTO = re.compile(
    r"(?<![0-9A-Za-z])("
    r"\d{3}\.\d{3}\.\d{3}-\d{2}"
    r"|[0-9A-Z]{2}\.[0-9A-Z]{3}\.[0-9A-Z]{3}/[0-9A-Z]{4}-\d{2}"
    r"|\d{14}|\d{11}"
    r")(?![0-9A-Za-z])"
)
_BASE64_LONGO = re.compile(r"^[A-Za-z0-9+/=\s]{256,}$")
_LIMITE_TEXTO = 500


def _normalizar_chave(chave: str) -> str:
    return re.sub(r"[^a-z0-9]", "", chave.lower())


def mascarar_texto(texto: str) -> str:
    if _BASE64_LONGO.match(texto):
        return f"<omitido:{len(texto)}>"
    texto = _DOCUMENTO_EM_TEXTO.sub(MASCARA, texto)
    if len(texto) > _LIMITE_TEXTO:
        texto = texto[:_LIMITE_TEXTO] + "..."
    return texto


def mascarar(valor: Any) -> Any:
    """Devolve copia de `valor` sem dado pessoal nem segredo.

    Aceita dict, lista, texto (JSON ou livre), bytes e escalares.
    """
    if isinstance(valor, dict):
        return {
            chave: (
                MASCARA
                if isinstance(chave, str)
                and _normalizar_chave(chave) in _CHAVES_SENSIVEIS
                else mascarar(item)
            )
            for chave, item in valor.items()
        }
    if isinstance(valor, list | tuple):
        return [mascarar(item) for item in valor]
    if isinstance(valor, bytes | bytearray):
        return f"<bytes:{len(valor)}>"
    if isinstance(valor, str):
        try:
            decodificado = json.loads(valor)
        except ValueError:
            return mascarar_texto(valor)
        if isinstance(decodificado, dict | list):
            return mascarar(decodificado)
        return mascarar_texto(valor)
    return valor
