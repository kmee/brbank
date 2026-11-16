"""Erros canonicos (B8). A resposta bruta chega sempre mascarada."""

import re
from typing import Any

from brbank.mascara import mascarar

# Chaves que os bancos usam para explicar a recusa, no corpo de erro:
# BB (erros[].mensagem, providencia), Bradesco (mensagem, errors,
# errosValidacao[].erros), Pix do BACEN, RFC 7807 (detail, violacoes[].razao) e os
# genericos (message, detalhe).
_CHAVES_MOTIVO = frozenset(
    {
        "mensagem",
        "message",
        "providencia",
        "detail",
        "detalhe",
        "descricao",
        "description",
        "error_description",
        "razao",
        "erros",
        "errors",
    }
)
_MAX_MOTIVOS = 4
_MAX_MOTIVO = 400
# Um banco que ecoa a requisicao no erro nao pode levar segredo para a tela.
_PAR_SECRETO = re.compile(
    r"(?i)\b(client_secret|client_id|access_token|refresh_token|token|password|senha)"
    r"(\s*[=:]\s*)[^&\s\"',;]+"
)


def motivo_do_banco(corpo: Any) -> str:
    """Texto curto com o motivo que o banco deu, lido do corpo de erro ja mascarado.

    Le so as chaves de mensagem conhecidas, sem repetir texto, e devolve vazio quando
    o corpo nao explica nada.
    """
    achados: list[str] = []

    def guardar(texto: str) -> None:
        texto = " ".join(texto.split())
        if texto and texto not in achados:
            achados.append(_PAR_SECRETO.sub(r"\1\2***", texto))

    def andar(valor: Any, sob_motivo: bool) -> None:
        if len(achados) >= _MAX_MOTIVOS:
            return
        if isinstance(valor, dict):
            for chave, item in valor.items():
                andar(item, isinstance(chave, str) and chave.lower() in _CHAVES_MOTIVO)
        elif isinstance(valor, list | tuple):
            for item in valor:
                andar(item, sob_motivo)
        elif isinstance(valor, str) and sob_motivo:
            guardar(valor)

    andar(corpo, False)
    texto = "; ".join(achados[:_MAX_MOTIVOS])
    if len(texto) > _MAX_MOTIVO:
        texto = texto[: _MAX_MOTIVO - 3] + "..."
    return texto


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
