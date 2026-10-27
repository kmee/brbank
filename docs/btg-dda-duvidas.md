# BTG Pactual Empresas, Banking DDA: duvidas em aberto

`BtgAdapter.validado_sandbox = False`. Nada foi executado contra o BTG; o DDA do adapter
(`AdapterDda`) foi escrito pela documentacao publica e testado so contra o simulador
(`docs/simulador.md`) e contra MockTransport.

## Fontes

Portal `developers.empresas.btgpactual.com`, lido em 2026-10-26. As paginas de referencia sao
montadas no navegador; a especificacao OpenAPI de cada operacao vem embutida (zlib mais base64) nos
scripts do portal e foi extraida de la, igual a cobranca:

- `docs/api-specs-target/Banking - DDA/` (ativar-dda-para-o-usuario, delete-direct-debit,
  get-debits, get-debits-summary, patch-debit, post-config, delete-config);
- ficha tecnica `docs/tecspecs/Banking - DDA-spec` (escopo e eventos de webhook com exemplos).

## Contrato adotado

| Operacao | Chamada | Observacao |
|---|---|---|
| `ativar_dda` | `POST /{cnpj}/banking/direct-debit` | corpo opcional (`tags`); 200 com `data.status` |
| `desativar_dda` | `DELETE /{cnpj}/banking/direct-debit` | 200 com `data.status` |
| `listar_debitos` | `GET /{cnpj}/banking/direct-debit/debits?pageNumber&pageSize&minDueDate` | cursor = proxima pagina |
| `resumo_debitos` | `GET /{cnpj}/banking/direct-debit/debits/summary` | |
| `alterar_debito` | `PATCH /{cnpj}/banking/direct-debit/debits` `{items:[{id,hidden}]}` | so mostrar ou ocultar |

Escopos: `openid` e `brn:btg:empresas:banking:direct-debit` (escrita); leitura aparece como
`brn:btg:empresas:banking:direct-debit.readonly` e `empresas.btgpactual.com/authorized-direct-debits.readonly`.
Token so por Authorization Code. `BtgConfig.escopos_dda` guarda o de escrita.

## Duvidas e suposicoes

1. **"Modificar DDA" nao altera o boleto.** A operacao e o `PATCH .../debits` e so muda `hidden`
   (ocultar ou mostrar um debito). Nao existe aceite, recusa ou alteracao de valor: o
   `alterar_debito(cnpj, id, **campos)` aceita so `oculto=True|False` e levanta `NaoSuportado` para o resto.
   O estado `RECUSADO` do `StatusDda` nunca e produzido pelo BTG (RF-25 do PRD, em aberto). A resposta
   do PATCH e documentada como um unico `Debit`, apesar de o corpo ser uma lista: o adapter aceita
   objeto ou lista. O campo `hidden` nao tem lugar no `DebitoDda` (fica na `resposta_bruta`).
2. **Quem e o pagador.** Nenhum debito traz o CNPJ do pagador: na consulta vem da URL, no webhook de
   `owner.taxId`. No `enabled` do exemplo oficial nao ha `owner` (o corpo parece o de uma transacao,
   com `accountId` `CNPJ-banco-agencia-conta`): o adapter usa o prefixo do `accountId` como CNPJ.
3. **Sem sacador avalista, numero do documento, desconto, juros nem multa.** O debito da API so tem
   `id`, `amount`, `dueDate`, `expirationDate`, `digitableLine`, `payee` e `status`. Esses campos do
   `DebitoDda` ficam `None`. A cessao so aparece como beneficiario diferente do fornecedor.
   `codigo_barras` e derivado da linha digitavel (so se os DVs conferem).
4. **Estados.** Consulta: `CREATED, OVERDUE, PAYMENT_PENDING_APPROVAL, PAYMENT_PROCESSING,
   PAYMENT_CONFIRMED, SCHEDULED`; webhook: `CREATED` e `PAID`. Mapeamento adotado: `PAID` e
   `PAYMENT_CONFIRMED` -> `PAGO`; todos os demais -> `ABERTO` (inclusive `OVERDUE`, que o Odoo
   enxerga pelo vencimento, e os intermediarios de pagamento iniciado no BTG, para o boleto nao
   sumir da fila enquanto o pagamento nao confirma). Estado desconhecido levanta `Validacao`. Nao
   se sabe se `PAYMENT_CONFIRMED` significa dinheiro liquidado nem se o debito pode voltar de
   `PAYMENT_PROCESSING` para `CREATED`. A consulta nao tem estado de cancelamento: boleto
   cancelado pelo beneficiario simplesmente some da listagem (a reconciliacao diaria e quem
   percebe; `CANCELADO` e `BAIXADO` nunca saem do BTG).
