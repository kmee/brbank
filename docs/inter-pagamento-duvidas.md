# Banco Inter API Banking v2 (pagamentos): duvidas

`InterPagamentoAdapter` (`brbank.adapters.inter_pagamento`, entry point `inter` em
`brbank.adapters_pagamento`): Pix por chave, por dados bancarios e copia e cola, boleto e guia com
codigo de barras. `validado_sandbox = False`: so servidor falso. O sandbox do Inter exige uma
integracao cadastrada no Internet Banking e o certificado dela (sem credencial publica).

## Fontes

- OpenAPI oficial da Banking v2 (3.0.3): `https://developers.inter.co/redocusaurus/swagger-banking-yaml.yaml`,
  o arquivo que alimenta `https://developers.inter.co/references/banking`. Lido em 29/10/2026.
- OpenAPI do token: `https://developers.inter.co/redocusaurus/swagger-token-yaml.yaml`.
- Changelog do portal (`https://developers.inter.co/changelog/feed.json`): Pix copia e cola na
  Banking v2 em 25/10/2023; sandbox de Pix pagamento em 25/10/2024; callbacks de REPROVADO,
  CANCELADO e APROVACAO_EXPIRADA desde 02/10/2024.
- Conferencia com o SDK oficial `inter-co/pj-sdk-python` (mesmas rotas, escopos e campos; manda
  `valor` como texto e conhece status antigos, ver abaixo).

**Acesso:** em 24/12/2025 o Inter fechou a API Pix e a Banking (fora saldo e extrato) para
integracoes novas; reabriu em 12/03/2026 com uma analise ("Em validacao") antes de liberar o
certificado, e uma analise regulatoria a mais desde 10/04/2026. Cliente novo nao tem acesso
imediato.

## Infraestrutura (confirmado no OpenAPI)

| Item | Valor |
| --- | --- |
| Base | `https://cdpj.partners.bancointer.com.br/banking/v2` (sandbox `https://cdpj-sandbox.partners.uatinter.co/banking/v2`) |
| Token | `POST {host}/oauth/v2/token`, form com `client_id`, `client_secret`, `grant_type=client_credentials`, `scope`; vale 60 min; 5 pedidos por minuto |
| mTLS | obrigatorio no token e em toda chamada, inclusive no sandbox |
| `x-conta-corrente` | so quando a integracao tem mais de uma conta (digitos com DV, sem zero a esquerda) |
| `x-id-idempotente` | so no `POST /pix`, UUID (RFC 4122), opcional |
| Escopos | `pagamento-pix.write/read`, `pagamento-boleto.write/read`, `pagamento-darf.write` (a consulta de DARF usa `pagamento-boleto.read`), `pagamento-lote.write/read`, `webhook-banking.write/read` |
| Limites | `POST /pix` 60 por minuto e 1 por segundo; `POST /pagamento` 120 por minuto; DARF e lote 10 por minuto |

## Mapa de endpoints e encaixe no `AdapterPagamento`

| Rota | Uso no adapter |
| --- | --- |
| `POST /pix` (`destinatario.tipo` CHAVE, DADOS_BANCARIOS, PIX_COPIA_E_COLA) | `pagar_lote` de `PIX` e `PIX_QRCODE` |
| `GET /pix/{codigoSolicitacao}` (ultimos 90 dias) | `consultar_pagamento` de Pix |
| `POST /pagamento` (boleto, convenio e tributo com codigo de barras) | `pagar_lote` de `BOLETO` e `GUIA` |
| `GET /pagamento?codigoTransacao=` | `consultar_pagamento` de boleto e guia |
| `DELETE /pagamento/{codigoTransacao}` | `cancelar_pagamentos` (agendamento de boleto e guia) |
| `PUT/GET/DELETE /webhooks/{pix-pagamento,boleto-pagamento}` | `configurar_webhook_pagamento(url, tipo)` e afins |
| callback (lista JSON) | `parse_webhook_pagamento` |
| `POST /pagamento/darf`, `GET /pagamento/darf` | fora: DARF sem codigo de barras nao tem `TipoPagamento` |
| `POST /pagamento/lote` (2 a 150 BOLETO ou DARF, resposta 202 com `idLote`), `GET /pagamento/lote/{idLote}` | fora por ora (ver duvida 5) |
| `GET/POST /webhooks/{tipo}/callbacks[/retry]` | fora (historico e reenvio de callback) |

Lote local (`LOTE_LOCAL`): cada linha e uma chamada; o numero do lote e local; `consultar_lote`
levanta `NaoSuportado`. Linha sem resposta volta `INDETERMINADO` e as seguintes nao saem, como no
Sicoob. As chamadas que movem dinheiro saem sem retry automatico.

**Pix.** `valor` e numero (obrigatorio tambem no copia e cola), `dataPagamento` AAAA-MM-DD (data
futura agenda), `descricao` ate 140. A resposta traz `codigoSolicitacao` (o `id_banco`) e
`tipoRetorno`; o endToEnd so aparece na consulta e no callback (vai em `autenticacao`). O
`x-id-idempotente` e um UUID5 da conta com a `chave_idempotencia` da linha: reenviar a mesma linha
manda a mesma chave. Pix por dados bancarios exige `nome` do favorecido (campo novo
`DestinoPix.nome`) e ISPB.

