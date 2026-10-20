# brbank

Biblioteca Python assincrona para emitir e acompanhar boletos e cobrancas Pix em
bancos brasileiros por uma interface unica. Nao importa nada do Odoo: o modulo
`l10n_br_payment_boleto` e uma das faces possiveis.

Licenca MIT. Python 3.10+ (o 3.10 e o do Odoo 16.0; `brbank._compat` cobre o que so
existe no 3.11) e `cryptography>=38`, para conviver com o pyOpenSSL da imagem do Odoo. **Alfa:** nenhum adapter foi validado em sandbox de
banco (`validado_sandbox = False`); o contrato de cada banco vem da documentacao
publica e de fontes de terceiros, com as duvidas em `docs/<banco>-duvidas.md`.
Estado: primeira fatia (modelo canonico, contrato
de adapter, transporte, autenticacao, orquestrador e os adapters da tabela abaixo).

## Bancos suportados

Todos os adapters passam pela mesma suite de contrato (registrar, consultar, baixar) contra um
servidor falso. "Origem" diz de onde veio o contrato: **Odoo** = portado de um modulo que ja roda
em producao (o adapter da biblioteca ainda nao rodou contra o banco), **ACBr** = levantado por leitura
do ACBr, usado so como documentacao, e de fontes de terceiros.

| Banco | Adapter (entry point) | Produto | Capacidades declaradas | Origem |
| --- | --- | --- | --- | --- |
| Inter (077) | `inter` | Cobranca v3, boleto hibrido | hibrido, so hibrido, baixa, PDF, webhook | Odoo |
| PinBank | `pinbank` | Boleto, envelope AES-CBC | baixa | Odoo |
| Sicoob (756) | `sicoob` | Cobranca v3, boleto hibrido | hibrido, alteracao, baixa | ACBr |
| Sicredi (748) | `sicredi` | Cobranca v2 | hibrido, alteracao, baixa, webhook | ACBr |
| Banco do Brasil (001) | `bb` | Cobranca v2 | alteracao, baixa | ACBr |
| Safra (422) | `safra` | Cobranca v1 (Safra Negocios) | so registro e consulta | ACBr |

Pix (BACEN `/cob`, `/cobv`, `/webhook`) por um adapter generico, `pix_bacen`, com um perfil por PSP
(host, escopos, headers e quirks): Inter, Banco do Brasil, Sicoob, Sicredi, Itau, Bradesco,
Santander e Efi. Capacidades: Pix cob e cobv, webhook, baixa.

Fora desta lista por enquanto: BNB (sem referencia de Pix confiavel), Caixa, Banrisul,
Unicred, BTG e C6 (planejados), gateways como Mercado Pago, PagBank e Stone (sob demanda) e a
variante ECOMM do Sicredi. Banco novo = um adapter com `BankAdapter` e um entry point em
`brbank.adapters`; veja `docs/` para o que cada banco ainda deixa em aberto.

**Nenhum adapter da biblioteca foi validado em sandbox.** Sicoob, Sicredi, Banco do Brasil e os
perfis Pix trazem `validado_sandbox = False` no codigo e so passam a `True` depois de rodar contra o
ambiente real do banco. Inter e PinBank herdam a logica de modulos Odoo em producao, mas o adapter
da biblioteca tambem ainda nao rodou contra o banco.

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

pix_bacen = "brbank.adapters.pix_bacen:PixBacenAdapter"
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

O `PixBacenAdapter` fala a API Pix do BACEN (`/cob`, `/cobv`, `/webhook`, devolucao) e
muda de banco por `PerfilPsp`: `inter`, `bb`, `sicoob`, `sicredi`, `itau`, `bradesco`,
`santander` e `efi` (BNB sem perfil). Todos com `validado_sandbox = False` ate rodar no
sandbox. O `txid` vem da chave de idempotencia, o copia-e-cola e o `txid` chegam em
`TituloRegistrado`, e `CONCLUIDA` sem Pix na consulta nao liquida. Duvidas e quirks:
`docs/pix-duvidas.md`.

```python
config = PixBacenConfig(
    perfil="sicoob",
    conta="matriz",
    chave_pix="...",
    client_id="...",
    ambiente="producao",
    certificado_pem=cert,
    chave_pem=chave,
)
adapter = PixBacenAdapter(config)
```

`brbank.criar_adapter("inter", config)` resolve pelo nome e
`adapter.suporta(Capacidade.PDF)` consulta as capacidades em runtime.

## Regras

- Valores em `Decimal` (float e recusado). Percentuais em unidade de percentual.
- Nome, documento e endereco ficam fora do `repr`; log, erro e resposta bruta
  passam por `brbank.mascara.mascarar`.
- Retry so para 429, 5xx e erro de transporte. Status desconhecido no banco
  levanta erro, nunca vira "pago".
- Webhook gera `EventoCanonico` e nao confirma nada: reconsulte antes de liquidar.
