# Itau (341) API de Cobranca e Bolecode: fatos, fontes e duvidas

`ItauAdapter.validado_sandbox = False`. So servidor falso (`tests/itau_falso.py`): nada rodou contra o
Itau, nao ha credencial. A especificacao de cada API fica atras de login. Antes de producao: conferir a
especificacao e rodar a sequencia de "Como testar boleto".

Graus: **oficial** (pagina publica do Itau), **terceiros** (duas ou mais fontes independentes concordam),
**fonte unica** (uma fonte so; vale como pista). Revisado em 31/10/2026.

## Fontes

Lidas em 31/10/2026, so paginas e repositorios publicos, sem login.

**Oficial e publico (Itau):**

- O1. Autenticacao mTLS e webhook: https://devportal.itau.com.br/autenticacao-documentacao
- O2. Certificado dinamico: https://devportal.itau.com.br/certificado-dinamico
- O3. Comunicado de certificados e hosts novos: https://devportal.itau.com.br/certificados-apis-expiracao-2026
- O4. Tutorial da cadeia: https://devportal.itau.com.br/tutorial-cadeia-de-certificados
- O5. Conheca o portal (conta, contrato, sandbox): https://devportal.itau.com.br/conheca-o-portal
- O6. Primeira chamada: https://devportal.itau.com.br/como-fazer-a-primeira-chamada-a-uma-api
- O7. Paginas de API, com `hasAccess: false`, `specificationsData` vazio e `openApiDownloadLink: null` no JSON
  publico: https://devportal.itau.com.br/nossas-apis/itau-ep9-gtw-cash-management-ext-v2 (idem
  `itau-ep9-api-recebimentos-v1-externo`, `itau-ep9-gtw-boletos-boletos-v3-ext-aws`). A pagina
  `sandbox-como-comecar` hoje leva ao login.
- O8. Manual CNAB do SISPAG: https://download.itau.com.br/bankline/SISPAG_CNAB.pdf e diferencas entre contas a
  pagar e transferencias:
  https://www.itau.com.br/atendimento-itau/para-empresas/pagamentos/quais-as-diferencas-entre-contas-a-pagar-pagamentos-e-transferencias

**Terceiros (nao oficiais):**

- T1. ACBr trunk2 r48815 (out/2026, LGPL, lido como documentacao, nenhum codigo portado):
  https://github.com/boagestao/acbr/blob/ca82b01f088981e17b3ff7bc112f9d91f3b0635d/Fontes/ACBrBoleto/WS/ACBrBoletoW_Itau_API.pas
  e `ACBrBoletoRet_Itau_API.pas` na mesma pasta.
- T2. `edigomes/api_boleto` (diz ler as specs do cliente e relata testes reais):
  https://github.com/edigomes/api_boleto/blob/348ef5f/README.md e `src/Banks/Itau/`.
- T3. `sistem-atc/bank-sdk` (diz seguir o portal): https://github.com/sistem-atc/bank-sdk/tree/585f2ba905959a036dba67e5fd30a5182e8a4025/src/Itau
- T4. `leandroferreirama/api-itau`: https://github.com/leandroferreirama/api-itau/blob/f64f139/Readme.md e
  `src/Itau/API/Environment.php`.
- T5. `joaotorresz/all4pay-saas-`, com testes de sandbox em 30/10/2026:
  https://github.com/joaotorresz/all4pay-saas-/blob/440157f/docs/rodada-11/cobranca-itau.md e
  `src/core/cobranca-banco/itau.ts`.
- T6. `thyagopacher/ms-payment-gateway`:
  https://github.com/thyagopacher/ms-payment-gateway/blob/e4a3f0e/app/Clients/Banks/Itau/ItauBoletoClient.php
- T7. Forum ACBr, topico 92306 (endpoint novo do Bolecode, 17 digitos, maio a ago/2026):
  https://www.projetoacbr.com.br/forum/topic/92306-aten%C3%A7%C3%A3o-descontinua%C3%A7%C3%A3o-do-endpoint-atual-da-api-bolecode-bolecode-pix-do-ita%C3%BA-nova-api-de-boleto-hibrido-dispon%C3%ADvel-para-testes/
