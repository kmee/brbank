# Santander (033) API de Cobranca / Gestao de Boletos v2: fontes e duvidas

`SantanderAdapter.validado_sandbox = False`. Nada foi executado contra o Santander: nao ha
credencial. Tudo abaixo vem da leitura da documentacao, com o servidor falso de
`tests/santander_falso.py` seguindo as mesmas regras.

## Fontes oficiais (lidas em 31/10/2026, paginas publicas, sem login)

O portal e uma aplicacao de pagina unica atras de WAF: `curl` e navegador comum levam 403. Com
navegador automatizado a pagina abre e os documentos vem em JSON de
`https://cross.api.santander.com.br/publicly_accessible_content/v2/publicly_accessible_content?id=<id>`
(chamada anonima que a propria pagina faz). Pagina:
https://developer.santander.com.br/api/documentation/issuance-of-bills ("Documentation updated:
October 26, 2026"; com o navegador em pt-BR vem o texto original em portugues, que prevalece sobre
o ingles, uma traducao automatica).

1. **OpenAPI:** "API de Cobranca Santander" 1.0.0, `original.yaml`, id
   `7409706a-84d4-41a9-8e24-f43d7b8082cf`, sha256
   `b787c244084c559f06cf894b59085d1b836965c39cb7ea2868401fdaa0bc6842`.
2. **Guia funcional em 30 secoes**, ids no menu da pagina. As mais usadas: Registro
   `2d43d8c9-13f9-4c26-b716-017034833ddf`, Instrucoes `59e31846-44af-4a7b-8606-a3364c3f4191`,
   Consulta detalhada `02b795b6-a65e-4d9d-b1b2-6bf7e66ebeda`, PDF
   `c1d29d7d-56d5-47f3-a7e5-ae8e89ca8003`, Webhook `732d3b63-949b-427f-86c2-0674a05a3189`, mTLS do
   webhook `fea1d9a3-5451-4b91-88c2-81f58b0687f5`, Notas do POST
   `f44d097e-12e9-45ee-b8d3-8cc29962f72e`, Notas do PATCH `3b659dca-5513-494b-85dc-21c52a0a4bd9`,
   Como se integrar `768424c6-9ffe-4115-8440-6a41da7e696b`.
3. **Colecoes Postman:** `collection_api_emissao_boletos_sand.json` (id
   `7c47d6c9-7f87-432c-ae68-256158584dd6`) e `collection_api_emissao_boletos_prod.json` (id
   `aaa1cb04-73a6-4794-9fa3-6b8a4169175b`).

## Confirmado na fonte oficial

