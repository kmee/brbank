"""Envelope de payload plugavel (B7): cifra a requisicao, decifra a resposta."""

from abc import ABC, abstractmethod


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
