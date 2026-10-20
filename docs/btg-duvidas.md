# BTG Pactual (208) BTG Empresas, Banking Collections: duvidas em aberto

`BtgAdapter.validado_sandbox = False`. Nada foi executado contra o BTG.

## Fontes

- **Oficial (vale mais):** portal `developers.empresas.btgpactual.com`, lido em 2026-10-20.
  As paginas de referencia da API sao montadas no navegador; a especificacao OpenAPI de cada
  operacao vem embutida (comprimida) nos scripts do portal e foi extraida de la:
  - `docs/api-specs-target/Banking - Collections/criar-cobranca` (POST),
    `buscar-cobranca` (GET, JSON ou PDF), `atualizar-cobranca` (PUT),
    `cancelar-cobranca` (DELETE), `listar-cobrancas` (GET com filtros);
  - ficha tecnica `docs/tecspecs/Banking - Collections-spec` (escopos, tabela de status,
    eventos de webhook com exemplos de payload);
  - `docs/starting-doc/Produtos de API/Conta PJ/Cobrancas/boletos` (visao geral);
  - fluxos de autorizacao `docs/starting-doc/Integrando/Fluxo de autorizacao/` (BTG Id,
    Client Credentials, Refresh Token);
  - `docs/starting-doc/Integrando/Webhooks/utilize-webhooks-para-receber-atualizacoes...`
    (envelope `webhookId/event/data`, segredo no `Authorization`).
  - Host do token do sandbox (`id.sandbox.btgpactual.com`) confirmado no `config.js` do
    proprio portal e no ACBr.
- **Referencia LIDA (LGPL, nenhum codigo portado):** ACBr, `ACBrBoletoW_BTGPactual.pas`,
  `ACBrBoletoRet_BTGPactual.pas`, `ACBrBancoBTGPactual.pas` (CNAB) e `Particularidades`.
  O ACBr fala a rota antiga `v1/bank-slips?accountId=` (versao `v1.4`), com campos diferentes
  (`bankSlipId`, `ourNumber` + `ourNumberDigit`, `pixInfo`, `interests.arrears`). O adapter
  segue a spec atual (`/{companyId}/banking/collections`), nao o ACBr.

Antes de usar em producao: rodar a suite contra o sandbox (Wiremock com header
`x-response`) e, sobretudo, contra producao com um titulo real de valor baixo, porque o
sandbox devolve respostas fixas e nao exercita o estado.

## Contrato adotado

| Operacao | Chamada | Observacao |
| --- | --- | --- |
| Registrar | `POST /{cnpj}/banking/collections` | 201 com a cobranca; pode vir `PROCESSING` |
| Consultar | `GET /{cnpj}/banking/collections/{collectionId}` | `Accept: application/json` |
| PDF | mesma URL com `Accept: application/pdf` | |
| Alterar | `PUT /{cnpj}/banking/collections/{collectionId}` | titulo inteiro, 200 com a cobranca |
| Baixar | `DELETE /{cnpj}/banking/collections/{collectionId}` | 204 |

Hosts: `api.empresas.btgpactual.com` e `api.sandbox.empresas.btgpactual.com`. Token:
`id.btgpactual.com/oauth2/token` e `id.sandbox.btgpactual.com/oauth2/token`.
`companyId` = CNPJ da empresa (`BtgConfig.cnpj_empresa`). `id_banco` = `collectionId` (UUID).

## Duvidas

1. **Autorizacao (a maior).** A ficha diz que o token da API de Cobrancas so sai pelo
   Authorization Code, e a pagina de Client Credentials diz que esse fluxo "nao concede
   acesso ao uso das APIs de Banking". O fluxo interativo (login do cliente no BTG Id)
   fica fora da biblioteca: o chamador faz uma vez, guarda o `refresh_token` e o adapter
   renova com `grant_type=refresh_token` e Basic (`client_id:client_secret`), sem `scope`.
   O refresh token dura 10 dias e cada uso renova o prazo; parado mais de 10 dias, e preciso
   refazer o consentimento. A doc nao diz se o refresh_token muda a cada renovacao: o adapter
   aceita os dois casos e chama `BtgConfig.ao_renovar_refresh_token` quando vem um valor novo
   (o chamador PRECISA persistir, senao o proximo processo usa um token invalido). Confirmar.
