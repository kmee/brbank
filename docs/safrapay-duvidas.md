# Safrapay (gateway do grupo Safra): duvidas em aberto

`SafrapayAdapter.validado_sandbox = False`. Nada foi executado contra o Safrapay (nem
homologacao nem producao). Contrato lido em 2026-11-19 na documentacao do portal
<https://developers.safrapay.com.br> (guias e referencia vem no JavaScript do portal,
sem login). E outro produto que a API de Cobranca do Banco Safra (`safra`): aqui o
lojista fala com o gateway e o gateway registra o boleto no emissor.

## Fontes

Paginas do portal: Primeiros Passos (endpoints, MerchantToken), Autenticacao
(`/v2/merchant/auth`, `/v2/refreshtoken`), Boleto, Pix, Cancelamento, Consulta,
Webhook, Enums e Codigos de erro, Credenciamento, Changelog do gateway.

## O que o adapter faz

- Hosts: producao `https://payment.safrapay.com.br`, homologacao
  (`ambiente="sandbox"`) `https://payment-hml.safrapay.com.br`.
- Token: `POST /v2/merchant/auth` com o MerchantToken (`mk_...`) cru no `Authorization`,
  sem `Bearer` e sem corpo; resposta `accessToken` (JWT de 30 minutos, sem `expires_in`).
  O `refreshToken` e ignorado: com varios processos, pedir de novo com a chave e mais
  simples que guardar um refresh que muda a cada uso. Classe `ChaveParaToken` (auth.py),
  com armazem de token entre processos.
- Meio pela configuracao (`meio`): `BOLETO` (`POST /v2/charge/boleto`, `source` 7) ou
  `PIX` (`POST /v2/charge/pix`, `source` 1). `id_banco` = `charge.id` (UUID); nosso
  numero = `transactionId` do boleto.
- Idempotencia: `merchantChargeId` = `chave_idempotencia`, consultado em
  `GET /v2/charge?merchantChargeId=` antes de criar (achado vira `Duplicado`); o POST de
  emissao nao repete sozinho (`SEM_RETRY`).
- Consulta `GET /v2/charge/{id}`; id que nao e UUID e recusado antes da chamada.
- Novo vencimento: `PUT /v2/charge/boleto {chargeId, newDueDate}` (fica
  `PendingNewDeadline` ate o banco confirmar).
- Baixa: `PUT /v2/charge/cancelation/{id}` com `{}`. Recusada para cobranca paga
  (no Pix seria estorno). Boleto so a partir do dia seguinte a emissao (erro 195).
- PDF: `GET /v2/charge/{id}/boleto/{transactionId}`, rota publica (sem token).
- Webhook: `ChargeId` + `Transactions[0].TransactionStatus` (numero) e `PaymentType`
  (4 boleto, 8 Pix; cartao e ignorado). `Authorization` com o MerchantToken em Base64
  (aceita tambem `Basic <base64>`), conferido quando `conferir_webhook`.
- Capacidades: alteracao, baixa, PDF, webhook, Pix cob, emissao e instrucao
  assincronas. Instrucoes: prorrogacao e baixa (so baixa com `meio="PIX"`).

## Tabela de status (`2026.11`)

`PendingPayment` 7, `PreAuthorized` 1, `PendingNewDeadline` 11 e `PendingCancel` 6 ->
registrado; `Pending` 4 -> pendente de registro; `Paid` 8 -> liquidado; `Canceled` 5 ->
baixado; `Denied` 3, `ErrorCreation` 9 e `Timeout` 12 -> rejeitado. `Expired` 10 muda
com o meio: Pix -> baixado (motivo decurso de prazo, o QR morreu); boleto -> registrado
(vencido continua pagavel ate o emissor cancelar). `Captured` 2 fica de fora (cartao).

## Duvidas

1. **Onde cai o dinheiro do boleto.** O cadastro do lojista tem `bankSlipSettings` com
   emissor (Bs2, Safra, Santander), conta, `clientId`, usuario, senha e carteira: tudo
   indica que o gateway registra no emissor com as credenciais do proprio lojista e o
   credito vai para a conta dele. A documentacao nao afirma. Decide se o produto serve.
2. **Multa, juros e desconto: recusados com `Validacao`.** A tabela do boleto v2 cita
   `transactions[].fine {startDate, amount, interest}` e `discount {type, amount,
   deadline}` sem unidade (percentual ou centavos? juros ao dia ou ao mes?) e sem
   exemplo. A API legada de Fatura (`/v1/Invoice`) usa outros nomes. Confirmar na
   homologacao antes de enviar.
3. **Telefone do pagador.** A tabela do boleto marca `customer.phone` como obrigatorio,
   mas o exemplo de Pix e o texto nao. O modelo `Pessoa` nao tem telefone: o adapter
   nao envia. Se a homologacao exigir, o campo entra no modelo.
4. **Baixa de boleto pelo cancelamento.** A pagina de cancelamento fala de cartao e
   Pix; para boleto so ha o erro 195 e o `cancel_charge` do MCP. Confirmar na
   homologacao e se ha tarifa por cancelamento (`BankSlipTaxPlan.amountCancelled`).
5. **Valor e data do pagamento.** Nem a consulta nem o webhook trazem valor pago, data
   de pagamento ou de credito. `valor_pago` usa o `amount` da cobranca (Pix e boleto
   sem encargos sao pagos pelo valor exato); `data_pagamento` e `data_credito` ficam
   vazias.
6. **`Expired` do boleto.** Tratado como vencido e pagavel. Se o Safrapay so marcar
   `Expired` depois de `bankIssuerDaysToCancel`, o certo seria baixado.
7. **Formato da busca por `merchantChargeId`.** A documentacao diz que devolve "a
   primeira cobranca que coincidir"; o formato da resposta sem resultado (404, 400 com
   codigo 8, ou `success: false`) nao esta escrito. O adapter trata 404 e os codigos 0
   e 8 como "nao achou".
8. **Webhook.** Retentativas e intervalo nao documentados; um webhook por tipo de
   evento por lojista (erro 140), sem filtro por meio. Cadastro em
   `POST https://webhook[-hml].safrapay.com.br/v1/webhook/bulk` (header `MerchantId`,
   e-mail de contingencia obrigatorio) ou pelo portal; o adapter nao cadastra.
9. **Pix.** QR dinamico de 30 minutos, sem vencimento nem multa (sem cobv): serve para
   pagar na hora, nao para cobranca com prazo. O EMV do exemplo parece QR com chave e
   valor, sem `location`.
10. **Maturidade.** O changelog marca "evolucoes boleto v2 (Safra, download PDF)" como
    versao candidata de 23/03/2026.
11. **Homologacao.** Nao descreve como simular o pagamento de boleto ou Pix.