- T8. Forum ACBr, topico 80004 (422 de `quantidade_dias_apos_vencimento`, exemplo do portal):
  https://www.projetoacbr.com.br/forum/topic/80004-mudan%C3%A7as-necess%C3%A1rias-em-acbrboletow_itau_api/
- T9. Forum ACBr, webhook: https://www.projetoacbr.com.br/forum/topic/76762-webhook-bolecode-itau/ e
  https://www.projetoacbr.com.br/forum/topic/79873-problema-webhook-itau/
- T10. Omie (carteira 109, chave Pix, cancelamento 24h depois):
  https://ajuda.omie.com.br/pt-BR/articles/7225464-configurando-a-integracao-com-o-itau-bolecode-via-api
- T11. `Hello-Solucoes/itauboletov2` (comentarios copiados da especificacao, tabela de instrucoes):
  https://github.com/Hello-Solucoes/itauboletov2/blob/fa7b6bc/src/ItauBoleto/Client/Requests/PostBoletos/InstrucaoCobranca.php
- T12. `sistem-atc/LMS` (protesto, mensagens):
  https://github.com/sistem-atc/LMS/blob/bf85045/app/Services/Banks/Banks/Itau/Methods/Protesto.php e
  `GeraBoleto.php` na mesma pasta.
- T13. `yaguin/itau-sdk-php`: https://github.com/yaguin/itau-sdk-php/blob/c9f967b/src/Models/BillingInstruction.php
- T14. Token de sandbox em outras integracoes: T2 e T5 de `docs/itau-pagamento-duvidas.md` (`rentech`,
  testado com curl, e `hyperswitch-prism`).

**Nao usadas de proposito:** previas embaralhadas no Scribd (`Manual de API SISPAG V2`, `Orientacoes Webhook de
Cobranca`; so o titulo e o resumo publico foram lidos), o host de homologacao interna do Itau e qualquer
area com login.

**Observacao direta (31/10/2026):** handshake TLS sem credencial nos hosts publicados. `api.gateway.itau.com.br`
responde com a cadeia GlobalSign Root R46, que ja vem no `certifi`: nenhuma CA propria e preciso no adapter.
`api.itau.com.br` ainda responde, com a cadeia antiga.

## O que mudou nesta revisao

| Item | Antes | Agora | Grau |
| --- | --- | --- | --- |
| Host de emissao, alteracao e baixa | `api.itau.com.br` | `api.gateway.itau.com.br` | **oficial** (O3, O4), T1, T4 |
| Token de sandbox | `devportal.itau.com.br/api/jwt` | `sandbox.devportal.itau.com.br/api/oauth/jwt` | terceiros (T5, T14); T1 segue no antigo |
| Bolecode: `indicador_pagamento_parcial`, `quantidade_maximo_parcial`, `desconto_expresso` | enviados | omitidos | terceiros (T1 "nao usado na nova api pix", T5) |
| Bolecode: multa e juros | `quantidade_dias_*` | `data_multa`, `data_juros` | terceiros (T1, T8 exemplo do portal, T11) |
| CASH: `quantidade_maximo_parcial: 0` | enviado | omitido | terceiros (T1 so manda com parcial, T2 nao manda) |
| Bolecode com 202 | `Validacao` (parecia falha) | `PENDENTE_REGISTRO`, declara `EMISSAO_ASSINCRONA` | terceiros (T2, T3) |
| Baixa automatica por dias | recusada | `instrucao_cobranca` codigo `8` + `quantidade_dias_apos_vencimento` | terceiros (T1, T8, T11 codigo; T2, T13 campo) |
| `Titulo.mensagens` | ignorado | `dado_boleto.lista_mensagem_cobranca` | terceiros (T2, T5, T12, T13) |
| Protesto | `NaoSuportado` | `PATCH .../protesto` com `dias_protesto` da config | terceiros (T1, T2, T12) |
| Credencial do Bolecode | uma so | `bolecode_*` opcionais | **oficial** (O1, dois fluxos), T4 |
| `x-itau-flowID` | nao enviado | enviado | **oficial** (O3, exemplo), T2, T5 |

