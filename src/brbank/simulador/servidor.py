"""Servidor HTTP que imita a API de DDA do BTG Empresas (so stdlib + brbank).

Formato das respostas tirado da spec publica do BTG (docs/btg-dda-duvidas.md);
o `BtgAdapter` e o oraculo: o que ele nao entende, o simulador nao deve emitir.
Estado em memoria; nada e gravado em disco. Todos os dados sao ficticios.
"""

import base64
import hashlib
import json
import re
import secrets
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from brbank.dda import normalizar_linha_digitavel
from brbank.simulador.dados import (
    BENEFICIARIOS,
    CNPJ_DEMO,
    NOME_DEMO,
    SEMENTE_DEMO,
    cnpj_com_dv,
    montar_linha_digitavel,
    nome_do_banco,
    vencimento_da_semente,
)

PREFIXO_EVENTO = "authorized-direct-debits."
_CNPJ = re.compile(r"[0-9A-Z]{14}")
_SEM_DATA = "0001-01-01"
_STATUS_ABERTOS = ("CREATED", "OVERDUE", "PAYMENT_PENDING_APPROVAL", "SCHEDULED")


def _erro(status: int, detalhe: str, codigo: str = "sim:error") -> tuple[int, dict]:
    return status, {
        "type": "Type",
        "errors": [{"detail": detalhe, "code": codigo, "arguments": {}}],
    }


def _data_texto(valor: Any) -> date | None:
    if not valor:
        return None
    return date.fromisoformat(str(valor)[:10])


@dataclass
class Debito:
    id: str
    cnpj_pagador: str
    valor: Decimal
    vencimento: date | None
    data_limite: date | None
    linha: str
    doc_beneficiario: str
    razao: str
    fantasia: str
    banco: str
    status: str = "CREATED"
    oculto: bool = False
    cancelado: bool = False


