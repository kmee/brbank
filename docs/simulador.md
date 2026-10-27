# Simulador da API de DDA do BTG

Servidor HTTP que imita a API de DDA do BTG Empresas, para testes e e2e. So stdlib mais o proprio
`brbank`; estado em memoria; todos os dados sao ficticios. O `BtgAdapter` e o oraculo: o simulador
so emite o que o adapter entende, no formato da spec publica (ver `docs/btg-dda-duvidas.md`).

## Subir

```bash
python -m brbank.simulador --host 0.0.0.0 --porta 8899 --semente demo [--rotacionar-refresh]
```

Opcoes extras: `--client-id` e `--client-secret` (padrao `sim-client` e `sim-secret`),
`--refresh-inicial` (padrao `sim-refresh-0`, ja valido, dispensa o fluxo de consentimento),
`--expira-token` (segundos, padrao 3600), `--webhook-url` e `--webhook-segredo`,
`--hoje AAAA-MM-DD` (data de referencia dos vencimentos da semente, padrao a data de hoje).

Em codigo (testes): `with Simulador(semente="demo") as sim: ... sim.url`.

Apontar o adapter para ele:

```python
BtgConfig(
    ...,
    client_id="sim-client",
    client_secret="sim-secret",
    refresh_token="sim-refresh-0",
    url_base_override="http://127.0.0.1:8899",
    url_token_override="http://127.0.0.1:8899/oauth2/token",
)
```

## Rotas

OAuth (Basic com `client_id:client_secret` no token; PKCE S256 obrigatorio):

- `GET /oauth2/authorize?response_type=code&client_id&redirect_uri&state&code_challenge&code_challenge_method=S256`
  aprova sozinho e responde 302 para `redirect_uri?code=...&state=...`.
- `POST /oauth2/token` (form): `grant_type=authorization_code` (`code`, `redirect_uri`,
  `code_verifier`; o codigo vale uma vez) ou `grant_type=refresh_token`. Resposta:
  `access_token`, `refresh_token`, `token_type`, `expires_in`, `scope`. Com `--rotacionar-refresh`,
  cada renovacao devolve refresh token novo (`sim-refresh-N`) e invalida o anterior.

API (`Authorization: Bearer <access_token>`; 401 sem token valido; 403 `dda:disabled` se o DDA da
empresa nao esta ativo; erros no formato `{type, errors:[{detail, code, arguments}]}`; no sandbox o
header `x-response: error` forca 400):

| Rota | Resposta |
|---|---|
| `POST /{cnpj}/banking/direct-debit` | ativa: `{data:{id, owner:{document,name}, status:"ENABLED", tags}, _links}` |
| `DELETE /{cnpj}/banking/direct-debit` | desativa: mesmo formato, `status:"DISABLED"` |
| `GET /{cnpj}/banking/direct-debit/debits` | `{data:[debito], _links:{next?, previous?}}`; filtros `pageNumber` (1), `pageSize` (10), `payeeDocument`, `payeeBankCode`, `status`, `minAmount`, `maxAmount`, `minDueDate`, `maxDueDate`, `hidden` (padrao: so os nao ocultos) |
| `GET /{cnpj}/banking/direct-debit/debits/summary` | `{data:{total, totalOverdue, totalCreated, totalPendingApproval, totalScheduled}}`, cada um `{count, amount}` |
| `PATCH /{cnpj}/banking/direct-debit/debits` | corpo `{items:[{id, hidden}]}`; responde `{data: debito}` |

Debito da consulta: `id`, `amount` (numero), `dueDate`, `expirationDate` (`AAAA-MM-DDT00:00:00.000Z`),
`digitableLine` (47 digitos com todos os DVs corretos), `payee{document, fantasyName, socialName,
bankCode, bankName}`, `hidden`, `status` (`CREATED` que passou do vencimento aparece como `OVERDUE`).

Webhook (envelope `{webhookId:"sim-webhook", event, data}`, com `Authorization: Bearer <segredo>` se
houver segredo): `authorized-direct-debits.enabled|disabled|debit-added|debit-paid`; `data` tem
`directDebitId`, `owner{taxId,name}`, `status` e `debits[]` (com `payee.taxId` e `payee.bank`). O
`debit-paid` repete as datas sentinela `0001-01-01` dos exemplos oficiais. So sao entregues com o
DDA ativo para o CNPJ e um destino registrado.

## Admin (`/_sim/*`, sem autenticacao: nao expor)

| Rota | Efeito |
|---|---|
| `POST /_sim/webhook` `{url, segredo}` | registra o destino (`url` vazia remove) |
| `POST /_sim/debitos` `{cnpj_pagador, valor?, vencimento?, data_limite?, beneficiario_documento?, beneficiario_nome?, beneficiario_fantasia?, banco_emissor?, linha_digitavel?, status?}` | cria o debito (linha valida e beneficiario ficticio com CNPJ valido se omitidos) e dispara `debit-added`. 201 `{id, debito, webhook:{enviado, status_http?, erro?}}` |
| `POST /_sim/debitos/<id>/pagar` | status `PAYMENT_CONFIRMED` e webhook `debit-paid` |
| `POST /_sim/debitos/<id>/cancelar` | o debito some da listagem (o BTG nao tem evento de cancelamento) |
| `POST /_sim/debitos/<id>/reenviar` | reenvia o ultimo webhook do debito (teste de deduplicacao) |
| `POST /_sim/dda/<cnpj>/desativar` e `/ativar` | o banco liga ou desliga o DDA da empresa sozinho (webhook `disabled` ou `enabled`; a listagem passa a recusar com 403) |
| `POST /_sim/revogar-tokens` | invalida todos os access tokens (forca 401 e renovacao) |
| `POST /_sim/reset` `{vazio?}` | volta ao estado de partida (com a semente); `vazio:true` limpa tudo |
| `GET /_sim/estado` | `{hoje, dda, debitos, eventos, renovacoes_token, tokens_emitidos, webhook}` |