2. **Escopos.** `openid` e obrigatorio; o de escrita e `brn:btg:empresas:banking:collections`.
   O de leitura aparece como `...collections.readonly` na ficha e `...collections.read-only`
   na spec. Como o escopo e fixado no consentimento (fora da biblioteca), o adapter nao manda
   `scope` na renovacao. Confirmar se a renovacao herda os escopos do consentimento.
3. **Status.** A consulta declara `CREATED, OVERDUE, PROCESSING, PAID, CANCELED, FAILED`;
   a ficha lista tambem `EXPIRED` ("vencida"), `UPDATED`, `UPDATING`, `CANCELING`,
   `CANCEL_FAILED` e `RETURNED` ("estornada"), sem dizer quando aparecem. Mapeados (tabela
   `2026.10`): CREATED e OVERDUE -> REGISTRADO, PROCESSING -> PENDENTE_REGISTRO, PAID ->
   LIQUIDADO, CANCELED -> BAIXADO, FAILED -> REJEITADO. Os outros viram `Validacao`.
   - `OVERDUE` nao tem descricao. Foi tratado como vencido e ainda nao pago nem cancelado
     (REGISTRADO); se significar "passou do `overDueDate`" e nao aceita mais pagamento, o
     certo seria BAIXADO. Confirmar.
   - `EXPIRED` ("vencida" na ficha, "expirada" no evento `collections.expired`): mesma
     duvida, por isso ficou fora.
   - `CANCELING`/`UPDATING` sugerem que baixa e alteracao terminam depois. O adapter devolve
     BAIXADO logo apos o 204 do DELETE (como os outros adapters); uma consulta logo em
     seguida pode encontrar `CANCELING` e levantar `Validacao`. Se confirmado, declarar
     `INSTRUCAO_ASSINCRONA` e mapear.
   - `RETURNED` ("estornada") pode ser devolucao de Pix pago: nunca mapear para LIQUIDADO.
4. **Valor e data do pagamento.** O schema da consulta NAO lista `amountPaid`, `paidAt` nem
   `settledAt`; eles aparecem nos exemplos de webhook (`collections.paid`), que trazem a
   mesma representacao da cobranca. O adapter le `amountPaid`/`paidAt` se vierem; se nao,
   `valor_pago` fica `None`. Liquidacao e D+1 (`settledAt`), pagamento e `paidAt`.
5. **Emissao assincrona.** O exemplo da spec responde `status: PROCESSING` na criacao, e a
   linha digitavel pode nao vir ainda. Por isso `EMISSAO_ASSINCRONA`: o registro devolve
   PENDENTE_REGISTRO e o chamador reconsulta. Criacao sem `status` tambem vira pendente.
6. **Expiracao obrigatoria.** `overDueDate` e obrigatorio e nao tem padrao documentado. Vem
   de `Titulo.dias_baixa_automatica` ou de `BtgConfig.dias_expiracao` (dias apos o
   vencimento); sem os dois a emissao e recusada antes de chamar o banco.
7. **Juros, multa e desconto.** Juros mensal -> `interest.type=PERCENTAGE_PER_MONTH` (a
   ficha confirma: valor mensal, cobrado por dia corrido); multa -> `fine.type=PERCENTAGE`;
   inicio de ambos = vencimento + 1 dia (escolha do adapter). Desconto percentual ->
   `discounts[{type: PERCENTAGE, limitDate: vencimento - dias}]`. Sem encargo, juros e multa
   vao como `NOT_APPLICABLE` com valor 0 (formato dos exemplos de webhook).
8. **Abatimento.** No registro e na alteracao vai em `rebate` (`FIXED_VALUE`). A spec diz que
   precisa ser maior que 0 e menor que o valor da cobranca; o adapter nao confere.