Capacidades: antes `HIBRIDO | ALTERACAO | BAIXA`; agora `HIBRIDO | ALTERACAO | BAIXA | EMISSAO_ASSINCRONA` (com
`bolecode=False`, so `ALTERACAO | BAIXA`). `PDF` e `WEBHOOK` seguem fora.

## Fatos

| Fato | Grau | Fonte |
| --- | --- | --- |
| OAuth2 `client_credentials` com id e segredo no formulario, sem escopo, e mTLS em producao | **oficial** | O1, O3 |
| Token em `https://sts.itau.com.br/api/oauth/token`, 300 segundos; certificado vale 365 dias | **oficial** | O1, O3 |
| `sts.itau.com.br/as/token.oauth2` existe para private_key_jwt e no teste do certificado; o adapter nao usa | **oficial** | O1, O2 |
| Sandbox sem mTLS e fora do OAuth2; token de sandbox so chama URL de sandbox | **oficial** | O1 |
| `api.itau.com.br` -> `api.gateway.itau.com.br` (limite 18/10/2026), libera IPs `200.196.155.117/32`, `138.59.163.69/32`, `3.44.192.200/29` | **oficial** | O3 |
| Hosts fora do comunicado nao mudam: `sts`, `secure.api.cloud`, `pix-pj.api`, `boletos.cloud` | **oficial** (regra geral), T4 cita `sts` e consulta | O3, T4 |
| Credencial de cobranca (e-mail cifrado) e de Pix/Bolecode (pedida ao Officer Cash) sao fluxos separados | **oficial** | O1 |
| Bolecode e baixa exigem habilitacoes e credenciais diferentes | terceiros | T4 (aviso explicito), O1 |
| Bolecode novo: `https://pix-pj.api.itau.com/recebimentos-pix/v1` + `/boletos-pix` | terceiros | T1, T2, T6, T7; T3 usa `/boletos_pix` (divergencia 1) |
| Sandbox: `sandbox.devportal.itau.com.br/itau-ep9-gtw-cash-management-ext-v2/v2` e `.../itau-ep9-api-recebimentos-v1-externo/v1` | terceiros | T1, T7 |
| Bolecode sem envelope `data`, com `dados_qrcode.chave`; CASH com `data` e `codigo_canal_operacao` | terceiros | T1, T2, T4; T3 envolve em `data` (divergencia 2) |
| Emissao em 17 digitos e percentual em 12, mesmo no Bolecode novo | terceiros | T1, T5, T7 (suporte); T2 manda decimal (divergencia 3) |
| Alteracao em decimal texto ("2.50") e `PATCH` responde 204 | terceiros | T1, T2, T3, T4 |
| `id_boleto` = agencia (4), conta (7), DAC, carteira (3), nosso numero (8) | terceiros | T1, T2, T3, T4, T5 |
| Nosso numero de 8 digitos, gerado pela empresa; `dac_titulo` vem separado | terceiros (T1, T5); DAC so em T3 | |
| Consulta `boletoscash/v2/boletos` com `view=specific`, porque `view=full` nao traz pagamento | terceiros | T1 ("suporte sugeriu"), T2, T3 |
| Consulta devolve varios itens quando o nosso numero foi reaproveitado (45 dias); o adapter le o ultimo | fonte unica | T1 |
| `Em Aberto`, `Paga`, `Baixada` em `situacao_geral_boleto` | terceiros | T1, T5 |
| Codigos curtos `E`, `L`, `BL`, `B`, `BA`, `BC` | fonte unica | T1 (e-mail do suporte ao ACBr) |
| `baixa.motivo_baixa = "Baixa por ter sido liquidado"` vira LIQUIDADO | fonte unica | T1 |
| `pagamentos_cobranca[].valor_pago_total_cobranca` e `data_inclusao_pagamento` | terceiros | T1, T5, T11 |
| Bolecode pago por Pix pode vir sem valor (`BL`) | fonte unica | T1; concilie por Pix ou francesinha |
| Baixa `PATCH .../baixa` com corpo vazio | terceiros | T1, T2, T3; T5 manda `codigo_baixa` (divergencia 4) |
| Alteracao e baixa so depois de 1 dia util da emissao | terceiros | T3, T2 ("Atualizacao nao permitida na mesma data de inclusao"), T10 |
| Protesto: `{"protesto": {"codigo_tipo_protesto": 1, "quantidade_dias_protesto": N}}`; 4 nao protestar, 9 cancelar | terceiros (1), fonte unica (4 e 9) | T1, T2, T12 |
| Instrucao de baixa: `codigo_instrucao_cobranca` 8, `quantidade_dias_apos_vencimento` | terceiros | T1, T8, T11, T13 |
| Multa 01 valor, 02 percentual, 03 isento; juros 90 percentual mensal; desconto 02 com `descontos[]` | terceiros | T1, T2 |
| Corpo de erro com `codigo`, `mensagem`, `campos[]` | terceiros | T1, T5 |
| Sem endpoint de PDF nas APIs de emissao e consulta: devolvem linha e barras | terceiros | T2, T1 |
| Webhook de boleto: cadastro em `https://boletos.cloud.itau.com.br/boletos/v3/notificacoes_boletos` (POST, GET, PATCH, DELETE) | terceiros | T2, T3, T6, T9 |
| Campos: `id_beneficiario`, `webhook_url`, `webhook_client_id`, `webhook_client_secret`, `webhook_oauth_url`, `webhook_oauth_scope`, `valor_minimo`, `tipos_notificacoes` (`BAIXA_OPERACIONAL`, `BAIXA_EFETIVA`) | terceiros | T2, T3, T6 |
| O Itau pega token no OAuth2 do recebedor (`client_secret_basic`) antes de notificar; ha CA do Itau para mTLS | **oficial** (mecanismo, O1), T2, T3 | O1 cita OAuth2 "para parceiro do pix indireto" |

