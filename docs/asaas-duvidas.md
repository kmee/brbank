# Asaas API v3: duvidas

`AsaasAdapter.validado_sandbox = True` desde 2026-10-25 (ver "Validacao no sandbox"). Nada rodou em
producao. O teste de integracao le `BRBANK_ASAAS_SANDBOX_API_KEY` (chave `$aact_hmlg_...` de uma
conta criada em sandbox.asaas.com) e roda com `uv run pytest -m sandbox tests/integracao`.

## Fontes

- Documentacao publica em https://docs.asaas.com (indice em `llms.txt`), lida em 2026-10-21.
- OpenAPI oficial: `https://www.asaas.com/openApi/document?version=3&languageCode=pt-BR`
  (1,1 MB). Quando guia e OpenAPI divergem, o adapter segue a OpenAPI e o ponto fica listado aqui.
- Nao ha modulo Odoo de origem nem SDK oficial em Python (`asaas-sdk` do PyPI e de terceiro,
  parado desde 2024, e nao foi usado).
- Analise completa, encaixe no Odoo e decisoes: `kmee-brain`, `kmee-produtos/brbank/ANALISE-ASAAS.md`.

## Validacao no sandbox (2026-10-25)

Teste: `tests/integracao/test_asaas_sandbox.py` (6 testes, 22 s). O sandbox do Asaas guarda estado,
entao a transicao de estado fica provada, nao so o contrato:

| Chamada | Resultado real |
| --- | --- |
| `POST /payments` (boleto) | 200, `PENDING`, `nossoNumero`, `bankSlipUrl` e `invoiceUrl`; `GET .../identificationField` traz linha e codigo de barras |
| `GET /payments?externalReference=` | acha a cobranca: segundo registro com a mesma chave vira `Duplicado` |
| `PUT /payments/{id}` (prorrogacao, abatimento) | 200; `dueDate` e `value` mudam, linha digitavel segue disponivel |
| `bankSlipUrl` | PDF de 25 KB sem chave de API |
| `POST /sandbox/payment/{id}/confirm` | 200; `GET` devolve `RECEIVED`, `value` = valor pago (abatido), `clientPaymentDate` de hoje, `originalValue` nulo |
| `DELETE /payments/{id}` de cobranca paga | 400 `invalid_action` ("so pendentes ou vencidas") |
| `DELETE` de cobranca aberta | 200; `GET` devolve `deleted: true` com `status: PENDING` (adapter le `BAIXADO`) |
| `GET /payments/pay_inexistente` | 404 sem corpo util |
| `POST /payments` (Pix) | 200 e `GET .../pixQrCode` com `payload`, mesmo sem chave Pix na conta; `identificationField` da 400 |
| `GET .../pixQrCode` de boleto sem chave Pix na conta | 400 `invalid_action` ("nao possui chave Pix") (adapter devolve QR vazio) |
| `POST /subscriptions` (boleto, mensal) | 200 `ACTIVE`; **a primeira cobranca nasce na hora** (vencimento dentro da antecedencia) e `nextDueDate` ja avanca um ciclo |
| `GET /subscriptions/{id}/paymentBook?month&year` | 200 PDF e gera as cobrancas ate o mes |
| `PUT /subscriptions/{id}` com `value` e `updatePendingPayments` | 200; **`value` muda** (a OpenAPI omite o campo) e as cobrancas pendentes mudam junto |
| `PUT` `status: INACTIVE` / `ACTIVE` + `nextDueDate` | 200 |
| `DELETE /subscriptions/{id}` | 200; `GET` devolve `deleted: true`, `status: INACTIVE`; a listagem de cobrancas fica vazia (pendentes removidas) |
| `POST /checkouts` `RECURRENT` | so `billingTypes: ["CREDIT_CARD"]`: com Pix, 400 ("RECURRENT so aceita CREDIT_CARD", "Pix exige DETACHED") |
| `customerData` no checkout | exige `phone`, `address`, `addressNumber`, `postalCode` e `province` juntos (o erro cita "phoneNumber", o campo e `phone`); sem `customerData`, 200 |
| `link` do checkout | `https://sandbox.asaas.com/000/checkoutSession/show/<id>` (formato diferente do guia) |
| `GET /webhooks` | lista vazia (webhook nao exercitado: exige URL publica) |

## O que o adapter assume (conferir no sandbox)

