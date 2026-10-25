# Asaas API v3: duvidas

`AsaasAdapter.validado_sandbox = False`. Nada rodou contra o sandbox nem a producao ainda: a
conta de sandbox (sandbox.asaas.com, cadastro livre, chave `$aact_hmlg_...`) esta a ser criada.
Quando a chave existir, o teste de integracao le `BRBANK_ASAAS_SANDBOX_API_KEY` e roda com
`uv run pytest -m sandbox tests/integracao`.

## Fontes

- Documentacao publica em https://docs.asaas.com (indice em `llms.txt`), lida em 2026-10-21.
- OpenAPI oficial: `https://www.asaas.com/openApi/document?version=3&languageCode=pt-BR`
  (1,1 MB). Quando guia e OpenAPI divergem, o adapter segue a OpenAPI e o ponto fica listado aqui.
- Nao ha modulo Odoo de origem nem SDK oficial em Python (`asaas-sdk` do PyPI e de terceiro,
  parado desde 2024, e nao foi usado).
- Analise completa, encaixe no Odoo e decisoes: `kmee-brain`, `kmee-produtos/brbank/ANALISE-ASAAS.md`.

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
| Checkout | `billingTypes` so cartao e Pix; `items[0]` com `imageBase64` (pixel transparente quando a configuracao nao traz imagem); `subscription.nextDueDate` enviado como `AAAA-MM-DD 00:00:00`; URL = `link` da resposta ou `.../checkoutSession/show?id=` | OpenAPI exige imagem e callback; o guia mostra data e hora no campo |
| Ligacao checkout -> assinatura | `subscription.checkoutSession` (resposta e webhook `SUBSCRIPTION_CREATED`) | a assinatura criada pelo checkout nao herda `externalReference` (nao documentado) |
| Alteracao | `PUT /subscriptions/{id}` so com os campos preenchidos; `value` e enviado apesar de nao constar no schema de atualizacao; `updatePendingPayments` quando `aplicar_nas_pendentes` | FAQ diz que o valor muda; a OpenAPI nao lista |
| Suspender e reativar | `status: INACTIVE`; `ACTIVE` exige `nextDueDate` novo | FAQ |
| Encerrar | `DELETE /subscriptions/{id}`; o Asaas remove as cobrancas pendentes e vencidas | FAQ |
| Cobrancas do ciclo | geradas pelo Asaas 40 dias antes do vencimento (14 ou 7 por configuracao da conta); chegam por `PAYMENT_CREATED` com `payment.subscription`, que vira `EventoCanonico.id_recorrencia`; `listar_cobrancas` pagina `GET /subscriptions/{id}/payments` sem buscar linha digitavel nem QR | FAQ e OpenAPI |
| Sandbox | `GET /subscriptions/{id}/paymentBook?month=&year=` antecipa a geracao dos ciclos | guia do sandbox |

## Duvidas abertas

1. **Corpo do 404**: a documentacao nao mostra o corpo de cobranca inexistente; o falso devolve
   `errors[{code: invalid_action}]`. O teste de contrato espera HTTP 404.
2. **`pixQrCode` em boleto sem chave Pix**: a documentacao diz apenas que sem chave o QR sai por
   instituicao parceira e vale ate 23:59 do dia (recurso a ser descontinuado). Conferir se a
   resposta e um QR de um dia, 400 ou 404.
3. **Fuso de `dateCreated`** nos eventos e de `paymentDate`/`clientPaymentDate`.
4. **Sandbox nao aplica juros, multa e desconto**: os campos sao aceitos mas nao exercitados; validar
   em producao com valor baixo.
5. **Antecedencia de geracao das cobrancas de assinatura** (40, 14 ou 7 dias) e configuracao da
   conta: onde fica e se a API a expoe. Relevante para a fase de recorrencia.
6. **`value` no `PUT /subscriptions/{id}`**: ausente no schema da OpenAPI, presente no FAQ.
7. **Checkout recorrente**: se a resposta ou o evento `CHECKOUT_PAID` traz `creditCardToken`.
8. **Retentativa de cartao recusado** no ciclo de uma assinatura: nao documentada.
9. **IPs do sandbox** para o webhook: a lista oficial e so de producao.
10. **URL do checkout no sandbox**: a documentacao so mostra a de producao
    (`https://asaas.com/checkoutSession/show?id=`); o adapter usa `link` da resposta quando vem.
11. **Formato de `subscription.nextDueDate` no checkout**: o guia mostra `2027-01-31 15:02:38` e o
    webhook devolve ISO com fuso; o adapter manda `AAAA-MM-DD 00:00:00`.
12. **`billingType` da assinatura criada pelo checkout** com mais de um meio oferecido: qual fica
    gravado e se as cobrancas seguintes respeitam a escolha do pagador.
