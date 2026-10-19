# CLAUDE.md

Biblioteca `brbank` (cobranca bancaria unificada). Nao importar nada do Odoo.

```bash
uv sync                          # instala (inclui o entry point brbank.adapters)
uv run pytest                    # testes (sem rede: httpx.MockTransport)
uv run pytest --cov=brbank       # cobertura (meta 80% no core)
uv run ruff check .              # lint
```

## Layout

- `src/brbank/`: `modelos` (B1), `adapter` (B2), `capacidades` (B3), `registro` (B4),
  `transporte` (B5), `auth` (B6), `envelope` (B7), `erros` (B8), `status` (B9),
  `orquestrador` (B10), `mascara` (LGPD), `documento` (CPF/CNPJ).
- `src/brbank/adapters/<banco>.py`: um adapter por banco, registrado em
  `[project.entry-points."brbank.adapters"]`.
- `tests/test_contrato.py`: suite comum registrar -> consultar -> baixar; adapter
  novo entra em `FABRICAS` com um servidor falso.

## Regras

- Dinheiro sempre `Decimal`. Nunca logar nem guardar PII/segredo em claro: usar `mascarar`.
- CNPJ alfanumerico: A-Z sem letras vedadas, DV por ASCII-48.
- Adapter portado de modulo Odoo: citar a origem no docstring e listar divergencias.
- Texto sem travessao nem caractere tipografico (so teclado QWERTY). Commits:
  `[ADD]`/`[FIX]` em portugues, sem Co-Authored-By, trailer `Assisted-by: Claude`.
