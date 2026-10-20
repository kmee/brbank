# Santander (033) API Gestao de Boletos v2: duvidas em aberto

`SantanderAdapter.validado_sandbox = False`. Nada foi executado contra o Santander.

## Fontes

Por ordem de confianca:

1. **Doc oficial (developer.santander.com.br, "API de Cobranca / Gestao de Boletos"):**
   NAO foi possivel ler. O WebFetch levou HTTP 403 e o `curl` so devolve a casca do
   portal (pagina montada por JavaScript, titulo "Developer Portal - Santander").
   Tudo abaixo precisa ser conferido contra ela e contra o ambiente de teste.
2. **Leitura do ACBr (LGPL, so como documentacao, nenhum codigo portado):**
   `WS/ACBrBoletoW_Santander_API.pas`, `WS/ACBrBoletoRet_Santander_API.pas`,
   `ACBrBancoSantander.pas` e a secao 3.17 de `Particularidades.html`. Dela vem: hosts,
   URL do token, formulario do token, header `X-Application-Key`, caminhos
   (`/workspaces/{id}/bank_slips`, `/bills`), campos do registro e da resposta, PATCH de
   instrucao (`operation`, `dueDate`, `deductionValue`), tipos de consulta
   (`default`, `duplicate`, `bankslip`, `settlement`, `registry`), situacoes `ATIVO`,
   `LIQUIDADO`, `BAIXADO`, a regra "BAIXA DE PAGAMENTO VIA PIX" e o prefixo `TST` do
   `nsuCode` em homologacao.
3. **Terceiros:**
   - `matfatjoe/api-santander` (PHP, GitHub, lido via `gh api`): token com mTLS em
     `/auth/oauth/v2/token`, `X-Application-Key` em toda chamada, registro e PATCH no
     workspace, consulta "sonda" (`/workspaces/{id}/bank_slips/{nsuCode}.{nsuDate}.{environment}.{covenantCode}.{bankNumber}`,
     confirma o registro ate D+2), `/bills/{convenio}.{nossoNumero}?tipoConsulta=` e
     criacao de workspace (`type=BILLING`, `covenants[{code}]`, `description`,
     `bankSlipBillingWebhookActive`, `pixBillingWebhookActive`, `webhookURL`).
   - `flycorp/santander-billet` (README no Packagist): mesmos caminhos; PDF por
     `POST /bills/{id}/bank_slips` com `payerDocumentNumber`.
   - Wiki CIGAM "Open Banking para Banco Santander": requisitos da URL de webhook, baixa
     so 1 minuto apos o registro, so protesto (sem negativacao), CNPJ alfanumerico aceito,
     ambiente de teste so registra.
   - Forum ACBr, topico 80305 (workspace so por Postman, sandbox instavel, testar em
     producao com `environment` de teste).

## Duvidas

1. **Webhook (o maior ponto em aberto).** O workspace aceita `webhookURL` e liga o aviso
   de boleto (`bankSlipBillingWebhookActive`). A URL precisa aceitar GET (cadastro) e POST
   (vazio e JSON) e responder 200 na hora (CIGAM). O **formato da notificacao nao e
   publico** em nenhuma fonte lida. Por isso `WEBHOOK` nao e declarada e `parse_webhook`
   levanta `NaoSuportado`: liquidacao por polling (`consultar_titulo`), como no PRD. Sem
   assinatura conhecida: quando o contrato aparecer, o evento so serve de gatilho para
   reconsultar, nunca para liquidar.
2. **Workspace.** O adapter recebe `workspace_id` pronto e nao cria workspace (o
   formato da resposta de `POST /workspaces`, inclusive o nome do campo do id, nao foi
   visto). Um workspace por convenio; o convenio da config deve ser o do workspace.
3. **Ambientes.** `producao`: host `trust-open`, `environment=PRODUCAO`. `teste`: o
   mesmo host com `environment=TESTE` e `nsuCode` com prefixo `TST` (ACBr); so registra,
   sem alteracao nem retorno (CIGAM). `sandbox`: host `trust-sandbox`, que o proprio
   Santander considera instavel e que nao devolve tudo no registro. No sandbox o ACBr
   manda `environment=PRODUCAO` e o `matfatjoe` manda `TESTE`; o adapter manda `TESTE`
   (nunca marca como producao fora de producao). Confirmar.
4. **Escopo do token.** Nenhuma fonte manda `scope`; o adapter nao manda. Credenciais no
   formulario (`client_id`, `client_secret`, `grant_type`), como no ACBr.
5. **Nosso numero.** Quem registra escolhe o `bankNumber` (`gerar_nosso_numero` ou
   `seu_numero` numerico). Tamanho: 12 digitos sem DV, pela regra do ACBr para o
   Santander (`VersaoDF = V1` na API: nao calcular DV). Nao se sabe se o banco devolve o
   numero com DV: o adapter usa o `bankNumber` da resposta quando vem. No sandbox o ACBr
   preenche com zeros ate 8 digitos na consulta; o adapter nao faz isso.
6. **`nsuCode`.** O ACBr usa o proprio nosso numero (com `TST` no teste). Seguido. Se o
   Santander exigir NSU unico por requisicao (e nao por titulo), um reenvio do mesmo
   titulo pode ser recusado com outro codigo que nao 409.
