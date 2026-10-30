# Santander (033) Hub de Pagamentos: fontes e duvidas em aberto

`SantanderPagamentoAdapter.validado_sandbox = False`. Nada foi executado contra o Santander:
o sandbox exige aplicacao cadastrada no portal e certificado, e nao ha credencial.

## Fontes (lidas em 30/10/2026, paginas publicas, sem login)

O portal developer.santander.com.br e uma aplicacao de pagina unica atras de WAF: `curl` e
navegador comum levam 403. Com navegador automatizado a pagina abre, e os documentos vem em
JSON de `https://cross.api.santander.com.br/publicly_accessible_content/v2/publicly_accessible_content?id=<id>`
(chamada anonima que a propria pagina faz; fora dela responde 401).

1. **OpenAPI oficial:** "Payment Hub" 1.1.7, arquivo `vx-hub-de-pagamentos_en (3).yaml`, id
   `0682c31f-4531-4d49-8f2d-0e763d659fa2` (sha256 do YAML
   `85f219aea5f3d095fa2ee6e4c3c96e7c2f7e46f4829584f8c31f58ac5426612a`). A pagina
   de TED e a de contas e tributos trazem o mesmo arquivo com outros ids.
2. **Guias funcionais oficiais** ("Documentation updated: October 27, 2026"):
   - https://developer.santander.com.br/api/documentacao/transferencias-pix-visao-geral
   - https://developer.santander.com.br/api/documentacao/pagamentos-contas-e-tributos-visao-geral
   - https://developer.santander.com.br/api/documentacao/transferencias-ted-visao-geral
3. **Colecoes Postman oficiais** (`collection_pagamentos_sand.json`,
   `collection_pagamentos_prod.json`), na secao de documentos das mesmas paginas. Nao tem TED.