## Semente `demo`

DDA ativo para o CNPJ ficticio **11.222.333/0001-81** (`11222333000181`), com 12 debitos. Os
vencimentos sao relativos a data de referencia (`--hoje`, padrao a data de hoje), para a demo nao
envelhecer. O modulo Odoo de demo cria faturas de fornecedor que casam com estes dados.

| # | Beneficiario | CNPJ | Valor | Vencimento | Banco | Caso de matching |
|---|---|---|---|---|---|---|
| 1 | ALFA EMBALAGENS LTDA | 45000001000174 | 1240.00 | hoje +5 dias | 341 | valor e vencimento iguais ao da fatura |
| 2 | BETA PAPEIS S.A. | 45000002000119 | 3587.50 | hoje +8 dias | 001 | divergencia de valor (fatura de 3500.00) |
| 3 | GAMA LOGISTICA LTDA | 45000003000163 | 890.00 | hoje +10 dias | 237 | divergencia de vencimento (fatura vence 3 dias antes) |
| 4 | DELTA LOCACOES LTDA | 45000004000108 | 2500.00 | hoje +15 dias | 104 | duas parcelas iguais (1 de 2) |
| 5 | DELTA LOCACOES LTDA | 45000004000108 | 2500.00 | hoje +15 dias | 104 | duas parcelas iguais (2 de 2) |
| 6 | OMEGA SECURITIZADORA DE CREDITOS S.A. | 45000005000152 | 4200.00 | hoje +9 dias | 208 | cessao: emitido pela securitizadora |
| 7 | SIGMA SERVICOS LTDA | 45000007000141 | 760.00 | hoje +6 dias | 756 | CNPJ desconhecido, sem fatura |
| 8 | ZETA METALURGICA LTDA | 45000008000196 | 3000.00 | hoje +11 dias | 001 | agrupa duas NFs (1800.00 e 1200.00) |
| 9 | ETA SUPRIMENTOS LTDA | 45000009000130 | 540.00 | hoje +4 dias | 341 | fornecedor conhecido sem fatura |
| 10 | TETA MANUTENCAO LTDA | 45000010000165 | 1100.00 | hoje -3 dias | 237 | boleto vencido com fatura vencida |
| 11 | IOTA GRAFICA LTDA | 45000011000100 | 680.00 | hoje +7 dias | 104 | possivel duplicidade (1 de 2) |
| 12 | IOTA GRAFICA LTDA | 45000011000100 | 680.00 | hoje +7 dias | 104 | possivel duplicidade (2 de 2) |

Faturas de demo sugeridas (partner com o CNPJ da tabela, `document_number` livre):

- 1 ALFA: 1 fatura de 1240.00 vencendo em +5 (vinculo automatico).
- 2 BETA: 1 fatura de 3500.00 vencendo em +8 (a diferenca de 87.50 vai na baixa).
- 3 GAMA: 1 fatura de 890.00 vencendo em +7 (3 dias antes do boleto).
- 4 e 5 DELTA: 2 faturas de 2500.00 vencendo em +15 (ambiguidade: nunca vincula sozinho).
- 6 OMEGA: fatura de 4200.00 vencendo em +9 do fornecedor **EPSILON INDUSTRIA LTDA**
  (`45000006000105`, sem boleto proprio); a OMEGA entra como cessionaria cadastrada do fornecedor.
  A API do BTG nao traz sacador avalista, entao a cessao chega como boleto cujo beneficiario e a
  securitizadora.
- 7 SIGMA: nenhum partner e nenhuma fatura (fornecedor desconhecido).
- 8 ZETA: 2 faturas, 1800.00 e 1200.00, ambas vencendo em +11 (N:1 por soma).
- 9 ETA: partner cadastrado, sem fatura.
- 10 TETA: 1 fatura de 1100.00 vencida em -3.
- 11 e 12 IOTA: 1 fatura de 680.00 vencendo em +7 (o segundo boleto e a duplicidade).

O teste `tests/test_simulador.py::test_docs_do_simulador_listam_a_semente` falha se esta tabela
divergir de `src/brbank/simulador/dados.py`.

## Limites conhecidos

O simulador nao e o BTG: ele nao valida o escopo OAuth, nao tem ordem de pagamento, nao simula
`PAYMENT_PROCESSING`/`SCHEDULED` (so por `status` no admin) e escolhe os comportamentos que a doc
publica deixa em aberto (oculto some da lista, DDA inativo responde 403). Quem decide e o sandbox
real.
