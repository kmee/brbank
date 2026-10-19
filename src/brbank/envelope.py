"""Envelope de payload plugavel (B7): cifra a requisicao, decifra a resposta."""

import base64
import binascii
import json
from abc import ABC, abstractmethod

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from brbank.erros import Validacao


class Envelope(ABC):
    ativo: bool = True

    @abstractmethod
    def cifrar(self, corpo: bytes) -> bytes: ...

    @abstractmethod
    def decifrar(self, corpo: bytes) -> bytes: ...


class EnvelopeNulo(Envelope):
    ativo = False

    def cifrar(self, corpo: bytes) -> bytes:
        return corpo

    def decifrar(self, corpo: bytes) -> bytes:
        return corpo


class EnvelopeAesCbc(Envelope):
    """AES-CBC com PKCS7 e base64, no formato do PinBank.

    Portado de `utils/cypher.py` do modulo Odoo `l10n_br_payment_boleto_pinbank`.
    A requisicao vai como `{"Data": {"Json": <base64 do JSON cifrado>}}` e a
    resposta volta no mesmo formato. O IV e fixo em zeros porque o PinBank
    assim exige: o mesmo texto gera sempre o mesmo cifrado, entao o envelope
    nao protege contra repeticao (o TLS e a protecao real).
    """

    def __init__(self, chave: bytes, iv: bytes = b"\0" * 16) -> None:
        if len(chave) not in (16, 24, 32):
            raise Validacao("chave AES deve ter 16, 24 ou 32 bytes")
        self._chave = chave
        self._iv = iv

    def __repr__(self) -> str:
        return "EnvelopeAesCbc(***)"

    def _cifra(self) -> Cipher:
        return Cipher(algorithms.AES(self._chave), modes.CBC(self._iv))

    def cifrar(self, corpo: bytes) -> bytes:
        try:
            # O modulo Odoo serializa sem escapar acentos; mantemos a paridade.
            texto = json.dumps(json.loads(corpo), ensure_ascii=False)
        except ValueError as exc:
            raise Validacao("corpo a cifrar nao e JSON") from exc
        padder = padding.PKCS7(128).padder()
        bloco = padder.update(texto.encode()) + padder.finalize()
        enc = self._cifra().encryptor()
        cifrado = enc.update(bloco) + enc.finalize()
        envelope = {"Data": {"Json": base64.b64encode(cifrado).decode()}}
        return json.dumps(envelope).encode()

    def decifrar(self, corpo: bytes) -> bytes:
        try:
            dados = json.loads(corpo)
            cifrado = (dados.get("Data") or {}).get("Json")
        except (ValueError, AttributeError) as exc:
            raise Validacao("resposta cifrada fora do formato") from exc
        if not cifrado:
            mensagem = dados.get("Message") if isinstance(dados, dict) else None
            raise Validacao(
                "resposta sem Data.Json", resposta_bruta={"Message": mensagem}
            )
        try:
            bruto = base64.b64decode(cifrado, validate=True)
            dec = self._cifra().decryptor()
            bloco = dec.update(bruto) + dec.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            return unpadder.update(bloco) + unpadder.finalize()
        except (binascii.Error, ValueError) as exc:
            raise Validacao("resposta cifrada ilegivel (chave errada?)") from exc