| Item | Valor | Onde |
|---|---|---|
| Hosts | `trust-open` (producao), `trust-sandbox` | OpenAPI, Postman, Visao Setup |
| Base | `/collection_bill_management/v2` | OpenAPI |
| Token | `POST /auth/oauth/v2/token`, formulario `client_id`, `client_secret`, `grant_type`, sem `scope` | Access Token, Postman |
| mTLS | token de 15 minutos, amarrado a impressao digital do certificado | guia mTLS |
| Cabecalho | `X-Application-Key: client_id` em tudo, menos no token | guia de erros, OpenAPI |
| Workspace | `POST /workspaces` com `type=BILLING` e `covenants[{code}]`; resposta 201 com `id` e `status`. Convenio de 7 digitos, no mesmo formato em todas as chamadas | Criacao de Workspace |
| Registro | `POST /workspaces/{id}/bank_slips`; responde com tudo o que foi enviado mais `barcode`, `digitableLine`, `entryDate` e, com chave Pix, `qrCodePix`, `qrCodeUrl` | guia, OpenAPI |
| Registro sincrono | a linha digitavel vem na resposta do POST; a sonda serve para confirmar um registro, nao para esperar. `EMISSAO_ASSINCRONA` saiu | guia |
| Instrucao sincrona | "processadas de forma online"; so cobranca caucionada vai em grades, e a API so registra na carteira simples. `INSTRUCAO_ASSINCRONA` nao entra | guia de instrucoes, Entenda o Produto |
| `nsuCode` | ate 20; unico por data e convenio; TESTE exige prefixo `TST`, PRODUCAO exige numerico. Em TESTE o numero fica queimado no dia | Nota 1, Como se integrar, erros 1081 e 1082 |
| `bankNumber` | numerico, ate **13** digitos, sem DV na API, diferente de zero | Nota 8, erro 1043 |
| `clientNumber` | ate 15, so letras, numeros e espaco (11 se houver protesto) | Notas 9 e 42, OpenAPI |
| Nome, endereco, bairro, cidade | 40, 40, 30, 20 | tabela do POST |
| `documentKind` | 12 valores | Nota 11, OpenAPI |
| Desconto | so em valor (`ISENTO`, `VALOR_DATA_FIXA`, `VALOR_DIA_CORRIDO`, `VALOR_DIA_UTIL`): nao existe percentual. Ate 3 datas crescentes, sem passar do vencimento | Nota 12 |
| Multa | `finePercentage` ate 60%; `fineQuantityDays` = dias apos o vencimento em que comeca | tabela do POST |
| Juros | `interestPercentage` mensal, ate 60% | tabela do POST |
| Baixa automatica | `writeOffQuantityDays`: 0 ou ausente = perfil do convenio; 1 a 98 = dias; 99 = nao baixar | Nota 14 |
| Mensagens | ate 45 textos de 100 | Nota 26, erros 1022 e 1023 |
| Chave Pix | `CPF`, `CNPJ`, `CELULAR`, `EMAIL`, `EVP`; a chave tem de estar no Santander, no mesmo CNPJ do convenio | Notas 23 e 24, OpenAPI |
| `txId` | opcional, 26 a 35; sem ele o banco gera | Nota 25 |
| Beneficiario final | so quando e outro: nao pode ter o documento nem a raiz do CNPJ do beneficiario original | Nota 7, erros 00491 e 00494 |
| Nosso numero repetido | codigo `0001` em corpo de erro; o guia so lista 400 | Regras de produto |
| Corpo de erro | `_errorCode`, `_message`, `_details`, `_timestamp`, `_traceId`, `_errors[{_code,_field,_message}]` | guia, OpenAPI |
| Instrucoes | `PATCH` com `covenantCode`, `bankNumber` e so o campo alterado. `operation` em `BAIXAR`, `PROTESTAR`, `CANCELAR_PROTESTO`, sempre sozinha; ate 10 outras juntas | guia, Nota 31 |
| Prorrogacao | `dueDate`, adianta ou estende; reflete no QR Code | Nota 29 do PATCH |
| Abatimento | `deductionValue`; zero cancela; somado ao desconto, tem de ficar abaixo do nominal | Nota 33 |
| Protesto | so de boleto vencido; cancelar depende do cartorio. Nao ha negativacao na API | Nota 31 |
| Consultas | `/bills?beneficiaryCode&bankNumber`; `/bills/{convenio}.{nosso}?tipoConsulta=default, duplicate, bankslip, settlement, registry`; sonda ate D+2 em `bank_slips/{nsu}.{data}.{P ou T}.{convenio}.{nosso}` | guia, OpenAPI |
| Status | `Ativo`, `Baixado`, `Liquidado`, `Liquidado Parcialmente` (a resposta vem em caixa mista) | guia, exemplos |
| Pagamento de hoje | linha digitavel ou barras: `Ativo` com complemento "Baixa Operacional"; `Liquidado` so em D+1 | guia |
| Pagamento por Pix | `Baixado` na hora, com indicacao da causa nas consultas por nosso numero, por seu numero e settlement | guia |
| Retencao | baixado sai da consulta em 15 a 30 dias; liquidado, em 30 a 45 dias | guia |
| Campos de pagamento | `paidValue`, `settlementValue`, `settlementCreditedValue`, `settlementDutyValue`, `writeOffValue`, `writeOffDescription` | exemplos do OpenAPI |
| PDF | `POST /bills/{nosso}.{convenio}/bank_slips` (ordem inversa da consulta) com `payerDocumentNumber` em numero; resposta `{link}`, valido por 5 minutos, proibido expor ao cliente final; titulo ativo; so producao | guia, telas, Postman de producao |
| Webhook, formato | `WBHKPAGEST`, `function` em `PAGAMENTO` ou `ESTORNO`, `bankNumber` e `covenant` completados com zeros, `payedValue` em numero | guia em portugues |
| Webhook, validacao da URL | POST com cabecalho `notification-uuid` e `{"message":"Validacao da URL do Webhook"}` (acentuada no original); tem de responder 2xx | guia |
| Webhook, duplicatas | pode repetir; tratar pelo `notification-uuid` | guia de mTLS |
| Webhook, autenticidade | **sem assinatura**; mTLS opcional, conferido no servidor que recebe (cadeia Sectigo) | guia de mTLS |
| Webhook e conciliacao | notificacao chamada de confirmacao efetiva, mas "a conciliacao sempre deve ser complementada" pelo GET ou arquivo de retorno; pago em cartorio nao gera aviso | guia |
| Ambiente TESTE | so no host de producao; valida e responde, nao registra, nao consulta, nao aceita instrucao, sem tarifa | Como se integrar |
| Sandbox | dados ficticios; colecao oficial manda `environment: PRODUCAO` e sonda com `P` | Postman do sandbox |

