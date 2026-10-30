# Itau (341) API Cash Management / SISPAG (pagamentos): duvidas

`ItauPagamentoAdapter` (`brbank.adapters.itau_pagamento`): Pix por chave, por dados bancarios e
copia e cola. `validado_sandbox = False`: so servidor falso (`tests/itau_pagamento_falso.py`). Nada
rodou contra o Itau: nao ha credencial.

**Grau de confianca.** A especificacao oficial nao foi lida. O contrato vem de terceiros que
concordam entre si, um deles com relato de producao. Antes de mover dinheiro de verdade: conferir a
especificacao com login e rodar a suite contra o sandbox (ver "O que falta").

## Fontes

Lidas em 30/10/2026, so paginas publicas, sem login.

**Oficial e publico (Itau):**

- O1. Autenticacao mTLS: https://devportal.itau.com.br/autenticacao-documentacao
- O2. Primeira chamada: https://devportal.itau.com.br/como-fazer-a-primeira-chamada-a-uma-api
- O3. Pagina da API: https://devportal.itau.com.br/nossas-apis/itau-ep9-gtw-sispag-ext . A pagina e
  o JSON publico dela (`/_next/data/.../itau-ep9-gtw-sispag-ext.json`) vem com `hasAccess: false`,
  `specificationsData: {}` e `openApiDownloadLink: null`: a especificacao so abre com login.
- O4. Pagina comercial do SISPAG: https://www.itau.com.br/empresas/pagamentos-recebimentos/sispag
- O5. Manual CNAB do SISPAG: https://download.itau.com.br/bankline/SISPAG_CNAB.pdf (via arquivo, fora
  deste adapter).

**Terceiros (nao oficiais):**

- T1. Colecao Postman "API Pagamentos PIX" (a descricao e o texto do proprio Itau, a copia e de um
  repositorio de terceiro; nao se sabe a data):
  https://github.com/igor061/itauapipix/blob/f4cb91bb29f50539e2fedd82f6b4a6f27ec8d8b8/docs/API%20Pagamentos%20PIX.postman_collection%20(1).json
- T2. Integracao em producao, comentarios com data, que diz citar a Especificacao Tecnica colada do
  portal e relata testes reais, inclusive respostas do suporte do Itau:
  https://github.com/mateusdobrasil/rentech/blob/9ffab1fc170eafa2128bc6b6120c5446c70332f3/app/lib/itauSispag.ts
  e `app/admin/rh/actions/actions-financeiro.ts` no mesmo repositorio.
- T3. SDK PHP `sistem-atc/bank-sdk` (status e aviso de que boleto nao tem inclusao):
  https://github.com/sistem-atc/bank-sdk/tree/585f2ba905959a036dba67e5fd30a5182e8a4025/src/Itau
- T4. Conector Abstra (lista parametros do schema):
  https://www.abstra.io/docs/tools/connectors/providers/itau-pagamento
- T5. Conector Hyperswitch (Juspay):
  https://github.com/juspay/hyperswitch-prism/tree/e2625fc4e5abfa26a34a868f62def7931c38eb58/crates/integrations/connector-integration/src/payout_connectors
- T6. Outras integracoes com a mesma rota:
  https://github.com/henriquefelipe/cobranca/blob/6a9a52febb60736213d014f7a693dc7c6caca791/Cobranca/Operadora/Itau.cs
  e https://github.com/JuniorMeloDev/FIDCIJJ_Full/blob/0b7c0ca86eb1b88998f07e68c614a8aca062fb26/src/app/lib/itauService.js
- T7. Lista de APIs de envio de Pix, diz que o Itau faz por SISPAG via API e que a documentacao e
  pedida aos canais de atendimento: https://github.com/bacen/pix-api/discussions/480

**Nao usadas de proposito:**

- Manuais no Scribd (`Manual de API SISPAG V2`, `ManualAPI Pagamentos Pix Itau v1`): a previa publica
  vem embaralhada. Decifrar seria contornar a restricao do site.