## Por que PDF e WEBHOOK nao entraram

- **PDF.** Nenhuma rota confirmada para boleto ja registrado. `boleto.api.itau.com/boleto/v1` (outro payload, em
  camelCase) tem um campo `base64`, mas so T2 descreve, e la a chamada deu `403 explicit deny` por nao
  estar contratada. Quem precisa de PDF renderiza a ficha com a linha digitavel.
- **WEBHOOK.** O cadastro e a autenticacao estao bem descritos, mas o corpo da notificacao nao: o unico trecho e
  codigo de um usuario em T9 (`idBoleto`, `id`, `tipoLiquidacao`) e T5 supoe `numero_nosso_numero`. Sem duas
  fontes concordantes, `parse_webhook` segue `NaoSuportado`. Quando entrar, o evento so dispara reconsulta,
  como pede o contrato: quem autentica o Itau e a aplicacao, nao a biblioteca.

## Divergencias e o que prevalece

1. **`/boletos-pix` x `/boletos_pix`.** T3 usa sublinhado sob `recebimentos-pix/v1`, sem teste real. T1, T2, T6 e o
   log de T7 usam hifen: vale hifen. `caminho_bolecode` troca.
2. **Envelope `data` no Bolecode.** T3 envolve; T1, T2 e T4 nao, e T1 tem retorno de usuarios: sem envelope.
3. **Valores no Bolecode novo.** T2 manda decimal (`"150.00"`), como a documentacao. T1 traz o erro do Itau com
   decimal ("O campo valor titulo aceita somente numeros") e T7 traz a confirmacao do suporte: 17 digitos.
4. **Corpo da baixa.** T5 manda `{"codigo_baixa": "OUTROS"}` em `boletoscash`, sem teste. Vale `{}` em `cash_management`.
5. **Multa e juros no Bolecode novo.** T2 mantem `quantidade_dias_*`, mas diz nao ter a API liberada. T1 foi
   ajustado depois de retorno de usuarios: vale `data_*`.