## Vem de terceiro (nao oficial)

| Item | Fonte |
|---|---|
| Texto `BAIXA DE PAGAMENTO VIA PIX` em `writeOffDescription`. O guia so diz "pagamento via PIX": o adapter procura esse trecho | ACBr, `ACBrBoletoRet_Santander_API.pas` (LGPL, so leitura; nenhum codigo portado): https://www.projetoacbr.com.br/forum/topic/80305-api-santander-rotas-workspace-e-rota-de-consulta-de-boletos/ |
| `qrCodeData.qrCode` em `tipoConsulta=bankslip`. Nao esta no OpenAPI: o adapter le se vier; a fonte oficial e a sonda | ACBr, idem; https://github.com/matfatjoe/api-santander |
| Baixa so aceita 1 minuto apos o registro; CNPJ alfanumerico aceito. Sem URL confirmada: as buscas de 31/10/2026 nao acharam a pagina | Wiki CIGAM "Open Banking para Banco Santander" |
| Sandbox instavel; workspace criado pelo Postman | https://www.projetoacbr.com.br/forum/topic/80305-api-santander-rotas-workspace-e-rota-de-consulta-de-boletos/ |
| Outro cliente da mesma API, usado so como segunda leitura | https://packagist.org/packages/flycorp/santander-billet |

## Divergencias dentro da fonte oficial

| Campo | Uma fonte | Outra | Adotado |
|---|---|---|---|
| Codigo de barras | `barCode` (guia, `bankslipData`) | `barcode` (schema do registro) | le os dois |
| HTTP do registro e do PATCH | 200 (guia) | 201 (OpenAPI) | qualquer 2xx |
| `_content` | lista (schema, telas) | objeto (exemplos do OpenAPI) | os dois |
| Multa no POST | `fineQuantityDays` (tabela, OpenAPI) | `FineDate` (um exemplo) | `fineQuantityDays` |
| Mensagens | 45 (nota, erro 1022) | `maxLength: 4` (OpenAPI) | 45 |
| Dias de baixa | ate 99 (notas 14 e 41) | ate 90 (OpenAPI, erro 3042) | ate 99; o banco recusa se valer 90 |
| `environment` no sandbox | `PRODUCAO` (Postman) | o texto so descreve TESTE para o host de producao | `PRODUCAO` |
| Ambiguidade de `documentNumber` | `x(14)` (tabela) | "campo numerico" (Nota 6) | texto; ver CNPJ alfanumerico |

## Decisoes do adapter sobre o que a fonte deixa aberto

1. **Webhook so manda reconsultar.** `parse_webhook` da `LIQUIDADO` para `PAGAMENTO` e `REGISTRADO`
   para `ESTORNO`, mas sem autenticidade no corpo o evento nunca liquida sozinho. Em Pix a reconsulta
   ja liquida; em linha digitavel, o titulo segue `REGISTRADO` ate D+1.
2. **`webhook_ativo` na config.** O webhook depende de configuracao no workspace; sem o campo
   `True`, a capacidade sai da instancia e `parse_webhook` levanta `NaoSuportado`.
3. **Baixa Operacional continua `REGISTRADO`.** O banco diz que o canal Caixa ainda pode estornar no dia.
4. **`Liquidado Parcialmente` vira `REGISTRADO`,** sem valor pago: o modelo canonico nao tem o
   estado, o titulo segue aberto e o adapter nunca envia `paymentType=PARCIAL`. Valor na resposta bruta.
5. **Pix sem o texto esperado fica `BAIXADO`.** Nunca liquidar por suposicao. O risco e o inverso: ver
   "O que falta", item 1.