- Um host `s3-swagger-cmsheadless.hom.cloud.itau.com.br` apareceu num buscador. E homologacao interna
  do Itau, e a documentacao e fechada por login: nao foi acessado.

## O que cada fato tem de lastro

| Fato | Grau | Fonte |
| --- | --- | --- |
| OAuth2 `client_credentials` e mTLS em producao; token em `https://sts.itau.com.br/api/oauth/token` | **oficial** | O1 |
| Sandbox sem mTLS e fora da especificacao OAuth; token de sandbox so chama URL de sandbox | **oficial** | O1 |
| Especificacao fechada por login | **oficial** | O3 |
| Base `https://api.itau.com.br/sispag/v1` | terceiros | T1, T2, T5, T6 |
| Sandbox `https://sandbox.devportal.itau.com.br/itau-ep9-gtw-sispag-ext/v1` e token `.../api/oauth/jwt` | terceiros | T2 (testado com curl), T5 |
| `POST /transferencias` aceita chave, dados bancarios ou `emv` | terceiros | T1, T2, T3, T4, T5 |
| "A inclusao e exclusiva para Pix"; consulta cobre todas as formas | terceiros (texto do Itau na colecao) | T1, T2, T3 |
| Headers `x-itau-apikey` (o client_id), `x-itau-flowID`, `x-itau-correlationID` | terceiros | T1, T2, T6; O1 mostra os dois ultimos no exemplo de token |
| `valor_pagamento` em texto "123.45"; `data_pagamento` AAAA-MM-DD (datetime da 500) | terceiros | T2 (testado), T1 (exemplo antigo usa datetime) |
| Campos do pedido, de `pagador` e da resposta | terceiros | T1, T2, T4 |
| Conta e DV juntos, 8 posicoes com zero a esquerda, pagador e recebedor | terceiros | T2 (producao), T1 nos exemplos |
| `CC`, `CP` conta pagamento, `PP` poupanca | terceiros | T2 |
| `referencia_empresa` ate 20, `informacoes_entre_usuarios` ate 100, sem acento | terceiros, fonte unica | T2 |
| `status_pagamento`: Sucesso, Sucesso (pre-autorizado), Rejeitado, Nao incluido | terceiros | T3, T5, T2 |
| `status_pagamento`: Agendado | fonte unica | T6 |
| Status da consulta (ver tabela) | terceiros | T1, T2, T3 |
| Aprovacao manual no Itau Empresas depois da inclusao | terceiros, fonte unica | T2 |
| Recusa 422 com `motivo_recusa[{codigo, nome}]`; erro 400 ou 422 com `mensagem` e `campos[]` | terceiros | T1, T2, T5 |

**Falta (nao achado em nenhuma fonte):** rota de aprovacao, chave de idempotencia, comportamento de
um reenvio, webhook de pagamento, cancelamento, inclusao de boleto, guia e TED, tabela completa de
`motivo_recusa`, limite de requisicoes.

## Mapa de endpoints e encaixe no `AdapterPagamento`

| Rota | Uso no adapter |
| --- | --- |
| `POST /transferencias` com `chave` | `pagar_lote` de `PIX` |
| `POST /transferencias` com `ispb`, `tipo_identificacao_conta`, `agencia_recebedor`, `conta_recebedor`, `tipo_de_identificacao_do_recebedor`, `identificacao_recebedor` | `pagar_lote` de `PIX` por conta |
| `POST /transferencias` com `emv` | `pagar_lote` de `PIX_QRCODE` (`pix_link` e `url` existem no schema, fora) |
| `GET /pagamentos_sispag/{id_pagamento_sispag}` | `consultar_pagamento` |
| `GET /pagamentos_sispag` (`agencia_operacao`, `conta_operacao`, `cnpj_empresa`, `tipo_lista` obrigatorios) | `localizar_por_referencia` (metodo proprio do Itau) |