9. **Alteracao.** O PUT exige `dueDate`, `overDueDate`, `amount`, `discounts`, `interest` e
   `fine`. O adapter consulta, troca o campo e reenvia o resto como veio. Na prorrogacao,
   `overDueDate` e o inicio de juros e multa andam o mesmo numero de dias que o vencimento;
   datas de desconto ficam como estavam. Confirmar se o PUT aceita `discounts` com item
   `NOT_APPLICABLE` (o adapter manda assim quando a consulta nao traz descontos).
10. **Protesto e negativacao.** Existem como `detail.protest` e `detail.badCredit`
    (`APPLICABLE` + data), no registro e no PUT, e ha endpoints de negativacao em lote. O
    modelo canonico nao traz a data, entao `PROTESTO` e `NEGATIVACAO` levantam `NaoSuportado`.
11. **Webhook.** Envelope `{webhookId, event, data}` e `data` = cobranca. Eventos:
    `collections.created`, `collections.paid`, `collections.expired` e `collections.cancelled`
    (a ficha grafa `colections.cancelled` na tabela; aceitamos as duas grafias). O BTG manda o
    segredo do webhook em `Authorization: Bearer <segredo>`; com `BtgConfig.webhook_secret` o
    adapter confere (comparacao em tempo constante). Sem segredo configurado, nao ha
    autenticacao: reconsulte sempre. `collections.expired` vira `Validacao` (ver item 3). O
    cadastro do webhook e feito pela API de Webhooks (token Client Credentials), fora daqui. A
    doc avisa que webhook no sandbox ainda nao esta disponivel para todos.
12. **Sacador avalista.** A spec nao tem campo para `Titulo.beneficiario`: e ignorado.
13. **Tamanhos.** `detail.documentNumber` (recebe `seu_numero`) tem no maximo 15 caracteres e
    `description` (mensagens unidas por espaco) 300: acima disso, `Validacao`. Endereco do
    pagador e cortado no tamanho da spec (rua 40, numero 10, bairro 20, cidade 50); CEP vai
    com 8 digitos sem hifen (a rota antiga do ACBr usava hifen). Endereco e opcional na spec:
    sem endereco, o campo nao vai.
14. **Idempotencia.** O adapter nao manda `correlationId` (maximo 20) nem `ourNumber` (o banco
    gera). Para achar um titulo apos falha de rede, `listar-cobrancas` filtra por
    `documentNumber`; nao implementado. Duplicidade nao tem codigo de erro conhecido, logo nao
    vira `Duplicado`.
15. **Erros.** 400 traz `invalidParams[{name, reason, value}]`; 422 e 500 trazem
    `Message` + `Details{brn, name, reason}`. O transporte mascara e repassa sem interpretar.
    `mascarar` nao conhece as chaves em ingles (`name`, `taxId`, `street`): o adapter limpa
    `payer`, `payee`, `address`, `pixKey` e afins das respostas que guarda, mas um corpo de
    erro que ecoe o nome do pagador passa pelo `mascarar` comum, que so pega CPF/CNPJ no texto.
    Recomendado incluir as chaves em ingles no `mascara.py` (afeta todos os adapters).
16. **CNPJ alfanumerico.** `taxId` vai normalizado, com letras se houver; nao se sabe se a API
    aceita. O `companyId` da URL tambem.
17. **Cota.** Limite de requisicoes nao documentado: `requisicoes_por_segundo=None`.
18. **mTLS.** A doc nao pede certificado de cliente para Banking; o adapter nao usa. Confirmar.
19. **Hibrido.** `HIBRIDO` e declarada fixa, mas so vale com `BtgConfig.hibrido=True` (padrao,
    `type=BANKSLIP_QRCODE`). O copia-e-cola vem em `detail.emv`; chave Pix e `txId` sao
    opcionais (sem chave o BTG usa uma EVP) e o adapter nao os envia.
20. **Fora do escopo.** Pix Cobranca (`DUE_DATE_QRCODE`, `IMMEDIATE_QRCODE`), Pix Automatico
    (jornada 4), split, lote (`criar-cobrancas-em-lote`, `batchId`), envio por SMS/e-mail
    (`deliveryMediums`) e a listagem paginada.