6. **Sonda como fonte do Pix na consulta:** chave montada com NSU = nosso numero e data = emissao, como
   este adapter registra; so ate D+2; recusa nao derruba a consulta. Depois disso, `pix_copia_cola` vem
   vazio: guarde o valor do registro.
7. **`id_banco` sem zeros a esquerda,** porque `/bills` responde em numero e o webhook completa com zeros.
8. **`seu_numero` higienizado:** tira o que nao e letra, numero ou espaco; acima de 15 e recusado sem
   chamar o banco (cortar perderia a identificacao). A consulta devolve o texto higienizado.
9. **Encargos ausentes nao sao enviados,** como nos exemplos oficiais (antes ia zero). Nome e endereco
   sao cortados nos tamanhos oficiais.
10. **`documento_beneficiario` opcional:** com ele, o `beneficiary` igual ao titular nao e enviado. Sem
    ele, o `Titulo.beneficiario` vai como veio e o banco pode recusar com `00491`.
11. **Percentual de desconto vira valor** sobre o nominal, arredondado ao centavo, porque a API nao tem
    tipo percentual.
12. **PDF busca o documento do pagador** em `tipoConsulta=bankslip` (`payerData`), porque o contrato so
    recebe o `id_banco`. O download vai sem token, e o link nunca entra em erro, log nem resposta bruta.
13. **Especies:** ficaram de fora `BOLETO_CARTAO_CREDITO`, `BOLETO_PROPOSTA` e `BOLETO_DEPOSITO_APORTE`,
    que exigem regras de pagamento proprias (`DIVERGENTE`, `PARCIAL`).
14. **Fora do contrato:** a rota de lista (`GET bank_slips?status=LIQUIDADO`, exige webhook ativo, ultimos
    90 dias), `CANCELAR_PROTESTO` (ver mudanca sugerida), `participantCode`, `sharing`, `iofPercentage`,
    desconto com varias datas e protesto no registro (o adapter manda `SEM_PROTESTO`).

## O que falta (quem pergunta: suporte.conectividade@santander.com.br)

1. **Texto exato da indicacao de Pix.** E o risco principal: se o texto real nao contiver "pagamento via
   Pix", um boleto pago por Pix fica `BAIXADO`, que no modulo Odoo vira transacao cancelada. Conferir no
   primeiro pagamento real, em `statusComplement` e `writeOffDescription`.
2. **HTTP e local do codigo `0001`.** O guia so diz a lista de codigos. O adapter procura em
   `_errors[]._code`, como texto ou numero. Se vier em outro campo, sai `Validacao`, sem recuperacao
   automatica.
3. **Reenvio com o mesmo NSU no mesmo dia.** O NSU e o nosso numero; o codigo de NSU repetido nao e
   documentado, e pode chegar antes do `0001`.
4. **Webhook pela Circular 3.978/2020:** "estrutura diferente", nao publicada; o adapter recusa. Perguntar
   ao especialista de cash.
5. **Fuso de `paymentDate`** nao informado: vira `datetime` sem fuso.
6. **CNPJ alfanumerico.** `x(14)` na tabela, "numerico" na Nota 6, inteiro no PDF. O registro manda o
   texto; o PDF recusa documento nao numerico sem chamar o banco. Terceiro diz que aceita.
7. **Multa e juros omitidos:** o guia so cita perfil do convenio para baixa e protesto. Confirmar que a
   omissao nao herda encargos do convenio.
8. **Cota:** existe 429, sem numero publicado. `requisicoes_por_segundo` segue sem padrao.
9. **Host `trust-open-h`:** aparece nas telas, sem token nem colecao: fora do adapter.
10. **PDF no sandbox:** o guia e a colecao so trazem producao; a instancia sandbox nao declara.

## Mudanca sugerida no contrato (fora deste PR)

- `TipoInstrucao.CANCELAR_PROTESTO`: a API tem, e o modelo nao.
- `Instrucao.valor` aceitar zero: e como a API cancela abatimento (hoje `gt=0`).
- Um estado canonico para pagamento parcial.
- `tests/test_contrato.py`, para a suite comum cobrir o webhook do Santander: em `_santander`,
  `webhook_ativo=True`; na fabrica, `webhook=lambda id_banco, situacao: santander_falso.webhook(id_banco)`.

## Como testar boleto

