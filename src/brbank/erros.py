"""Erros canonicos (B8). A resposta bruta chega sempre mascarada."""

from typing import Any

from brbank.mascara import mascarar


class ErroBrbank(Exception):
    """Base. `retryable` diz se repetir a mesma chamada pode funcionar."""

    retryable: bool = False

    def __init__(
        self,
        mensagem: str,
        *,
        resposta_bruta: Any = None,
        status_http: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status_http = status_http
        self.resposta_bruta = mascarar(resposta_bruta)
        if retryable is not None:
            self.retryable = retryable

    def __str__(self) -> str:
        return self.mensagem


class Duplicado(ErroBrbank):
    """O banco ja tem o titulo. Quem captura deve consultar, nao falhar."""

    def __init__(self, mensagem: str, *, id_banco: str | None = None, **kw: Any):
        super().__init__(mensagem, **kw)
        self.id_banco = id_banco


class Validacao(ErroBrbank):
    """Dado recusado (por nos ou pelo banco): repetir igual falha igual."""


class NaoSuportado(Validacao):
    """O adapter nao declara a capacidade pedida."""


class Indisponivel(ErroBrbank):
    """429, 5xx ou erro de transporte."""

    retryable = True


class AdapterNaoEncontrado(ErroBrbank):
    """Nenhum pacote instalado registra o adapter pedido."""
