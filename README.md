# brbank

Biblioteca Python assincrona para emitir e acompanhar boletos e cobrancas Pix em
bancos brasileiros por uma interface unica. Nao importa nada do Odoo: o modulo
`l10n_br_payment_boleto` e uma das faces possiveis.

Licenca MIT. Python 3.11+. Estado: primeira fatia (modelo canonico, contrato
de adapter, transporte, autenticacao, orquestrador, `InterAdapter` e `PinbankAdapter`).

## Uso

```python
from brbank import Orquestrador
from brbank.adapters.inter import InterAdapter, InterConfig

config = InterConfig(
    conta="matriz",
    client_id="...",
    client_secret="...",
    conta_corrente="123456",
    ambiente="sandbox",
    certificado_pem=cert_bytes,
    chave_pem=chave_bytes,  # vindos do cofre
)
adapter = InterAdapter(config)
orquestrador = Orquestrador(adapter, concorrencia=10)

registrado = await orquestrador.emitir(titulo)  # idempotente por chave
resultados = await orquestrador.emitir_em_varios(lote)  # falha isolada por titulo
```

Um banco novo e um pacote que registra o adapter no entry point `brbank.adapters`:

```toml
[project.entry-points."brbank.adapters"]
inter = "brbank.adapters.inter:InterAdapter"
pinbank = "brbank.adapters.pinbank:PinbankAdapter"
sicoob = "brbank.adapters.sicoob:SicoobAdapter"

sicredi = "brbank.adapters.sicredi:SicrediAdapter"
```

O `PinbankAdapter` usa OAuth2 `password` e payload cifrado AES-CBC (`EnvelopeAesCbc`),
registra, consulta e baixa por `NossoNumero`, e nao tem webhook nem PDF. Contrato em
aberto: `docs/pinbank-duvidas.md`.

O `SicoobAdapter` (Cobranca Bancaria v3, boleto hibrido) usa OAuth2 `client_credentials`
sem `client_secret` (a identidade e o mTLS), header `client_id`, id do titulo = nosso
numero, registra, consulta, baixa e envia instrucao (prorrogacao, abatimento, protesto).
Sem PDF e sem webhook declarados. **`validado_sandbox = False`**: contrato montado de
documentacao de terceiros, nunca executado contra o Sicoob. Duvidas: `docs/sicoob-duvidas.md`.

O `BancoDoBrasilAdapter` (`bb`, API Cobranca v2, convenio de 6 ou 7 digitos) usa OAuth2
`client_credentials` e a chave `gw-dev-app-key` em query param; registra, consulta, baixa,
prorroga e abate, sem webhook nem PDF. Nasce com `validado_sandbox = False`: ver
`docs/bb-duvidas.md`.

O `SicrediAdapter` (API de Cobranca v2, OAuth2 `password`) registra, consulta, baixa e
altera (prorrogacao e abatimento); webhook sem assinatura exige reconsulta. Nasce com
`validado_sandbox = False`; contrato em aberto: `docs/sicredi-duvidas.md`.

`brbank.criar_adapter("inter", config)` resolve pelo nome e
`adapter.suporta(Capacidade.PDF)` consulta as capacidades em runtime.

## Regras

- Valores em `Decimal` (float e recusado). Percentuais em unidade de percentual.
- Nome, documento e endereco ficam fora do `repr`; log, erro e resposta bruta
  passam por `brbank.mascara.mascarar`.
- Retry so para 429, 5xx e erro de transporte. Status desconhecido no banco
  levanta erro, nunca vira "pago".
- Webhook gera `EventoCanonico` e nao confirma nada: reconsulte antes de liquidar.

Requisitos do produto: PRD "brbank cobranca bancaria unificada e integracao Odoo".