Capacidades: `PIX`, `PIX_QRCODE`, `RESERVA_NUMERO` (numero local), `LOTE_LOCAL`. Nao declaradas:
`BOLETO`, `GUIA`, `TRANSFERENCIA`, `LIBERACAO`, `CANCELAMENTO`, `WEBHOOK`.

Lote local: cada linha e uma chamada, sem retry automatico, numero do lote local, `consultar_lote`
levanta `NaoSuportado`. Sem resposta, 5xx, 429, 409, 2xx ilegivel, status de inclusao desconhecido ou
sucesso sem `cod_pagamento` volta `INDETERMINADO` e as linhas seguintes nao saem (`NAO_ENVIADO`).

**Aprovacao.** Segundo T2, `Sucesso` so diz que a API aceitou o pedido: o pagamento segue para aprovacao
manual no Itau Empresas e pode expirar ("Nao Efetuado / Pagamento expirado"). Sem rota de aprovacao
conhecida, `LIBERACAO` nao e declarada e a linha fica `ACEITO`. `PAGO` so sai da consulta.

**Reenvio.** Sem chave de idempotencia conhecida. A `chave_idempotencia` vai em `referencia_empresa`
quando cabe (ate 20 posicoes, letras, digitos e `.-/#`); senao, os 20 primeiros caracteres do SHA-256.
Para resolver um `INDETERMINADO`: `localizar_por_referencia(conta_debito, pagamento)`. Lista vazia nao
prova que nada foi incluido.

| Itau | `StatusPagamento` |
| --- | --- |
| Inclusao: Sucesso, Sucesso (pre-autorizado) | `ACEITO` |
| Inclusao: Agendado | `AGENDADO` |
| Inclusao: Rejeitado, Nao incluido | `RECUSADO` |
| Consulta: Pendente de autorizacao | `ACEITO` |
| Consulta: Pendente de efetivacao, Autorizado | `EM_PROCESSAMENTO` |
| Consulta: Efetuado, Efetuados | `PAGO` |
| Consulta: Nao Efetuado, Rejeitado, Rejeitado em arquivo | `REJEITADO` |
| Consulta: Cancelado | `CANCELADO` |
| Consulta: Estornado | `DEVOLVIDO` |
| Consulta: Pendente de alteracao/exclusao | fora (vira `Validacao`) |

A semantica desses status e deducao pelo nome: nenhuma fonte a define.

## Divergencias entre as fontes

1. **Campos do Pix por conta.** T5 manda um objeto `recebedor`; T1, T2 e T4 mandam campos soltos. T2
   mandou soltos em producao: vale soltos.
2. **Host da inclusao.** T2 relata que o suporte recomendou `https://api.gateway.itau.com.br/sispag/v1`
   para `POST /transferencias` e que a consulta nesse host falha ("ERRO NA CHAMADA AO SUB-PROGRAMA OI").
   O padrao segue `api.itau.com.br`; `ItauPagamentoConfig.url_inclusao` troca so a inclusao.
3. **Sandbox.** T2 diz que `sandbox.devportal.itau.com.br` e o que funciona e que `api.itau.com.br/sandbox`
   da especificacao devolve 404 no token. T3 usa o segundo. Vale T2, que testou. O adapter de cobranca
   usa `devportal.itau.com.br/api/jwt` como token de sandbox (`docs/itau-duvidas.md`): tambem nao
   confirmado. Tudo configuravel (`url_base`, `url_token`).
4. **Autenticacao.** O1 descreve dois fluxos (segredo com mTLS, e private_key_jwt com mTLS). T2 diz que a
   especificacao fala em private_key_jwt, mas usa o segredo. O adapter usa o segredo, como o de
   cobranca.
5. **`modulo_sispag`.** T2 diz que so `Fornecedores` e entrada valida e que a resposta devolve `CONTAS`
   sempre; T5 aceita tambem `Diversos`. Padrao `Fornecedores`, configuravel.