7. **Duplicidade.** O codigo HTTP de titulo repetido e desconhecido. Se for 409, o
   adapter devolve `Duplicado` com `id_banco` (o nosso numero enviado) e o orquestrador
   reconsulta. Se vier 400 com `_errors`, vira `Validacao`.
8. **Registro assincrono.** O ACBr le linha digitavel e codigo de barras na resposta do
   POST (201), mas avisa que o sandbox nao devolve tudo, e a "sonda" confirma o registro
   ate D+2. O adapter declara `EMISSAO_ASSINCRONA`: sem linha nem codigo de barras, o
   titulo volta `PENDENTE_REGISTRO`. A sonda nao foi implementada.
9. **Corpo da resposta.** Objeto (terceiros) ou lista (o ACBr junta respostas em lista).
   O adapter aceita objeto, lista ou `_content` paginado e usa o primeiro item.
10. **Consulta em ate tres chamadas.** Nenhum endpoint traz tudo (comentario do ACBr,
    "conversado com o banco"): `/bills?beneficiaryCode&bankNumber` (situacao e
    `paidValue`), `tipoConsulta=bankslip` (linha digitavel, codigo de barras e
    `qrCodeData.qrCode`, so quando ha chave Pix) e `tipoConsulta=settlement` (so quando
    `LIQUIDADO` ou `BAIXADO`). O ACBr remove zeros a esquerda do nosso numero no
    `bankslip` e nao no `settlement`; o adapter usa o numero como esta. O nome do bloco
    aparece como `bankSlipData` (ACBr) e `bankslipData` (terceiro): os dois sao lidos.
11. **Status.** Mapeados so `ATIVO`, `LIQUIDADO` e `BAIXADO`. Qualquer outro (protesto,
    cartorio, pagamento parcial) vira `Validacao` ate haver lista oficial.
12. **Pix pago como baixa.** Boleto hibrido pago por Pix aparece `BAIXADO` com
    `writeOffDescription = "BAIXA DE PAGAMENTO VIA PIX"` (ACBr; no CNAB e o motivo 510).
    O adapter traduz para `LIQUIDADO`. Confirmar o texto exato.
13. **Valor e data do pagamento.** Valor: `paidValue` da consulta; na falta,
    `settlementCreditedValue` e depois `writeOffValue`. O ACBr prefere o creditado, mas
    ele parece ser liquido da tarifa (`settlementDutyValue` ao lado): para conciliar o
    titulo vale o pago. Data: `settlementDate`, depois `writeOffDate`. Confirmar.
14. **Desconto.** As fontes so mostram tipos em valor (`ISENTO`, `VALOR_DATA_FIXA`,
    `VALOR_DIA_CORRIDO`, `VALOR_DIA_UTIL`). O percentual do modelo canonico vira valor
    fixo sobre o nominal (arredondado ao centavo) com `limitDate` = vencimento - dias.
    Se existir tipo percentual na API, trocar.
15. **Multa e juros.** `finePercentage` (so percentual) e `fineQuantityDays`: o adapter
    manda 1 (multa a partir do dia seguinte ao vencimento, como nos outros bancos); nao se
    sabe se 0 ja significa isso. Juros: `interestPercentage` mensal; sem encargo, "0.00".
16. **Baixa automatica.** `dias_baixa_automatica` vira `writeOffQuantityDays` (o ACBr
    calcula a mesma diferenca em dias a partir do vencimento).
17. **Instrucoes.** `PATCH /workspaces/{id}/bank_slips` com `covenantCode`, `bankNumber` e
    o campo da alteracao: `dueDate` (prorrogacao), `deductionValue` (abatimento),
    `operation=PROTESTAR` e `operation=BAIXAR`. Negativacao nao existe (CIGAM). O estado
    volta da reconsulta. Nao se sabe se o PATCH e processado na hora: se nao for,
    `INSTRUCAO_ASSINCRONA` deve entrar. A baixa so e aceita 1 minuto apos o registro
    (CIGAM); antes disso o banco recusa e vira `Validacao`.
18. **PDF.** Existe `POST /bills/{convenio}.{nossoNumero}/bank_slips` com o documento do
    pagador, mas o formato da resposta (link ou base64) nao foi visto. `PDF` nao e
    declarada.
19. **Tamanhos.** Nome 40, endereco 40, bairro 30, cidade 20 (o ACBr trunca; o adapter
    tambem). `clientNumber` (seu numero) e `messages` vao sem limite conhecido.
20. **CPF/CNPJ.** Sem mascara, `documentType` CPF ou CNPJ; CNPJ alfanumerico aceito
    segundo o CIGAM. CEP no formato `00000-000`.
21. **PII na resposta.** As chaves do Santander sao em ingles (`payer`, `name`,
    `documentNumber`...) e o `mascarar` comum so conhece nomes em portugues: o adapter
    troca esses blocos por `***` antes de guardar a resposta bruta. Corpo de erro (que
    pode ecoar dados enviados) passa so pelo `mascarar` comum do transporte.
22. **Cota.** Limite de requisicoes desconhecido; `requisicoes_por_segundo` sem padrao.
23. **Erros.** Corpo `{_errorCode, _message, _errors[{_code, _field, _message}]}` (ACBr);
    nao interpretado, o transporte mascara e repassa.