**Boleto e guia.** `valorPagar` em texto ("26.80"; no lote do Inter e numero), `dataVencimento`
obrigatoria: vem de `Pagamento.vencimento` (campo novo) ou, no boleto, do fator de vencimento do
codigo de barras (`brbank.instrumento.vencimento_do_boleto`, com o reinicio do fator em
22/02/2025). Guia sem `vencimento` e recusada antes de chamar o banco. `cpfCnpjBeneficiario` so vai
quando e CPF ou CNPJ numerico (o padrao do OpenAPI nao aceita o CNPJ alfanumerico).

**Aprovacao.** Conforme a configuracao da conta, o pagamento espera aprovacao no Internet Banking
(Menu Aprovar > Gestao de Aprovacoes). Nao ha rota de aprovacao: `LIBERACAO` nao e declarada e a
linha fica `ACEITO` ate a aprovacao.

| Inter | `StatusPagamento` |
| --- | --- |
| `tipoRetorno` APROVACAO / PROCESSADO / AGENDADO | `ACEITO` / `EM_PROCESSAMENTO` / `AGENDADO` |
| Pix: CRIADO, APROVADO, DEBITADO, ENVIADO | `EM_PROCESSAMENTO` |
| Pix: AGUARDANDO_APROVACAO | `ACEITO` |
| Pix: PAGO (e PIX_PAGO do SDK) | `PAGO` |
| Pix: REPROVADO, EXPIRADO, CANCELADO, AGENDAMENTO_CANCELADO | `CANCELADO` |
| Pix: FALHA, CANCELADO_SEM_SALDO, NAO_DEBITADO | `REJEITADO` |
| Pix: AGENDADO | `AGENDADO` |
| Pix: PARCIALMENTE_DEBITADO, PARCIALMENTE_PAGO | fora (vira `Validacao`) |
| Boleto: EMPROCESSAMENTO, APROVADO / AGUARDANDO_APROVACAO / AGENDADO / REALIZADO | `EM_PROCESSAMENTO` / `ACEITO` / `AGENDADO` / `PAGO` |
| Boleto: CANCELADO, AGENDADO_CANCELADO, REPROVADO, APROVACAO_EXPIRADA | `CANCELADO` |
| Boleto: ERRO, NAO_COMPENSADO | `REJEITADO` |
| Callback Pix: EFETIVADO / ERRO / REPROVADO, APROVACAO_EXPIRADA, CANCELADO | `PAGO` / `REJEITADO` / `CANCELADO` |
| Callback boleto: REALIZADO / AGENDADO / CANCELADO, REPROVADO, APROVACAO_EXPIRADA / ERRO | `PAGO` / `AGENDADO` / `CANCELADO` / `REJEITADO` |

## Inconsistencias do proprio OpenAPI (tratadas)

1. O exemplo do callback Pix usa `status: "PROCESSADO"`, fora do enum dele: lido como
   `EM_PROCESSAMENTO`.
2. Tipos de valor mudam por rota: `valorPagar` texto no boleto avulso e numero no lote; `valor` do
   Pix numero no pedido e texto no callback. O adapter le os dois.
3. A lista de status do Pix esta so na descricao, sem enum; o SDK oficial conhece ainda
   TRANSACAO_CRIADA, TRANSACAO_APROVADA, PIX_ENVIADO e PIX_PAGO (mapeados).
4. `dataPagamento` da resposta de DARF vem DD/MM/AAAA no exemplo (DARF esta fora do adapter).

## Duvidas (nao encontrado na documentacao)

1. **Copia e cola com valor diferente do QR.** O OpenAPI exige `valor` e nao diz o que acontece
   se ele difere do valor do QR (estatico com valor, dinamico, cobranca com vencimento e
   encargos). Nao ha equivalente ao `pagarComJurosMulta` do Sicoob: `Pagamento.pagar_com_juros_multa`
   e ignorado no Inter. Confirmar no sandbox; o erro de QR vem como 422 ("Nao foi possivel
   processar o QR Code").
2. **Idempotencia.** O que o Inter responde a um `x-id-idempotente` repetido (a mesma resposta? um
   erro?) e por quanto tempo guarda a chave: nao documentado. Boleto e guia nao tem chave. O 409
   do `POST /pagamento` tem so exemplo generico.
3. **endToEnd na inclusao.** O OpenAPI nao o tem na resposta do `POST /pix` (o SDK oficial le um
   `endToEndId` ali).
4. **`ENVIADO` x `PAGO`.** Os dois existem na lista de status do Pix; `ENVIADO` fica
   `EM_PROCESSAMENTO` ate o banco confirmar a semantica.
5. **Lote do banco.** `POST /pagamento/lote` aceita so BOLETO e DARF (2 a 150), sem Pix. Usar o
   lote do banco para boleto e guia (com `consultar_lote` por `idLote`) e o passo seguinte; exige
   decidir como a capacidade convive com o `LOTE_LOCAL` do Pix no mesmo adapter.
6. **Massa de teste.** O sandbox tem codigos de barras de teste (no OpenAPI), mas nao achamos QR
   nem chave Pix de teste para pagamento.
7. **Webhook com mais de uma conta.** Se o cadastro do webhook precisa de `x-conta-corrente`: nao
   dito. O adapter manda o header quando configurado.