6. **Prefixo da conta.** T2 relata que o pagamento so apareceu para aprovacao com `00` na frente da conta
   (o "codigo de beneficiario"). O preenchimento ate 8 posicoes produz isso em conta de 5 digitos e DV.
   Conta do recebedor com mais de 8 posicoes vai inteira, sem cortar: nao se sabe se o Itau aceita.

## Duvidas para responder com a especificacao ou o sandbox

1. **Boleto, guia e TED.** Tres fontes dizem que a API so inclui Pix, mas a especificacao atual nao foi
   vista. Se o Itau tiver aberto a inclusao, o adapter ganha capacidades. Hoje isso fica no CNAB (O5) ou
   no Itau Empresas.
2. **Aprovacao por API.** Existe rota? "Sucesso (pre-autorizado)" dispensa a aprovacao manual? Ficou
   `ACEITO` por seguranca.
3. **Idempotencia.** O Itau recusa `referencia_empresa` repetida? T2 usa a mesma referencia num lote, o
   que sugere que nao. Um modulo Odoo de terceiro trata 409 como duplicidade, sem lastro
   (https://github.com/heitorbran/payment_itau_pix/blob/c07728e04b397c6fbe7e0efe991179ee4d742149/models/base_payment_api.py):
   aqui vira `INDETERMINADO`.
4. **Tempo ate aparecer na lista.** Quando a inclusao aparece no `GET /pagamentos_sispag`, para confiar em
   lista vazia.
5. **Identificador.** `cod_pagamento` da inclusao e o `id_pagamento_sispag` da consulta (T2 usa assim). Em
   422 de `Rejeitado` vem sem codigo.
6. **endToEnd.** T3 e T2 citam um bloco `dados_pix_transferencia`, sem detalhar os campos: `autenticacao`
   fica vazia ate conhecer o campo.
7. **QR.** `valor_pagamento` diferente do QR e recusado (908, T1). Como pagar QR dinamico com encargos: nao
   se sabe. `pagar_com_juros_multa` e recusado localmente em vez de ignorado, porque pagaria valor errado.
8. **`motivo_recusa`.** Os codigos conhecidos sao 580 (data no passado, T2), 870 (T2) e 908 (T1). A mascara
   apaga `nome`, entao a recusa HTTP traz so o codigo.
9. **`cnpj_empresa`.** O nome diz CNPJ e a colecao diz "Documento do Pagador": conta de pessoa fisica nao
   testada. O exemplo de T1 tem 8 digitos, T2 e T6 mandam o documento inteiro.
10. **Paginacao.** `page` comeca em 0 (T1). T2 viu `totalPages` em camelCase em producao e `total_pages` na
    colecao: o adapter le os dois.
11. **Formato do corpo.** O exemplo de T1 embrulha a consulta em `value` (artefato do Postman); T2 confirmou
    `data`. Lido `data`, com a raiz como alternativa no detalhe.
12. **Limite de requisicoes.** Desconhecido; `requisicoes_por_segundo` sem padrao.
13. **Certificado.** T2 relata que certificado e token precisam ser do mesmo app (sandbox e producao tem
    credenciais separadas), e que o token de producao tambem exige o certificado.

## O que falta para `validado_sandbox = True`

- **Documento:** a Especificacao Tecnica da API `itau-ep9-gtw-sispag-ext` no devportal.itau.com.br, com login
  de empresa que tenha acesso (ou o `openApiDownloadLink` da mesma pagina).
- **Quem pedir:** o agente comercial ou o time de implantacao do Itau da conta (T2 descreve o e-mail com o
  token temporario de ativacao) e o suporte do portal (https://devportal.itau.com.br/suportefaq). Perguntar as
  duvidas 1 a 3.
- **Credencial:** app de sandbox no portal, depois credencial de producao com certificado dinamico.