6. **Mensagens.** T2 tambem cita `lista_mensagens_cobranca` dentro do item; T12 traz as duas. Vale a do
   `dado_boleto`, que mais fontes usam. T1 nao manda mensagem, e por isso o Bolecode novo pode recusar: teste.
7. **Token de sandbox.** T1 e `paulohenriquejamsoft/AcbrBoleto` usam `devportal.itau.com.br/api/jwt`; T5 viu o
   portal fazer POST em `sandbox.devportal.itau.com.br/api/oauth/jwt` com 200 e `/api/oauth/token` dar 403. Vale o
   mais novo, medido. `url_token` troca.
8. **Status `Vencido`.** T5 e `MarcosViana-Brasil/prjConciliacaoFinanceira` tratam, sem teste. T1 nao lista. Nao entrou:
   viraria `Validacao` em vez de um estado adivinhado. Idem `LC`, `EA`, `TM`, `TS` e `Pagamento devolvido`.
9. **Negativacao.** T3 manda `codigo_tipo_negativacao`; T1 nao tem a rota e nenhuma fonte lista os codigos.
   Segue `NaoSuportado`.

## Perguntas para o agente comercial ou o suporte do portal

So a especificacao com login responde. Canal: Officer Cash, gerente ou "abrir um chamado" no portal; Parceiros e
Tecnologia (11) 3003 9483, contatoitau@itau-unibanco.com.br (O3).

1. A emissao de Bolecode em `recebimentos-pix/v1` usa `/boletos-pix`? Qual o payload oficial (envelope, formato de
   `valor_titulo`, `data_multa` x `quantidade_dias_multa`)?
2. Um mesmo `client_id` pode ter Bolecode e Cobranca, ou sao sempre duas credenciais? Qual delas consulta Bolecode?
3. O 202 "Operacao em andamento" e so do Bolecode? Em quanto tempo a consulta acha o titulo?
4. Qual o corpo da notificacao de `notificacoes_boletos`, para `BAIXA_OPERACIONAL` e `BAIXA_EFETIVA`? Qual identifica
   o titulo? O webhook cobre Bolecode pago por Pix?
5. Lista completa de `situacao_geral_boleto`? Existe `Vencido`? Com `BL`, como obter o valor pago?
6. O sandbox grava o titulo com `efetivacao`? Da para consultar, alterar e baixar nele? Tem massa de teste? Exige
   `x-sandbox-token`?
7. Qual o codigo de erro de nosso numero repetido? (hoje vira `Validacao`, nao `Duplicado`)
8. Limite de requisicoes? Tamanho maximo de `texto_seu_numero` na emissao e de `mensagem`? Maximo de mensagens?
9. Codigos de `codigo_tipo_negativacao` e a rota de negativacao.
10. `codigo_instrucao_cobranca` e texto ou numero? `dia_util` e obrigatorio na instrucao 8?
11. Existe rota de PDF de boleto ja registrado? Precisa contratar a `boleto/v1`?
12. `sacador_avalista` exige endereco?

## Pagar boleto por API (SISPAG): so pesquisa

Nada contradiz a conclusao de `docs/itau-pagamento-duvidas.md`: a inclusao publica da API e Pix; boleto e consultavel,
nao incluivel. T3 reforca ("o doc Cash Management cobre inclusao de Pix + consulta"). Busca de codigo por `sispag/v2`,
`sispag/v1/boletos` e parecidos nao achou nada. Pagamento de boleto da empresa existe por arquivo (CNAB, O8) e no Itau
Empresas. Grau: terceiros, sem especificacao. Ainda falta perguntar: o `Manual de API SISPAG V2` do Scribd (nao lido) e a
versao atual da especificacao abrem inclusao de boleto?

## Como testar boleto

**Nao da para testar sem login de empresa.** Autocadastro cria conta, mas o catalogo e o sandbox dependem do contrato
assinado com o Itau (O5). Sem ele, peca ao gerente ou Officer Cash.