4. **Terceiro, so como segunda fonte:** `github.com/adrianotrentim/bancobr-net`, pasta
   `src/BancoBr.API/Santander`. Confirma o fluxo POST + PATCH, o `debitAccount` explicito ("sem
   isso o banco debitaria a conta principal"), a conta com DV em 12 posicoes, `finalPayer` e a
   finalidade da TED por extenso. Nenhum codigo portado.

## Confirmado na fonte oficial

| Item | Valor | Onde |
|---|---|---|
| Hosts | `trust-open` (producao), `trust-sandbox` | OpenAPI, Postman, guia Access Token |
| Base | `/management_payments_partners/v1` | OpenAPI |
| Token | `POST /auth/oauth/v2/token`, formulario `client_id`, `client_secret`, `grant_type` | guia Access Token |
| mTLS | token amarrado ao certificado, 15 minutos | guia mTLS |
| Cabecalho | `X-Application-Key: client_id` | guia Access Token, OpenAPI |
| Workspace | `PAYMENTS` para pagamento proprio; Pix e TED nao existem em `DIGITAL_CORBAN` | guias Onboarding e Create Workspace |
| Boleto | `POST`/`PATCH`/`GET .../bank_slip_payments[/{id}]` | OpenAPI, guia |
| Guia e concessionaria | `.../barcode_payments[/{id}]` | OpenAPI, guia |
| Pix (chave, dados, QR) | `.../pix_payments[/{id}]` | OpenAPI, guia |
| TED | `.../transfer[/{id}]` | OpenAPI, guia |
| Dois passos | POST valida, PATCH `status: AUTHORIZED` efetiva | guia Transactional View |
| Status | `READY_TO_PAY`, `PENDING_CONFIRMATION`, `PAYED`, `SCHEDULED`, `REJECTED`, `REFUNDED`; `STARTED`, `PENDING_VALIDATION`, `AUTHORIZED` | guia Transactional View, enums, tabelas de erro |
| Agendamento | so em barcode | guia Transactional View |
| `id` do cliente | opcional, UUID v4; repetido da erro `30082` (boleto, guia), `40053` (Pix), `20002` (TED) | guias, tabelas de erro |
| Boleto repetido | novo POST com os mesmos dados descarta o anterior | guia Initiate boleto |
| `finalPayer` | obrigatorio no PATCH de boleto e guia; repete o titular do workspace | guia Complete boleto |
| Erro | `_errorCode`, `_message`, `_details`, `_timestamp`, `_traceId`, `_errors[{_code,_field,_message}]` | guias |
| Pix por dados | `bankCode` ou `ispb`, nunca os dois | guia Initiate Pix |
| TED | ISPB so em conta de pagamento (`20062`), nome do favorecido obrigatorio; texto acima de 10 milhoes | tabelas de erro, guia |
| Devolucao | TED paga pode virar `REJECTED` em ate 3 dias uteis; boleto vira `REFUNDED` | guias TED e Transactional View |

## Decisoes do adapter sobre o que a fonte deixa aberto

1. **Autorizacao em `autorizar_lote`, nao so em `liberar_lote`.** O lote nao existe no banco e o
   PATCH precisa de rota, id e valor de cada linha. `liberar_lote(id)` do contrato so tem o numero:
   funciona apenas na instancia que enviou. Num fluxo com aprovacao depois (outro processo), use
   `autorizar_lote(LoteRegistrado, conta_debito)`. Ver "Mudanca sugerida no contrato".
2. **`id` deterministico.** UUID no formato v4 derivado de `workspace:chave_idempotencia`, com
   SHA-256. A doc pede "UUID v4", e o banco pode validar o formato; nao se sabe se valida. A doc nao
   diz por quanto tempo o id fica ocupado.
3. **Id repetido vira consulta**, para os codigos `30082`, `30088`, `40053`, `20002`, `20064`. Se a
   consulta da 404 (transacao registrada com outro id), a linha e `RECUSADO`.
4. **PATCH recusado (4xx) vira consulta:** a verdade da linha e o que o GET diz, com os motivos do
   erro. Saldo insuficiente deixa `ACEITO` e pode ser autorizado de novo. A doc nao diz em que estado
   a recusa deixa o pagamento: pode vir `REJEITADO`, e a consulta mostra.
5. **PATCH 2xx com `READY_TO_PAY` vira `INDETERMINADO`.** O texto do guia diz que o PATCH devolve
   `PENDING_CONFIRMATION`, `PAYED` ou `REJECTED`, mas o diagrama de "sucesso no PATCH" mostra
   `READY_TO_PAY`; o outro diagrama, com os mesmos estados, mostra `SCHEDULED` e `PAYED`. Parece erro
   do desenho. Perguntar.
6. **POST sem resposta e `INDETERMINADO`,** embora o POST nao mova dinheiro: o estado no banco e
   desconhecido, e o `id_banco` ja vem preenchido para consultar. Reenviar a mesma linha e seguro
   (vira consulta). 429 significa nao processado: `RECUSADO`/`NAO_ENVIADO` no POST, `ACEITO`/
   `NAO_ENVIADO` no PATCH.
7. **Data:** fora de guia, data diferente de hoje e recusada sem chamar o banco. Pix por chave ou
   dados e TED nao levam data, e sairiam na hora.
8. **Conta de debito sempre explicita.** Sem `debitAccount` o banco debita a conta principal, e
   o contrato traz a conta do lote. Agencia com 4 digitos, conta com DV e zeros ate 12
   (`conta_com_digito=False` tira o DV).
9. **Conferencia do beneficiario** contra `beneficiary` e `finalBeneficiary` da resposta do POST,
   ignorando zeros a esquerda (o exemplo do OpenAPI traz CPF com 14 posicoes). Documento diferente nao
   e autorizado. Vale para boleto e Pix por chave com `documento`.
10. **QR com encargos:** com `pagar_com_juros_multa`, autoriza o `totalValue` do banco; sem ele, total
    diferente do valor vira `RECUSADO`/`VALOR_DIVERGENTE`. Boleto e guia nao tem essa trava: o banco
    recusa valor errado (`30053`, `30149`), e boleto pode aceitar pagamento parcial
    (`partialPayment`).
11. **Valor em texto** (`"80.80"`) em todo `paymentValue`: o guia aceita texto ou numero no boleto e
    na guia, e o OpenAPI pede texto no Pix. TED em numero ate 10 milhoes, como no OpenAPI.

## Divergencias dentro da fonte oficial

| Campo | Uma fonte | Outra | Adotado |
|---|---|---|---|
| ISPB do beneficiario | `isbp` (schema) | `ispb` (guia, exemplos) | `ispb` |
| Tipo de chave celular | `CELL PHONE` (guia traduzido) | `CELULAR` (enum) | `CELULAR` (terceiro confirma) |
| Tipo de conta | `CHECK_ACCOUNT` (guia, resposta) | `CONTA_CORRENTE` (enum) | `CONTA_CORRENTE` no envio; a resposta nao e lida |
| Finalidade da TED | `PAGAMENTO_DE_FORNECEDORES-5` (enum) | sem sufixo (exemplos, terceiro); em ingles (tabela de erro) | sem sufixo, em `ted_finalidade` |
| `status` no PATCH da TED | enum sem `AUTHORIZED` (schema) | `AUTHORIZED` (guia) | `AUTHORIZED` |
| Obrigatorios da TED | `ispbCode` obrigatorio (schema) | so em conta de pagamento (guia, exemplos) | guia |
| `finalPayer` no workspace PAYMENTS | "repete o titular" (guia) | erro `30076`, "nao pode ser igual ao do workspace" | guia; o erro deve ser de Corban. Perguntar |
| Homologacao | `trust-open-h` (so em `servers` do OpenAPI) | sem token nem colecao | fora do adapter |

## O que falta (quem pergunta: suporte do portal ou gerente do Santander)

1. **Webhook:** configuracao pelo workspace (`webhookURL`), mas o guia so mostra o fluxo. Sem formato
   de notificacao nem assinatura. `WEBHOOK` nao declarada; estado por `consultar_pagamento`. Falta a
   especificacao da notificacao.
2. **Cancelamento:** o guia permite cancelar `SCHEDULED`, mas o OpenAPI nao tem rota (o terceiro diz
   "nao ha cancelamento"). `CANCELAMENTO` nao declarada. Falta a rota.
3. **Formato do telefone na chave Pix:** o adapter manda `+55DDDNUMERO`, o formato do DICT. Nenhum
   exemplo oficial.
4. **CNPJ alfanumerico:** `documentNumber` e `number` no OpenAPI e texto nos exemplos do Postman. O
   adapter manda texto. Nao se sabe se o banco aceita letras.
5. **Conta com zeros:** o guia manda completar, e os exemplos trazem os dois jeitos (`000000000001` e
   `130375431`). Se o banco comparar como texto com o que esta no workspace, responde `30056`, sem
   pagar nada.
6. **Conta de pagamento:** o modelo nao tem esse tipo. Na TED, `conta_credito` com `ispb` e `compe`
   vira `PG`. Pix por dados bancarios sai como corrente ou poupanca; `CONTA_PAGAMENTO` nao e enviado.
7. **Nome do favorecido na TED:** obrigatorio no banco, e `Pagamento` nao tem o campo. Por enquanto vem
   de `nome_favorecido_ted` na config; sem ele a linha e recusada sem chamar o banco.
8. **`PENDING_VALIDATION` e `STARTED`:** aparecem nos filtros e nas tabelas de erro, nao no guia de
   status. Mapeados para `EM_PROCESSAMENTO`; `autorizar_lote` so autoriza `ACEITO`, entao um POST
   nesse estado pede `consultar_pagamento` antes.
9. **Cota:** existe 429, sem numero publicado. O guia avisa que muitos POST de Pix sem PATCH podem ser
   lidos como varredura e bloqueados: nao usar o POST como consulta de chave.
10. **Horarios:** boleto ate 23:50 (Santander 23:59), arrecadacao ate 23:00, TED em dias uteis das 6:30
    as 16:55; tributo federal nao paga em feriado nem fim de semana. O banco recusa; o adapter nao
    confere.
11. **Fora do adapter, por decisao de escopo:** tributo por campos (`taxes_by_fields_payments`),
    veiculos, Transferencia Inteligente e DDA (`available_bank_slips`) existem no mesmo OpenAPI.

## Mudanca sugerida no contrato (fora deste PR)

- `Pagamento.favorecido_nome` (TED): tira `nome_favorecido_ted` da config.
- `liberar_lote` receber o `LoteRegistrado` e devolver o estado por linha: e o que um banco sem lote
  com passo de autorizacao precisa. Enquanto isso, `autorizar_lote` existe so neste adapter.