@dataclass
class Estado:
    """Tudo que o simulador sabe. Um `RLock` protege cada leitura e escrita."""

    client_id: str = "sim-client"
    client_secret: str = field(default="sim-secret", repr=False)
    refresh_inicial: str = field(default="sim-refresh-0", repr=False)
    rotacionar_refresh: bool = False
    expira_token_s: int = 3600
    hoje: date = field(default_factory=date.today)
    semente: str | None = None
    webhook_url: str | None = None
    webhook_segredo: str | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self._inicializar()

    def _inicializar(self) -> None:
        self.debitos: dict[str, Debito] = {}
        self.dda: dict[str, dict[str, str]] = {}
        self.eventos: list[dict[str, Any]] = []
        self.refresh_validos: set[str] = {self.refresh_inicial}
        self.acessos: dict[str, float] = {}
        self.codigos: dict[str, dict[str, str]] = {}
        self.renovacoes = 0
        self.tokens_emitidos = 0
        self._seq_debito = 0
        self._seq_refresh = 0
        if self.semente:
            self._aplicar_semente(self.semente)

    def resetar(self, *, vazio: bool = False) -> None:
        with self.lock:
            semente = self.semente
            if vazio:
                self.semente = None
            self._inicializar()
            self.semente = semente

    # --- debitos ---------------------------------------------------------

    def _aplicar_semente(self, nome: str) -> None:
        if nome != "demo":
            raise ValueError(f"semente desconhecida: {nome!r}")
        self.dda[CNPJ_DEMO] = self._novo_dda(CNPJ_DEMO, "ENABLED")
        for item in SEMENTE_DEMO:
            quem = BENEFICIARIOS[item.beneficiario]
            vencimento = vencimento_da_semente(item, self.hoje)
            self.novo_debito(
                cnpj_pagador=CNPJ_DEMO,
                valor=item.valor,
                vencimento=vencimento,
                data_limite=vencimento + timedelta(days=30),
                doc_beneficiario=quem.cnpj,
                razao=quem.razao_social,
                fantasia=quem.fantasia,
                banco=quem.banco,
                semente=f"{CNPJ_DEMO}:{item.ordem}",
            )

    @staticmethod
    def _novo_dda(cnpj: str, status: str) -> dict[str, str]:
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"brbank-sim-dda:{cnpj}")),
            "status": status,
            "nome": NOME_DEMO,
        }

    def novo_debito(
        self,
        *,
        cnpj_pagador: str,
        valor: Decimal,
        vencimento: date | None,
        data_limite: date | None,
        doc_beneficiario: str | None = None,
        razao: str | None = None,
        fantasia: str | None = None,
        banco: str = "341",
        linha: str | None = None,
        status: str = "CREATED",
        semente: str | None = None,
    ) -> Debito:
        with self.lock:
            self._seq_debito += 1
            numero = self._seq_debito
            doc = doc_beneficiario or cnpj_com_dv(f"4600{numero:08d}")
            if linha is None:
                # Sem vencimento nao ha fator: usa o de hoje so para a linha.
                linha = montar_linha_digitavel(
                    banco=banco,
                    valor=valor,
                    vencimento=vencimento or self.hoje,
                    semente=semente or f"{cnpj_pagador}:{numero}",
                )
            else:
                linha = normalizar_linha_digitavel(linha, validar_dv=False)
            debito = Debito(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"brbank-sim:{numero}")),
                cnpj_pagador=cnpj_pagador,
                valor=valor,
                vencimento=vencimento,
                data_limite=data_limite,
                linha=linha,
                doc_beneficiario=doc,
                razao=razao or f"FORNECEDOR FICTICIO {numero} LTDA",
                fantasia=fantasia or f"FORNECEDOR {numero}",
                banco=banco,
                status=status,
            )
            self.debitos[debito.id] = debito
            return debito

    def status_efetivo(self, d: Debito) -> str:
        if d.status == "CREATED" and d.vencimento and d.vencimento < self.hoje:
            return "OVERDUE"
        return d.status

    def rest(self, d: Debito) -> dict[str, Any]:
        """Debito no formato da consulta (`payee.document`, datas com hora)."""

        def dia(valor: date | None) -> str:
            return f"{valor.isoformat() if valor else _SEM_DATA}T00:00:00.000Z"

        return {
            "id": d.id,
            "amount": float(d.valor),
            "dueDate": dia(d.vencimento),
            "expirationDate": dia(d.data_limite),
            "digitableLine": d.linha,
            "payee": {
                "document": d.doc_beneficiario,
                "fantasyName": d.fantasia,
                "socialName": d.razao,
                "bankCode": d.banco,
                "bankName": nome_do_banco(d.banco),
            },
            "hidden": d.oculto,
            "status": self.status_efetivo(d),
        }

    def webhook_debito(self, d: Debito, *, pago: bool = False) -> dict[str, Any]:
        """Debito no formato do webhook (`payee.taxId`, `payee.bank`).

        O `debit-paid` oficial traz as datas sentinela `0001-01-01`: o simulador
        repete para exercitar o tratamento.
        """

        def dia(valor: date | None) -> str:
            return _SEM_DATA if pago or valor is None else valor.isoformat()

        return {
            "id": d.id,
            "amount": float(d.valor),
            "dueDate": dia(d.vencimento),
            "expirationDate": dia(d.data_limite),
            "digitableLine": d.linha,
            "status": "PAID" if pago else self.status_efetivo(d),
            "payee": {
                "taxId": d.doc_beneficiario,
                "socialName": d.razao,
                "fantasyName": d.fantasia,
                "bank": {"code": d.banco, "name": nome_do_banco(d.banco)},
            },
        }

    def dono(self, cnpj: str) -> dict[str, str]:
        return {"taxId": cnpj, "name": NOME_DEMO}

    # --- webhook ---------------------------------------------------------

    def montar_evento(
        self, evento: str, cnpj: str, debito: Debito | None = None, pago: bool = False
    ) -> dict[str, Any] | None:
        """Envelope `{webhookId, event, data}`; None se o DDA nao esta ligado."""
        registro = self.dda.get(cnpj)
        if registro is None:
            return None
        data: dict[str, Any] = {
            "directDebitId": registro["id"],
            "owner": self.dono(cnpj),
            "status": registro["status"],
        }
        if debito is not None:
            data["debits"] = [self.webhook_debito(debito, pago=pago)]
        return {
            "webhookId": "sim-webhook",
            "event": PREFIXO_EVENTO + evento,
            "data": data,
        }

    def enviar(self, envelope: dict[str, Any] | None, debito_id: str = "") -> dict:
        """Entrega ao destino, fora da trava (ele pode chamar o simulador de volta)."""
        with self.lock:
            url, segredo = self.webhook_url, self.webhook_segredo
        if envelope is None or not url:
            return {"enviado": False}
        corpo = json.dumps(envelope).encode()
        cabecalhos = {"Content-Type": "application/json"}
        if segredo:
            cabecalhos["Authorization"] = f"Bearer {segredo}"
        registro: dict[str, Any] = {
            "evento": envelope["event"],
            "debito": debito_id,
            "destino": url,
            "corpo": envelope,
        }
        try:
            pedido = urllib.request.Request(url, data=corpo, headers=cabecalhos)
            with urllib.request.urlopen(pedido, timeout=5) as resp:
                registro["status_http"] = resp.status
        except urllib.error.HTTPError as exc:
            registro["status_http"] = exc.code
        except (urllib.error.URLError, OSError) as exc:
            registro["erro"] = type(exc).__name__
        with self.lock:
            registro["seq"] = len(self.eventos) + 1
            self.eventos.append(registro)
        return {
            "enviado": "erro" not in registro,
            **{k: registro[k] for k in ("status_http", "erro") if k in registro},
        }

    # --- OAuth -----------------------------------------------------------

    def novo_acesso(self) -> str:
        self.tokens_emitidos += 1
        token = f"at-{self.tokens_emitidos}"
        self.acessos[token] = time() + self.expira_token_s
        return token

    def acesso_valido(self, token: str) -> bool:
        with self.lock:
            return self.acessos.get(token, 0) > time()

    def trocar_refresh(self, refresh: str) -> dict[str, Any] | None:
        with self.lock:
            if refresh not in self.refresh_validos:
                return None
            self.renovacoes += 1
            novo = refresh
            if self.rotacionar_refresh:
                self._seq_refresh += 1
                novo = f"sim-refresh-{self._seq_refresh}"
                self.refresh_validos.discard(refresh)
                self.refresh_validos.add(novo)
            return self._resposta_token(novo)

    def trocar_codigo(
        self, codigo: str, redirect_uri: str, verificador: str
    ) -> dict[str, Any] | None:
        with self.lock:
            pedido = self.codigos.pop(codigo, None)
            if pedido is None or pedido["redirect_uri"] != redirect_uri:
                return None
            desafio = (
                base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if desafio != pedido["code_challenge"]:
                return None
            self._seq_refresh += 1
            novo = f"sim-refresh-c{self._seq_refresh}"
            self.refresh_validos.add(novo)
            return self._resposta_token(novo)

    def _resposta_token(self, refresh: str) -> dict[str, Any]:
        return {
            "access_token": self.novo_acesso(),
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self.expira_token_s,
            "scope": "openid brn:btg:empresas:banking:direct-debit",
        }

    # --- consulta --------------------------------------------------------

    def filtrar(self, cnpj: str, consulta: dict[str, list[str]]) -> list[Debito]:
        def um(chave: str) -> str | None:
            return consulta[chave][0] if consulta.get(chave) else None

        itens = [
            d
            for d in self.debitos.values()
            if d.cnpj_pagador == cnpj and not d.cancelado
        ]
        # Padrao do simulador: oculto some da lista (hidden=true os mostra).
        mostrar_ocultos = (um("hidden") or "").lower() == "true"
        itens = [d for d in itens if d.oculto == mostrar_ocultos]
        if um("status"):
            itens = [d for d in itens if self.status_efetivo(d) == um("status")]
        if um("payeeDocument"):
            itens = [d for d in itens if d.doc_beneficiario == um("payeeDocument")]
        if um("payeeBankCode"):
            itens = [d for d in itens if d.banco == um("payeeBankCode")]
        if um("minAmount"):
            itens = [d for d in itens if d.valor >= Decimal(um("minAmount") or 0)]
        if um("maxAmount"):
            itens = [d for d in itens if d.valor <= Decimal(um("maxAmount") or 0)]
        minimo, maximo = _data_texto(um("minDueDate")), _data_texto(um("maxDueDate"))
        if minimo:
            itens = [d for d in itens if d.vencimento and d.vencimento >= minimo]
        if maximo:
            itens = [d for d in itens if d.vencimento and d.vencimento <= maximo]
        return sorted(itens, key=lambda d: (d.vencimento or date.max, d.id))

    def resumo(self, cnpj: str) -> dict[str, Any]:
        itens = [
            d
            for d in self.debitos.values()
            if d.cnpj_pagador == cnpj and not d.cancelado and not d.oculto
        ]

        def bloco(status: tuple[str, ...]) -> dict[str, Any]:
            achados = [d for d in itens if self.status_efetivo(d) in status]
            return {
                "count": len(achados),
                "amount": float(sum((d.valor for d in achados), Decimal(0))),
            }

        return {
            "total": bloco(_STATUS_ABERTOS),
            "totalOverdue": bloco(("OVERDUE",)),
            "totalCreated": bloco(("CREATED",)),
            "totalPendingApproval": bloco(("PAYMENT_PENDING_APPROVAL",)),
            "totalScheduled": bloco(("SCHEDULED",)),
        }


def _basic_ok(cabecalho: str, estado: Estado) -> bool:
    esperado = base64.b64encode(
        f"{estado.client_id}:{estado.client_secret}".encode()
    ).decode()
    return cabecalho == f"Basic {esperado}"


def _link(href: str, metodo: str = "GET") -> dict[str, str]:
    return {"rel": "self", "href": href, "method": metodo, "type": "application/json"}


def criar_handler(estado: Estado) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # silencioso
            return

        # --- plumbing ----------------------------------------------------

        def _corpo(self) -> bytes:
            tamanho = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(tamanho) if tamanho else b""

        def _json_corpo(self) -> dict[str, Any]:
            bruto = self._corpo()
            if not bruto:
                return {}
            dados = json.loads(bruto)
            if not isinstance(dados, dict):
                raise ValueError("corpo deve ser um objeto JSON")
            return dados

        def _responder(self, status: int, corpo: Any = None, **cabecalhos: str) -> None:
            dados = b"" if corpo is None else json.dumps(corpo).encode()
            self.send_response(status)
            if dados:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(dados)))
            for chave, valor in cabecalhos.items():
                self.send_header(chave.replace("_", "-"), valor)
            self.end_headers()
            if dados:
                self.wfile.write(dados)

        def _par(self, resultado: tuple[int, Any]) -> None:
            self._responder(*resultado)

        def _despachar(self, metodo: str) -> None:
            partes_url = urlsplit(self.path)
            caminho = partes_url.path
            consulta = parse_qs(partes_url.query)
            partes = [p for p in caminho.split("/") if p]
            try:
                if caminho == "/oauth2/authorize" and metodo == "GET":
                    return self._autorizar(consulta)
                if caminho == "/oauth2/token" and metodo == "POST":
                    return self._par(self._token())
                if partes[:1] == ["_sim"]:
                    return self._par(self._admin(metodo, partes[1:], consulta))
                if len(partes) >= 3 and partes[1:3] == ["banking", "direct-debit"]:
                    return self._par(self._api(metodo, partes, consulta))
                return self._par(_erro(404, "rota desconhecida", "sim:not-found"))
            except (ValueError, InvalidOperation, KeyError) as exc:
                return self._par(_erro(400, f"requisicao invalida: {exc}"))

        def do_GET(self) -> None:
            self._despachar("GET")

        def do_POST(self) -> None:
            self._despachar("POST")

        def do_DELETE(self) -> None:
            self._despachar("DELETE")

        def do_PATCH(self) -> None:
            self._despachar("PATCH")

        # --- OAuth ---------------------------------------------------------

        def _autorizar(self, consulta: dict[str, list[str]]) -> None:
            def um(chave: str) -> str:
                return (consulta.get(chave) or [""])[0]

            if um("client_id") != estado.client_id:
                return self._par(_erro(400, "client_id desconhecido", "invalid_client"))
            if um("response_type") != "code" or not um("redirect_uri"):
                return self._par(_erro(400, "pedido de autorizacao invalido"))
            if um("code_challenge_method") != "S256" or not um("code_challenge"):
                return self._par(_erro(400, "PKCE S256 obrigatorio", "invalid_request"))
            codigo = secrets.token_urlsafe(16)
            with estado.lock:
                estado.codigos[codigo] = {
                    "redirect_uri": um("redirect_uri"),
                    "code_challenge": um("code_challenge"),
                }
            destino = um("redirect_uri")
            separador = "&" if "?" in destino else "?"
            volta = urlencode({"code": codigo, "state": um("state")}, quote_via=quote)
            self._responder(302, None, Location=f"{destino}{separador}{volta}")

        def _token(self) -> tuple[int, Any]:
            if not _basic_ok(self.headers.get("Authorization", ""), estado):
                return 401, {"error": "invalid_client"}
            campos = {k: v[0] for k, v in parse_qs(self._corpo().decode()).items()}
            tipo = campos.get("grant_type")
            if tipo == "refresh_token":
                resposta = estado.trocar_refresh(campos.get("refresh_token", ""))
            elif tipo == "authorization_code":
                resposta = estado.trocar_codigo(
                    campos.get("code", ""),
                    campos.get("redirect_uri", ""),
                    campos.get("code_verifier", ""),
                )
            else:
                return 400, {"error": "unsupported_grant_type"}
            if resposta is None:
                return 400, {"error": "invalid_grant"}
            return 200, resposta

        # --- API do BTG ----------------------------------------------------

        def _api(
            self, metodo: str, partes: list[str], consulta: dict[str, list[str]]
        ) -> tuple[int, Any]:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not estado.acesso_valido(token):
                return _erro(401, "token invalido ou expirado", "unauthorized")
            if self.headers.get("x-response") == "error":
                return _erro(400, "erro pedido por x-response", "sim:forced")
            cnpj, resto = partes[0].upper(), partes[3:]
            if not _CNPJ.fullmatch(cnpj):
                return _erro(400, "companyId invalido")
            if not resto:
                return self._ativacao(metodo, cnpj)
            if resto[0] != "debits" or len(resto) > 2:
                return _erro(404, "rota desconhecida", "sim:not-found")
            with estado.lock:
                registro = estado.dda.get(cnpj)
                if registro is None or registro["status"] != "ENABLED":
                    return _erro(
                        403, "DDA nao esta ativo para a empresa", "dda:disabled"
                    )
                if resto == ["debits", "summary"] and metodo == "GET":
                    return 200, {"data": estado.resumo(cnpj), "_links": [_link("/")]}
                if resto == ["debits"] and metodo == "GET":
                    return self._listar(cnpj, consulta)
                if resto == ["debits"] and metodo == "PATCH":
                    return self._ocultar(cnpj)
            return _erro(405, "metodo nao permitido")

        def _alternar_dda(self, cnpj: str, ligar: bool) -> tuple[dict[str, Any], Any]:
            """Liga ou desliga o DDA da empresa e monta o evento (o caller envia)."""
            with estado.lock:
                registro = estado.dda.get(cnpj) or estado._novo_dda(cnpj, "DISABLED")
                registro["status"] = "ENABLED" if ligar else "DISABLED"
                estado.dda[cnpj] = registro
                envelope = estado.montar_evento(
                    "enabled" if ligar else "disabled", cnpj
                )
                return dict(registro), envelope

        def _ativacao(self, metodo: str, cnpj: str) -> tuple[int, Any]:
            if metodo not in ("POST", "DELETE"):
                return _erro(405, "metodo nao permitido")
            ligar = metodo == "POST"
            registro, envelope = self._alternar_dda(cnpj, ligar)
            resposta = {
                "data": {
                    "id": registro["id"],
                    "owner": {"document": cnpj, "name": registro["nome"]},
                    "status": registro["status"],
                    "tags": [],
                },
                "_links": {
                    "enable": _link(f"/direct-debits/{registro['id']}", "POST"),
                    "disable": _link(f"/direct-debits/{registro['id']}", "DELETE"),
                },
            }
            estado.enviar(envelope)
            return 200, resposta

        def _admin_dda(self, cnpj: str, acao: str) -> tuple[int, Any]:
            """O banco liga ou desliga o DDA sozinho (webhook enabled ou disabled)."""
            cnpj = cnpj.upper()
            if not _CNPJ.fullmatch(cnpj):
                return _erro(400, "cnpj invalido (14 caracteres)")
            if acao not in ("ativar", "desativar"):
                return _erro(404, "acao desconhecida", "sim:not-found")
            registro, envelope = self._alternar_dda(cnpj, acao == "ativar")
            entrega = estado.enviar(envelope)
            return 200, {
                "cnpj": cnpj,
                "status": registro["status"],
                "webhook": entrega,
            }

        def _listar(self, cnpj: str, consulta: dict[str, list[str]]) -> tuple[int, Any]:
            pagina = int((consulta.get("pageNumber") or ["1"])[0])
            tamanho = int((consulta.get("pageSize") or ["10"])[0])
            if pagina < 1 or tamanho < 1:
                return _erro(400, "pageNumber e pageSize devem ser positivos")
            todos = estado.filtrar(cnpj, consulta)
            fatia = todos[(pagina - 1) * tamanho : pagina * tamanho]
            links: dict[str, Any] = {}
            if pagina * tamanho < len(todos):
                links["next"] = _link(
                    f"debits?pageNumber={pagina + 1}&pageSize={tamanho}"
                )
            if pagina > 1:
                links["previous"] = _link(
                    f"debits?pageNumber={pagina - 1}&pageSize={tamanho}"
                )
            return 200, {"data": [estado.rest(d) for d in fatia], "_links": links}

        def _ocultar(self, cnpj: str) -> tuple[int, Any]:
            itens = self._json_corpo().get("items") or []
            ultimo = None
            for item in itens:
                debito = estado.debitos.get(str(item.get("id")))
                if debito is None or debito.cnpj_pagador != cnpj or debito.cancelado:
                    return _erro(400, "debito nao encontrado", "dda:not-found")
                debito.oculto = bool(item.get("hidden"))
                ultimo = debito
            if ultimo is None:
                return _erro(400, "items vazio")
            return 200, {"data": estado.rest(ultimo), "_links": {}}

        # --- admin -----------------------------------------------------------

        def _admin(
            self, metodo: str, partes: list[str], consulta: dict[str, list[str]]
        ) -> tuple[int, Any]:
            if metodo == "GET" and partes == ["estado"]:
                return 200, self._estado()
            if metodo != "POST":
                return _erro(405, "metodo nao permitido")
            if partes == ["reset"]:
                estado.resetar(vazio=bool(self._json_corpo().get("vazio")))
                return 200, {"ok": True}
            if partes == ["revogar-tokens"]:
                with estado.lock:
                    estado.acessos.clear()
                return 200, {"ok": True}
            if partes == ["webhook"]:
                dados = self._json_corpo()
                with estado.lock:
                    estado.webhook_url = dados.get("url") or None
                    estado.webhook_segredo = dados.get("segredo") or None
                return 200, {"ok": True, "url": estado.webhook_url}
            if partes == ["debitos"]:
                return self._criar_debito(self._json_corpo())
            if len(partes) == 3 and partes[0] == "dda":
                return self._admin_dda(partes[1], partes[2])
            if len(partes) == 3 and partes[0] == "debitos":
                return self._acao_debito(partes[1], partes[2])
            return _erro(404, "rota desconhecida", "sim:not-found")

        def _criar_debito(self, dados: dict[str, Any]) -> tuple[int, Any]:
            cnpj = re.sub(
                r"[^0-9A-Za-z]", "", str(dados.get("cnpj_pagador", ""))
            ).upper()
            if not _CNPJ.fullmatch(cnpj):
                return _erro(400, "cnpj_pagador obrigatorio (14 caracteres)")
            valor = Decimal(str(dados.get("valor", "100.00")))
            if valor <= 0:
                return _erro(400, "valor deve ser positivo")
            vencimento = (
                _data_texto(dados["vencimento"])
                if "vencimento" in dados
                else estado.hoje + timedelta(days=7)
            )
            limite = (
                _data_texto(dados["data_limite"])
                if "data_limite" in dados
                else (vencimento + timedelta(days=30) if vencimento else None)
            )
            doc = dados.get("beneficiario_documento")
            debito = estado.novo_debito(
                cnpj_pagador=cnpj,
                valor=valor.quantize(Decimal("0.01")),
                vencimento=vencimento,
                data_limite=limite,
                doc_beneficiario=re.sub(r"[^0-9A-Za-z]", "", doc).upper()
                if doc
                else None,
                razao=dados.get("beneficiario_nome"),
                fantasia=dados.get("beneficiario_fantasia"),
                banco=str(dados.get("banco_emissor", "341")),
                linha=dados.get("linha_digitavel"),
                status=str(dados.get("status", "CREATED")),
            )
            with estado.lock:
                envelope = estado.montar_evento("debit-added", cnpj, debito)
            entrega = estado.enviar(envelope, debito.id)
            return 201, {
                "id": debito.id,
                "debito": estado.rest(debito),
                "webhook": entrega,
            }

        def _acao_debito(self, id_debito: str, acao: str) -> tuple[int, Any]:
            with estado.lock:
                debito = estado.debitos.get(id_debito)
                if debito is None:
                    return _erro(404, "debito desconhecido", "sim:not-found")
                if acao == "pagar":
                    debito.status = "PAYMENT_CONFIRMED"
                    envelope = estado.montar_evento(
                        "debit-paid", debito.cnpj_pagador, debito, pago=True
                    )
                elif acao == "cancelar":
                    # O BTG nao tem evento nem status de cancelamento: o boleto some
                    # da listagem (a reconciliacao diaria e quem percebe).
                    debito.cancelado = True
                    return 200, {"id": debito.id, "cancelado": True}
                elif acao == "reenviar":
                    envelope = next(
                        (
                            e["corpo"]
                            for e in reversed(estado.eventos)
                            if e["debito"] == debito.id
                        ),
                        None,
                    )
                    if envelope is None:
                        return _erro(404, "nenhum evento enviado para o debito")
                else:
                    return _erro(404, "acao desconhecida", "sim:not-found")
            entrega = estado.enviar(envelope, debito.id)
            return 200, {"id": debito.id, "status": debito.status, "webhook": entrega}

        def _estado(self) -> dict[str, Any]:
            with estado.lock:
                return {
                    "hoje": estado.hoje.isoformat(),
                    "dda": dict(estado.dda),
                    "debitos": [
                        {
                            **estado.rest(d),
                            "cnpj_pagador": d.cnpj_pagador,
                            "cancelado": d.cancelado,
                        }
                        for d in estado.debitos.values()
                    ],
                    "eventos": list(estado.eventos),
                    "renovacoes_token": estado.renovacoes,
                    "tokens_emitidos": estado.tokens_emitidos,
                    "webhook": {
                        "url": estado.webhook_url,
                        "segredo": bool(estado.webhook_segredo),
                    },
                }

    return Handler


class Simulador:
    """Sobe o servidor numa thread (testes) ou em primeiro plano (`python -m`)."""

    def __init__(self, host: str = "127.0.0.1", porta: int = 0, **opcoes: Any) -> None:
        self.estado = Estado(**opcoes)
        self._http = ThreadingHTTPServer((host, porta), criar_handler(self.estado))
        self._http.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def porta(self) -> int:
        return self._http.server_address[1]

    @property
    def url(self) -> str:
        host = self._http.server_address[0]
        return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{self.porta}"

    def iniciar(self) -> "Simulador":
        self._thread = threading.Thread(
            target=self._http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def servir(self) -> None:
        try:
            self._http.serve_forever()
        finally:
            self._http.server_close()

    def parar(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "Simulador":
        return self.iniciar()

    def __exit__(self, *_: object) -> None:
        self.parar()