**O que providenciar:**

| Item | Sandbox | Producao |
| --- | --- | --- |
| Conta no devportal ligada a empresa | sim | sim |
| `client_id` e `client_secret` | do app no portal | e-mail com credenciais e token temporario (O1, O2) |
| Certificado | nao tem mTLS (O1) | dinamico: gere o CSR, envie a `sts.itau.com.br/seguranca/v1/certificado/solicitacao`; a resposta traz o certificado e o `client_secret`, uma vez so |
| Prazo | | token temporario chega em 1 dia util e vale 7 dias; certificado vale 365 dias e renova nos ultimos 30 (O1, O2) |
| Dados da conta | de teste do portal | agencia, conta, DAC, carteira (109) e chave Pix |
| Rede | | liberar IPs do `api.gateway` no firewall (O3) |

O programa pronto do portal (Java, Python, Node, Go, C#) gera chaves e certificado: `certificado_dinamico` em O2. Bolecode e
Cobranca costumam ser duas credenciais: use a de Bolecode em `bolecode_client_id`, `bolecode_client_secret`,
`bolecode_certificado_pem` e `bolecode_chave_pem`, e a de cobranca nos campos principais.

**Hosts.** `ItauConfig(ambiente="sandbox")` ja aponta para `sandbox.devportal.itau.com.br` e o token em `/api/oauth/jwt`
(terceiros, ver divergencia 7). T5 tomou 403 "Request blocked" do CloudFront ao chamar de fora do navegador em nuvem:
rode de uma maquina no Brasil. Se o token falhar, teste `url_token="https://devportal.itau.com.br/api/jwt"`.

**Limite do sandbox.** Com `etapa_processo` padrao (`simulacao` no Bolecode, `validacao` no CASH), ele so valida o payload e
nao grava (T1, T2): a consulta seguinte nao acha o titulo. Portanto o sandbox cobre so o passo 1. Os demais precisam de
producao, com titulo real, tarifa e pagamento de verdade feito por quem opera. Antes de producao, teste `CASH`
(`bolecode=False`) e Bolecode em separado e use valor baixo com vencimento longo.

**Sequencia** (`id_banco` = nosso numero; anote o `x-itau-correlationID` do log para o suporte):

| # | Passo | Chamada | Esperado |
| --- | --- | --- | --- |
| 1 | Registrar | `registrar_titulo` | `REGISTRADO` com `linha_digitavel` e `codigo_barras`; Bolecode com `pix_copia_cola`. Se vier `PENDENTE_REGISTRO`, aguarde e consulte. Campo recusado volta nomeado em `campos[]` |
| 2 | Consultar | `consultar_titulo` | `situacao_geral_boleto` = `Em Aberto` -> `REGISTRADO` |
| 3 | Alterar vencimento | `enviar_instrucao` `PRORROGACAO`, **no dia util seguinte** | 204 e, na consulta, `data_vencimento` nova em `resposta_bruta`; continua `REGISTRADO`. No mesmo dia, erro "nao permitida na mesma data de inclusao" |
| 4 | Abatimento | `enviar_instrucao` `ABATIMENTO` | 204; continua `REGISTRADO`, com o valor reduzido em `valor_abatimento` |
| 5 | Baixar | `baixar_titulo` | 204 e `BAIXADO`; a consulta traz `Baixada` -> `BAIXADO` |
| 6 | Liquidar (outro titulo) | operador paga um titulo pelo banco, nos dois meios; depois `consultar_titulo` | Linha digitavel: `Paga` ou `L` -> `LIQUIDADO`, com `valor_pago` e `data_pagamento`. Pix: `BL` -> `LIQUIDADO`, `valor_pago` pode vir `None` (conciliar pelo Pix) |

**Registrar no fim:** payload aceito, resposta real de cada passo (mascarada), status que apareceram e respostas das
perguntas acima. So com isso `validado_sandbox` pode mudar, e os fatos deste documento sobem de grau.
