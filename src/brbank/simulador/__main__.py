"""`python -m brbank.simulador`: sobe a API de DDA do BTG simulada."""

import argparse
import contextlib
import sys
from datetime import date

from brbank.simulador.servidor import Simulador


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m brbank.simulador",
        description="API de DDA do BTG Empresas simulada (dados ficticios).",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--porta", type=int, default=8899)
    ap.add_argument("--semente", choices=["demo"], help="carga inicial de debitos")
    ap.add_argument(
        "--rotacionar-refresh",
        action="store_true",
        help="cada renovacao devolve refresh token novo e invalida o anterior",
    )
    ap.add_argument("--client-id", default="sim-client")
    ap.add_argument("--client-secret", default="sim-secret")
    ap.add_argument("--refresh-inicial", default="sim-refresh-0")
    ap.add_argument("--expira-token", type=int, default=3600, help="segundos")
    ap.add_argument(
        "--webhook-url", help="destino dos webhooks (ou POST /_sim/webhook)"
    )
    ap.add_argument("--webhook-segredo", help="enviado em Authorization: Bearer")
    ap.add_argument("--hoje", help="data de referencia AAAA-MM-DD (padrao: hoje)")
    args = ap.parse_args(argv)

    sim = Simulador(
        args.host,
        args.porta,
        client_id=args.client_id,
        client_secret=args.client_secret,
        refresh_inicial=args.refresh_inicial,
        rotacionar_refresh=args.rotacionar_refresh,
        expira_token_s=args.expira_token,
        hoje=date.fromisoformat(args.hoje) if args.hoje else date.today(),
        semente=args.semente,
        webhook_url=args.webhook_url,
        webhook_segredo=args.webhook_segredo,
    )
    print(f"simulador BTG DDA em {sim.url} (semente: {args.semente or 'nenhuma'})")
    with contextlib.suppress(KeyboardInterrupt):
        sim.servir()
    return 0


if __name__ == "__main__":
    sys.exit(main())