5. **Data sentinela.** O exemplo de `debit-paid` traz `dueDate` e `expirationDate` `0001-01-01`:
   viram `None`. O mesmo vale se aparecer na consulta.
6. **Linha digitavel do exemplo e invalida.** A da ficha (`2080000...4800`) nao fecha os DVs. O
   adapter exige so 47 ou 48 digitos do banco (nao recusa a divida por DV) e nao preenche o
   `codigo_barras` quando os DVs nao conferem. `normalizar_linha_digitavel` confere os DVs por
   padrao, para quem quiser alertar.
7. **Id do evento.** O envelope e `{webhookId, event, data}`. Nao se sabe se `webhookId` e o id
   do cadastro do webhook (constante) ou da entrega. Por isso o `id_evento` e `eventId` ou `id`
   do envelope, se existirem, senao o hash do corpo (reenvio identico = mesmo id; evento
   diferente = id diferente). Nunca o `webhookId`. Confirmar.
8. **Nome do evento.** Fichas: `authorized-direct-debits.enabled|disabled|debit-added|debit-paid`.
   O escopo de leitura sugere o prefixo `empresas.btgpactual.com/`: o adapter casa pelo trecho
   `authorized-direct-debits.` em qualquer posicao. Sufixo desconhecido e ignorado.
9. **Paginacao.** `pageNumber`/`pageSize` e `_links.next` (o exemplo de `next` aponta para
   `/direct-debits/<id>`, lixo de copia). O adapter usa pagina numerica e considera a pagina curta
   (menos itens que `pageSize`) como a ultima; so segue se vier `_links.next`. Tamanho padrao 100
   (`BtgAdapter.tamanho_pagina_dda`), maximo nao documentado. Ordenacao nao documentada: durante a
   reconciliacao, debito novo pode deslocar paginas (a deduplicacao por linha digitavel absorve).
10. **`desde`.** O unico filtro de data e de vencimento (`minDueDate`). `listar_debitos(desde=...)`
    filtra por vencimento minimo, nao por data de criacao: boleto vencido e ainda aberto exige
    `desde=None`.
11. **Ocultos.** Nao se sabe se a listagem esconde os ocultos por padrao (`hidden` e filtro
    opcional). O adapter nao envia o filtro.
12. **Resumo.** `total` conta o que? O adapter usa `total.count` como `quantidade`, `total.amount`
    como `valor_total` e a soma de created, overdue, pending approval e scheduled como `ABERTO`.
13. **Ativar e desativar.** Resposta com `status` `ENABLED, DISABLED, PENDING_ENABLED,
    PENDING_DISABLED`: a ativacao pode ser assincrona. O adapter aceita `ENABLED` e `PENDING_ENABLED`
    depois de ativar e qualquer outro estado depois de desativar; o chamador espera o webhook
    `enabled` ou `disabled`. Nao se sabe se ativar e idempotente (ja ativo) nem se a adesao e por
    CNPJ ou por raiz. O DDA e uma adesao ao CIP: boletos anteriores a ativacao nao aparecem.
14. **Configs.** `POST/DELETE .../direct-debit/configs` (tipo `AUTO_HIDE` por documento do emissor)
    ficam fora do contrato.
15. **Webhook.** Mesma URL e segredo da cobranca (`Authorization: Bearer`). Nao se sabe se uma
    empresa admite mais de uma URL por evento. O cadastro do webhook e pela API de Webhooks (token
    Client Credentials), fora da biblioteca.
16. **Sandbox.** Wiremock com `x-response`: o adapter manda o header em `ambiente="sandbox"`, como
    na cobranca. O servidor do sandbox da DDA e `api-sandbox.empresas.btgpactual.com` na spec e
    `api.sandbox.empresas.btgpactual.com` na cobranca: nao confirmado qual vale (a cobranca usa o
    segundo, que ja esta em `URL_SANDBOX`).
17. **OAuth.** `url_autorizacao` aponta para `id.btgpactual.com/oauth2/authorize` (spec) e, no
    sandbox, `id.sandbox.btgpactual.com/oauth2/authorize` (suposto pelo host do token). Parametros
    padrao OAuth2: `response_type=code`, `client_id`, `redirect_uri`, `scope`, `state`, PKCE S256.
    Nao se sabe se o BTG Id aceita PKCE nem se exige `client_secret` na troca (o adapter manda Basic
    e `code_verifier`), nem quais `redirect_uri` ele aceita. Tambem nao se sabe se o refresh token
    rotaciona: o adapter suporta os dois casos (ver `ler_refresh_token` e `armazem`).
18. **CPF.** `owner.taxId` pode ser CPF (conta de pessoa fisica)? O `DebitoDda.cnpj_pagador`
    exige 14 caracteres: evento com CPF no `owner` levanta `Validacao`.