**Credenciais e certificado.** Sandbox: conta criada no portal, https://developer.santander.com.br, com
aplicacao criada la. Producao: login com o usuario master da conta PJ (ou secundario com acesso) e duplo
fator; o convenio de cobranca precisa do parametro "Entrada Online" ligado (falar com o gerente). Em
cada ambiente, crie a aplicacao, envie a parte publica do certificado (`.pem`, `.cer` ou `.crt`, com a
cadeia completa) e receba `client_id` e `client_secret`. Certificado: A1 ICP-Brasil, 2048 bits, validade
de ao menos 30 dias, nao autoassinado, com uso "TLS Web Client Authentication". O Santander recomenda um
para cada ambiente. Bolepix: chave no Santander, no mesmo CNPJ do convenio. Duvidas:
suporte.conectividade@santander.com.br.

**Workspace.** Uma vez por ambiente: `POST {host}/collection_bill_management/v2/workspaces` com
`{"type":"BILLING","covenants":[{"code":"<convenio 7 digitos>"}],"description":"Teste"}`, cabecalho
`X-Application-Key` e o token, ou pela colecao Postman oficial. Guarde o `id` da resposta: e o
`workspace_id`. Webhook: acrescente `webhookURL`, `bankSlipBillingWebhookActive` e
`pixBillingWebhookActive`; so o primeiro workspace de cada convenio define as regras de envio.

**Hosts.** Sandbox: `https://trust-sandbox.api.santander.com.br` (`ambiente="sandbox"`, dados ficticios,
sem PDF). Producao: `https://trust-open.api.santander.com.br`: `ambiente="teste"` nao registra e nao cobra
tarifa; `ambiente="producao"` registra de verdade e pode cobrar tarifa.

**Sequencia no sandbox** (o guia avisa que as respostas podem variar; anote o que divergir):

| Passo | Chamada do adapter | Estado esperado |
|---|---|---|
| 1. Registrar | `registrar_titulo` | `REGISTRADO`, com linha digitavel e codigo de barras; com chave Pix, tambem Pix e `txId` |
| 2. Consultar | `consultar_titulo` | `REGISTRADO`, mesma linha |
| 3. Repetir o registro | `registrar_titulo` com o mesmo nosso numero | `Duplicado` com `id_banco`. Anote HTTP e codigo: "O que falta", itens 2 e 3 |
| 4. Mudar vencimento | `enviar_instrucao(PRORROGACAO)` | `REGISTRADO`; confira o novo `dueDate` na resposta bruta |
| 5. Abatimento | `enviar_instrucao(ABATIMENTO)` | `REGISTRADO`; valor abaixo do nominal |
| 6. Baixa | `baixar_titulo`, um novo boleto, ao menos 1 minuto apos o registro | `BAIXADO`; a consulta diz `BAIXADO` e nao traz valor pago |
| 7. Webhook | POST de teste na URL | corpo de validacao: nenhum evento e 2xx |

**Em producao, antes de registrar de verdade:** rode o passo 1 com `ambiente="teste"` para validar
dados e certificado. Use outro nosso numero a cada tentativa: o NSU fica queimado no dia. Consulta e
instrucao nao funcionam nesse modo.

**Liquidacao e PDF, so em `ambiente="producao"`.** O guia nao oferece simulacao de pagamento: provar a
liquidacao exige pagar um boleto real de valor baixo, com o OK do responsavel pela conta. Nada neste
teste paga sozinho.

| Passo | Como | Estado esperado |
|---|---|---|
| 8. PDF | `obter_pdf`, com o titulo ativo | bytes comecando com `%PDF` |
| 9. Pago por linha digitavel, mesmo dia | `consultar_titulo` | `REGISTRADO`; complemento "Baixa Operacional" na resposta bruta |
| 10. Dia util seguinte | `consultar_titulo` | `LIQUIDADO`, com `valor_pago` e `data_pagamento` |
| 11. Pago pelo QR Code Pix | `consultar_titulo`, logo depois | `LIQUIDADO`. Se vier `BAIXADO`, anote `statusComplement` e `writeOffDescription` e avise: "O que falta", item 1 |
| 12. Webhook do pagamento | aviso recebido | evento com o nosso numero sem zeros; a reconsulta decide |

Ao terminar, registre aqui o que divergiu. `validado_sandbox` so vira `True` depois disso.