| Ponto | Assumido | Por que |
| --- | --- | --- |
| Idempotencia | `externalReference` = chave de idempotencia; `GET /payments?externalReference=` antes do `POST`; achado vira `Duplicado` | a API nao tem header de idempotencia; e a recomendacao oficial |
| Cliente | `GET /customers?cpfCnpj=` e, sem resultado, `POST /customers` com `notificationDisabled` | o Asaas nao deduplica clientes e manda e-mail/SMS por conta propria |
| Registro | sincrono: a cobranca nasce `PENDING` com `nossoNumero`; linha digitavel em `GET /payments/{id}/identificationField` | guia de boleto |
| QR Pix no boleto | `GET /payments/{id}/pixQrCode` e chamado para todo meio; recusa vira `None` quando o meio nao e PIX | com chave Pix na conta o boleto e hibrido; sem chave a resposta real (400? 404?) nao esta documentada |
| `txid` | nao preenchido | a API expoe `pixTransaction` so depois do pagamento; o id da cobranca (`pay_`) e a referencia |
| `seu_numero` | primeira linha de `description` (as mensagens vem depois) | a cobranca nao tem campo proprio para o numero do chamador |
| Abatimento | reduz `value` no registro e no `PUT` | o Asaas nao tem abatimento; `discount` e condicionado a data |
| Encargos | `fine` e `discount` em `PERCENTAGE`, `interest` percentual ao mes | OpenAPI |
| `daysAfterDueDateToRegistrationCancellation` | so para `BOLETO` e `UNDEFINED`; omitido usa o padrao da conta | imutavel depois da criacao |
| Status | `CONFIRMED` = liquidado; `REFUNDED` = baixado; `deleted: true` = baixado antes de olhar o status; status desconhecido levanta `Validacao` na consulta e e ignorado no webhook | decisoes do Mileo em 2026-10-21 |
| `valor_pago` | `value` da cobranca liquidada (`originalValue` guarda o valor original) | descricao dos campos |
| Instrucoes | prorrogacao e abatimento por `PUT /payments/{id}` com `billingType`, `value` e `dueDate` (os tres sao obrigatorios), depois reconsulta; baixa por `DELETE`; protesto e negativacao nao implementados | OpenAPI |
| PDF | download publico de `bankSlipUrl` sem a chave da conta | guia de boleto |
| Webhook | um evento por `POST`; tipo canonico vem de `payment.status` e `deleted`, nao do nome do evento; eventos informativos (`_VIEWED`, `_SPLIT_*`, `_ANTICIPATED`), de outras categorias ou com status desconhecido devolvem `[]`; `asaas-access-token` conferido so quando `webhook_auth_token` esta configurado | 15 respostas 400 seguidas pausam a fila |
| `dateCreated` do evento | "AAAA-MM-DD HH:MM:SS" sem fuso, mantido naive | exemplo da documentacao; fuso nao documentado |
| Rate limit | 5 req/s por conta no adapter; `429` vem sem `Retry-After` e cai no backoff proprio | cota de 25.000 por 12 h e 50 `GET` simultaneos |

## Assinaturas (brbank.recorrencia): o que o adapter assume

| Ponto | Assumido | Por que |
| --- | --- | --- |
| Criacao | `POST /subscriptions` com `externalReference` = chave; busca antes (`GET /subscriptions?externalReference=`) e achada vira `Duplicado`; cliente garantido como na cobranca | sem idempotencia na API |
| Cartao | `criar_assinatura` com meio CARTAO levanta `NaoSuportado`; o caminho e `criar_checkout` (`POST /checkouts`, `chargeTypes: RECURRENT`) | dado de cartao nao passa pela biblioteca (PCI); tokenizacao em producao depende de habilitacao |
| Checkout | `billingTypes` so cartao (recorrente); `items[0]` com `imageBase64` (pixel transparente quando a configuracao nao traz imagem); `subscription.nextDueDate` enviado como `AAAA-MM-DD 00:00:00`; URL = `link` da resposta ou `.../checkoutSession/show?id=` | OpenAPI exige imagem e callback; o guia mostra data e hora no campo |
| Ligacao checkout -> assinatura | `subscription.checkoutSession` (resposta e webhook `SUBSCRIPTION_CREATED`) | a assinatura criada pelo checkout nao herda `externalReference` (nao documentado) |
| Alteracao | `PUT /subscriptions/{id}` so com os campos preenchidos; `value` funciona (confirmado no sandbox, a OpenAPI omite); `updatePendingPayments` quando `aplicar_nas_pendentes` | sandbox 2026-10-25 |
| Suspender e reativar | `status: INACTIVE`; `ACTIVE` exige `nextDueDate` novo | FAQ |
| Encerrar | `DELETE /subscriptions/{id}`; o Asaas remove as cobrancas pendentes e vencidas | FAQ |
| Cobrancas do ciclo | geradas pelo Asaas 40 dias antes do vencimento (14 ou 7 por configuracao da conta); chegam por `PAYMENT_CREATED` com `payment.subscription`, que vira `EventoCanonico.id_recorrencia`; `listar_cobrancas` pagina `GET /subscriptions/{id}/payments` sem buscar linha digitavel nem QR | FAQ e OpenAPI |
| Sandbox | `GET /subscriptions/{id}/paymentBook?month=&year=` antecipa a geracao dos ciclos | guia do sandbox |

## Duvidas abertas

1. **Fuso de `dateCreated`** nos eventos e de `paymentDate`/`clientPaymentDate` (o sandbox devolveu a
   data de hoje em Brasilia; sem hora para comparar).
2. **Sandbox nao aplica juros, multa e desconto**: os campos sao aceitos mas nao exercitados; validar
   em producao com valor baixo.
3. **Antecedencia de geracao das cobrancas de assinatura** (40, 14 ou 7 dias): configuracao da
   conta no painel; a API nao a expoe. No sandbox a primeira cobranca nasceu junto com a assinatura
   (vencimento a 15 dias).
4. **Checkout recorrente**: se o evento `SUBSCRIPTION_CREATED` traz `creditCardToken`; e o que
   acontece com `customerData` quando a conta tem a tokenizacao habilitada.
5. **Retentativa de cartao recusado** no ciclo de uma assinatura: nao documentada.
6. **IPs do sandbox** para o webhook: a lista oficial e so de producao. O webhook nao foi exercitado
   (exige URL publica): fica para o ciclo com o Odoo exposto por tunel.
7. **Boleto hibrido**: a conta de sandbox ainda nao tem chave Pix; com chave, confirmar que o
   `pixQrCode` do boleto responde 200 e o PDF traz o QR.

Resolvidas no sandbox em 2026-10-25 (ver a tabela acima): corpo do 404, QR em boleto sem chave
(400), `value` no `PUT` da assinatura (funciona), URL do checkout no sandbox (`link` da resposta),
formato de `nextDueDate` do checkout (`AAAA-MM-DD 00:00:00` aceito) e meios do checkout recorrente
(so cartao, o que elimina a duvida sobre o `billingType` escolhido).
