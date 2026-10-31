# Bradesco (237), API Cobranca: fontes, conflitos e duvidas

`BradescoAdapter.validado_sandbox = False`. Nada foi executado contra o Bradesco: nao ha
credencial. O adapter cobre os dois produtos de cobranca do portal, escolhidos por
`BradescoConfig.hibrido`. Capacidades: `ALTERACAO`, `BAIXA`, `HIBRIDO` (com `hibrido=True`) e
`WEBHOOK` (com `webhook=True`). Sem `PDF`, que a API nao tem.

**Isto corrige a versao anterior**, escrita de fontes de terceiros e que dizia que a especificacao
ficava atras de login. A pagina do portal e so JavaScript, mas ela le uma API JSON aberta.

## Fontes oficiais, lidas em 31/10/2026, sem login

Catalogo: `https://developers.bradesco.com.br/api/v1/products?limit=100`. Cada OpenAPI em
`.../products/{id}/versions/{v}/assetresources` (campo `hrefdata`); manual e collection em
`.../api/v1/documentResources/{doc}/data` (`Accept-Language: pt-BR` traz o texto em portugues). O
certificado do host nao fecha a cadeia em todo cliente Python; `curl` le normal.

| Documento | Identificador |
| --- | --- |
| Produto "Bank Slip" (convencional), versao 1.7.2 | `8a2e869e9549334601954cca1ee604c3`, versao `e4b734f4e7c340fba72c983ba26cee90` |
| Produto "Bank Slip with QR code" (hibrido), versao 1.8.3 | `8a2e869e9549334601954cc57bc0043e`, versao `abfae684f6d644bbb9d26701a73c9ea1` |
| Manual convencional 1.6.4 (07/10/2026) | `8a2e85119621daed0196343220fe6e11` |
| Manual com QR Code 1.0.5 (09/10/2026) | `8a2e91349818c87d0198993623c1019f` |
| Collection convencional | `8a2e95d49562f18001956762bdd6013f` |
| Collection com QR Code | `8a2e8a0796a2f3260196af951bb132d4` |
| Visao geral do webhook | `8a2e837798c43b4a0198c8e9f6671d6e` |
| Token mTLS | `8a2e85209542f4f501954356353d127f` |
| Como gerar a credencial (PDF) | `2cce96c3a8e847d3a5d121937d0d0380` |
| CNPJ alfanumerico | `https://developers.bradesco.com.br/api/v1/documents/04bbb092da7546eb9bc31e53af8559c3/data` |

## Confirmado oficialmente

| Assunto | Oficial |
| --- | --- |
| Hosts | `openapi.bradesco.com.br`, `openapisandbox.prebanco.com.br` |
| Token | `POST /auth/server-mtls/v2/token`, formulario com `grant_type`, `client_id`, `client_secret`, mTLS, 1 hora |
| Registro convencional | `POST /boleto/cobranca-registro/v1/cobranca`; devolve `nuTituloGerado` |
| Consulta convencional | `POST /boleto/cobranca-consulta/v1/consultar`; bloco `titulo` |
| Baixa (os dois produtos) | `POST /boleto/cobranca-baixa/v1/baixar`, `codigoBaixa` 57 (unico aceito) |
| Alteracao convencional | `PUT /boleto/cobranca-altera/v1/alterar`: layout inteiro, so com o que muda preenchido |
| Protesto e negativacao | `POST /boleto/cobranca-protesto-negativacao/v1/executar`; so aceita depois de 3 dias uteis do vencimento e processa a noite |
| Registro hibrido | `POST /boleto-hibrido/cobranca-registro/v1/gerarBoleto`; devolve `ctitloCobrCdent`, `wqrcdPdraoMercd` (EMV), `iconcPgtoSpi` (txId) |
| Consulta hibrida | `POST /boleto-hibrido/cobranca-consulta-titulo/v1/consultar`; campos na raiz; `semvQrcode` |
| Alteracao hibrida | `POST /boleto-hibrido/cobranca-alteracao/v1/alteraBoletoConsulta`, `txId` no cabecalho |
| Decomposicao do documento | CNPJ 8 + 4 + 2; CPF 9 + filial 0 + 2 |
| Negociacao | 18 posicoes no registro (agencia 4, zeros 7, conta 7); 11 no resto |
| Especies | Tabela de siglas identica a usada (DM = 2...) |
| Juros e multa | Juros mensais (valor x % / 30 ao dia); multa unica, em percentual |
| Nosso numero | Ate 11 digitos, opcional no convencional (`nuTitulo`): o banco gera se faltar |
| Janela | 00:00 a 03:00 a API pode ficar indisponivel |
| Erros | Envelope `status`, `transacao`, `mensagem`, `causa` (400, 412, 500); 422 `codigo`/`mensagem`; 401 com 3 motivos; 403 falta vincular o recurso no portal |
| PDF | Nao existe. A "2a via" e a consulta; `base64` no hibrido e so a imagem do QR Code |

### Tabela de status (`codStatus`), versao 2026.10

| Codigos | Rotulo oficial | Canonico |
| --- | --- | --- |
| 1 a 12, 14 a 35, 99 | a vencer, pagamento vinculado, protesto, cartorio, sustacao, baixa solicitada, pagamento retido, negativacao, reativacao | `REGISTRADO` |
| 13, 61, 62 | pago no dia, pago, pago em cartorio | `LIQUIDADO` |
| 52, 53, 54, 55, 57, 58, 59, 60, 63, 64, 98 | registro duplicado, decurso, medida judicial, remessa, a pedido, protestado, devolvido, franco de pagamento, sustado, duplicado | `BAIXADO` |
| 51 no hibrido | "por acerto (quando o pagamento ocorre via QR Code)" | `LIQUIDADO` |
| 51 no convencional, 56, 65 a 73 | ver "O que continua aberto", item 1 | `Validacao` (fora da tabela) |

No hibrido, o FAQ oficial diz que o Pix aparece como "pago no dia" em ate 20 minutos e o titulo e
baixado no processamento da noite: a sequencia e 13 e depois 51.

## Conflitos entre as fontes oficiais

Regra aplicada: na duvida, a opcao cuja falha e uma recusa, nunca um valor errado.

1. **`vlNominalTitulo` no convencional (o mais perigoso).** Manual: "0.25", "10.50", exemplo
   `1255.99`. Collection: `"500.00"`, com a nota "ajustados os valores decimais". OpenAPI: texto so de
   digitos, ate 17. O FAQ de juros ainda diz "R$ 1,00 = 100". Padrao: **texto com duas casas**. Se o banco quiser
   digitos, `"150.00"` e recusado; ja `15000` a um banco que le reais registraria R$ 15.000,00.
   `valor_em_centavos=True` troca. Defesa adicional: o retorno ecoa `vlTitulo` em centavos, e
   divergencia vira `Validacao` citando o nosso numero, para baixar o titulo. **Isso inverte o padrao
   anterior do adapter, que mandava centavos.**
2. **Valor no hibrido:** sem conflito, centavos (manual: R$ 2,00 = 200; exemplo: `5000` gera EMV de
   `50.00`). A collection convencional nao vale aqui.
3. **`qtdeDiasJuros` e `qtdeDiasMulta` no convencional.** Descricao do campo: para iniciar 2 dias
   depois do vencimento, enviar 3. FAQ, exemplo e collection: 1. O exemplo de consulta mostra
   `diasMulta` 1 com `dataMulta` igual ao vencimento. Padrao: **2** (inicio no dia seguinte ao
   vencimento). Errar para mais da um dia de folga ao pagador; errar para menos cobraria encargo de
   quem paga em dia. `dias_encargos` troca. No hibrido o manual e claro: minimo 1, inicia depois de N
   dias; padrao 1.
4. **Percentuais no convencional.** Texto do manual: "2 inteiros e 3 decimais", mas a tabela e os
   exemplos usam 2 casas e dizem que o excesso e truncado. Collection: `"20.00"`. FAQ: 10% = `10000`.
   OpenAPI: so digitos. Padrao: `"2.00"`; mais de 2 casas e recusado aqui, para nao truncar em
   silencio. Com `valor_em_centavos=True`, o formato do FAQ.
5. **Percentuais no hibrido:** "5 decimais (2% = 2.00000)", com tamanho 6 e tipo inteiro no OpenAPI.
   Enviado `"2.00000"`, como o texto; `200000` lido literalmente seria recusado de qualquer modo.
6. **Abatimento na alteracao convencional.** OpenAPI: inteiro, sem escala. Sem exemplo oficial com
   valor. Padrao: segue `valor_em_centavos`, texto `"5.00"`. A consulta depois da alteracao confere
   `valAbat`; divergencia vira `Validacao`, porque essa escala nao e recusada pelo banco enquanto
   couber no titulo.
7. **`Authorization`.** Manuais e OpenAPI falam em Bearer Token; collections e o exemplo do OpenAPI
   mandam o token cru. Falha e sempre 401. Padrao `Bearer`; `token_sem_bearer=True` manda cru.
8. **Tipos JSON.** OpenAPI em texto, manual e collection em numero, varia por rota. Adotado: registro
   convencional, baixa (`cpfCnpj`), protesto e webhook em texto, como o OpenAPI; consulta e alteracao
   em numero, como manual e collection. `nuNegociacao` e `cnegocCobr` sempre em texto: 18 digitos nao
   cabem em numero IEEE sem perder precisao. Falha e recusa.
9. **Data de vencimento na alteracao convencional.** OpenAPI, tabela e exemplo: DDMMAAAA. Collection:
   `20250101`. Usado DDMMAAAA; o outro formato daria data invalida, logo recusa.
10. **`codigoNegativacao`.** Tabela: "fixo 1". OpenAPI: "fixo 1 quando houver inclusao ou alteracao de
    negativacao". Collection: 0. Enviado 0.
11. **Negativacao na rota de protesto.** Manual: so funcoes 1 e 2. OpenAPI: tambem 3 e 4 (`parmFuncao`
    em branco). Implementada a 3 pelo OpenAPI.
12. **Moeda no hibrido (`cindcdEconmMoeda`).** Manual: fixo 00006; exemplo: 6; collection: 9. Enviado
    6.
13. **`codUsuario` na alteracao hibrida.** Manual e exemplo: `OPENAPI`; collection: `NETEMPR`. Enviado
    `OPENAPI`. No registro, `APISERVIC` nas tres.
14. **Rota da alteracao hibrida no sandbox.** O manual cita a rota convencional; OpenAPI e collection,
    a hibrida. Usada a hibrida.
15. **Webhook.** A rota muda por ambiente (`/cadastrar` no sandbox, `/executar` em producao), no manual
    e no OpenAPI. O texto do manual pede "token" no cadastro, mas o layout nao tem esse campo. Validade
    do certificado: "minimo de um ano" no manual, 4 meses a 3 anos na visao geral.
16. **Tamanhos.** Logradouro, bairro e municipio do pagador divergem (OpenAPI 40, 40, 30; manual 100, 50,
    50). Usado o menor. `nuCliente` aceita 25 no registro, mas a consulta devolve `snumero` em 15.

## Escolhas do adapter

- **Texto:** so letras, digitos e espaco em nome e endereco (manual 1.6.1 e 1.6.2, padrao do OpenAPI),
  com a pontuacao virando espaco. `nuCliente` e mensagens mantem a pontuacao, sem acento: o exemplo
  oficial usa `"."` e o hibrido `"019690-1-1"`.
- **Linha digitavel e codigo de barras:** a API devolve a linha com pontos e o `cdBarras` no padrao de
  barras EBCDIC (`<NWnn...>`, 112 posicoes). O adapter devolve 47 e 44 digitos, derivando o codigo da
  linha pela regra do manual. Conferido com o exemplo oficial, decodificado pela tabela EBCDIC.
- **Registro sem retry** quando o banco gera o nosso numero: repetir apos timeout pode registrar dois
  titulos. Com `gerar_nosso_numero`, o numero e unico por carteira e negociacao (recusa 69, "titulo ja
  registrado") e o retry volta. Quem chama precisa consultar antes de registrar de novo.
- **Consulta apos alterar:** a resposta so diz "alteracao efetuada". O adapter consulta, confere o que
  ficou gravado e devolve o estado real. Consulta fora do ar devolve `REGISTRADO`, sem a conferencia.
- **Baixa:** `dados.status` 57 vira `BAIXADO`; 11 "com baixa solicitada" fica `REGISTRADO`.
- **`vlrPagto`:** ver "O que continua aberto", item 2. Valor 50 vezes acima ou abaixo do titulo vira
  `Validacao`, exceto parcial abaixo.
- **13 "pago no dia" sem valor:** `LIQUIDADO` com `valor_pago=None`; quem liquida no ERP espera o valor.
- **`dias_baixa_automatica`:** `tipoPrazoDecursoTres` no convencional, que exige o indicador do contrato
  zerado (manual), e `qtdDecurPrz` no hibrido (365 dias; o QR Code para em 60).
- **Webhook:** o layout nao tem assinatura nem token; o manual so pede TLS 1.2 e o certificado do banco
  como confiavel na aplicacao do cliente. `parse_webhook` devolve o evento **sem `valor_pago`**: serve so
  para disparar a consulta, nunca para liquidar. Aviso de outra carteira ou negociacao e recusado. Por
  ser opcional e depender de cadastro, `WEBHOOK` so vale com `webhook=True`; `cadastrar_webhook` faz o
  cadastro (operacoes I, A, C, E).
- **Dado pessoal:** chaves com trecho de dado pessoal (sacado, pagador, avalista, cedente, cpf, cnpj,
  nome, endereco, cep, chave, EMV) sao mascaradas em `resposta_bruta`.
- **Fora de proposito:** split (`cobranca-manutencao-split`, `cobranca-consulta-split`), listas de
  pendentes, baixados e liquidados (`cobranca-pendente`, `cobranca-baixado-consulta`, `cobranca-lista`),
  debito automatico, bonificacao e reserva de Location (`idLoc`, opcional): o contrato nao pede.
  `cobranca-lista` traz `valorPagamento` em centavos com D-1 e serve de conferencia no Odoo.

## O que continua aberto

Nada aqui foi inventado: a fonte oficial nao responde. Quem responde: `suporte.api@bradesco.com.br`,
`suportetransacoes_api@bradesco.com.br` (erros negociais), `governancaapis@bradesco.com.br` (webhook).

1. **Status sem prova de pagamento.** 51 no convencional ("POR ACERTO", sem a observacao do hibrido);
   56, 68, 69 ("COBRADO - POR RASTREAMENTO", "POR BAIXA MANUAL"); 66, 67 ("CREDITO EXDD"); 65, 70 a 73
   (transferencia para desconto ou de recebiveis). O manual so traz o rotulo. Ficam como `Validacao`,
   para decisao humana: tratar como pago ou como baixa podem estar ambos errados.
2. **Escala de `vlrPagto`.** E o unico valor decimal da consulta (`number`, exemplo `0.00`), lido como
   reais; os demais sao inteiros em centavos. O exemplo oficial so mostra zero.
   `vlr_pagto_em_centavos=True` troca.
3. **`txId` na alteracao hibrida.** O manual o exige no cabecalho (o OpenAPI o marca como opcional) e so
   o registro o devolve: a consulta nao. O contrato de `enviar_instrucao` nao o carrega; o adapter usa o
   que guardou no registro ou `obter_txid`. Sem nenhum, `Validacao`.
4. **Autenticidade do webhook:** se o banco usa certificado de cliente para chamar a URL; o texto so diz
   configurar o certificado dele como confiavel.
5. **Como simular pagamento no sandbox:** o portal nao documenta.
6. **Duplicidade:** corpo do erro de "boleto ja cadastrado" (a collection hibrida tem o caso, sem
   resposta). Nao ha mapeamento para `Duplicado`; `Status-Header` 69 esta na tabela, sem layout.
7. **`nuTitulo` e digito:** o manual fala em nosso numero "sem o digito" e o retorno tem 11 posicoes.
8. **`ctitloCobrCdent` zero no hibrido:** o manual o marca obrigatorio; o exemplo de sucesso da collection
   manda 0. Adotado 0 quando nao ha `gerar_nosso_numero`.
9. **CNPJ alfanumerico:** o aviso oficial diz que os campos passam a texto, sem versao nova para a cobranca
   do portal, mas o OpenAPI ainda fixa `^[0-9]*$`. Beneficiario, pagador e avalista alfanumericos seguem
   recusados aqui.
10. **Cota (`rate limit`):** o OpenAPI cita 429 sem numero. `requisicoes_por_segundo=None`.
11. **QR Code ausente:** o FAQ manda checar `wqrcdPdraoMercd`. Se vier vazio, `pix_copia_cola=None`; o
    titulo existe como boleto comum. `informacoesChavePix` na consulta traz o motivo, sem tabela.

## De terceiros

Serviram a versao anterior e hoje so confirmam uso. Nenhum campo depende deles.

- Collection antiga "Cobranca com QR Code Cliente", `https://github.com/matfatjoe/bradesco-api`.
- Integracao em producao, `https://github.com/psgestaoecapital/erp-psgestao` (sem licenca, so leitura):
  relata que `vlrPagto` nao tem escala no manual e escolhe a leitura proxima de `valMoeda`; relata o codigo
  `CBTT0004` quando a negociacao de 18 posicoes nao e a derivada e vem do gerente.
- SDK `https://github.com/sistem-atc/bank-sdk` (sem licenca, so leitura).
- ACBr, `WS/ACBrBoletoW_Bradesco.pas` (LGPL, nenhum codigo portado): fluxo legado com JWT e
  `X-Brad-Signature`, nao coberto; as APIs do portal usam mTLS.
- Forum do ACBr, topicos 86141, 86142, 88636, 87696, 82855 (`https://www.projetoacbr.com.br/forum/`):
  codStatus 51 como Pix pago e `vlrPagto` em ponto flutuante.

## Como testar boleto

**Credenciais e certificado.** O operador precisa de:

1. Login no portal `developers.bradesco.com.br` ("Sign In / Register"), com a organizacao da empresa.
2. Assinatura do produto no plano Sandbox ("Products" > "Bank Slip" e, para hibrido, "Bank Slip with QR
   code" > "Sign Up"). No sandbox a aprovacao e imediata; em producao, ate 48 horas por e-mail.
3. Certificado A1 **autoassinado** em sandbox, validade de 4 meses a 3 anos, com **nome da empresa e CNPJ
   no CN**. Em producao, A1 de autoridade certificadora, mesmas regras.
4. Aplicacao e credencial no portal ("Register Application", "Request Credential", modelo **MTLS**): CNPJ,
   razao social e o certificado publico, **so o corpo, sem cabecalho**. Ate 10 minutos para liberar. O
   `client_secret` so fica visivel por **3 dias**.
5. Para producao, ainda: contrato de cobranca ativo, funcionalidade "175 - Registro via Webservice" pedida
   ao gerente e primeiro acesso ao Net Empresa feito.

**Configuracao.** `BradescoConfig(ambiente="sandbox", client_id=..., client_secret=..., certificado_pem=...,
chave_pem=..., documento_beneficiario=..., agencia=..., conta_corrente=..., carteira=9)`, somando
`hibrido=True` para o boleto com QR Code. Host: `https://openapisandbox.prebanco.com.br`. Sem massa propria,
use as da collection oficial. Evite 00:00 a 03:00.

**Sequencia** (conferir o campo citado na resposta bruta, antes de qualquer outra coisa):

| # | Passo | Chamada | Estado esperado | O que conferir |
| --- | --- | --- | --- | --- |
| 1 | Registrar R$ 150,00 com multa 2% e juros 1% | `registrar_titulo` | `REGISTRADO` | `vlTitulo` = 15000. Se o banco recusar `"150.00"`, usar `valor_em_centavos=True` e registrar o achado aqui. Linha com 47 digitos, codigo com 44; no hibrido, `pix_copia_cola` e `txid` preenchidos |
| 2 | Consultar | `consultar_titulo` | `REGISTRADO` (`codStatus` 1) | `valMoeda` = 15000; `dataMulta` e `dataPerm`: devem cair no dia seguinte ao vencimento. Se ficarem um dia depois disso, usar `dias_encargos=1` |
| 3 | Prorrogar | `enviar_instrucao` com `PRORROGACAO` e `nova_data_vencimento` | `REGISTRADO` | Resposta `CBTT0445`; `dataVencto` novo na consulta. Recusa `CBTT0162` indica formato da data |
| 4 | Abater R$ 5,00 | `enviar_instrucao` com `ABATIMENTO` e `valor="5.00"` | `REGISTRADO` | `valAbat` = 500. Erro "outro abatimento" indica escala: confirmar `valor_em_centavos` |
| 5 | Baixar outro titulo | `baixar_titulo` | `BAIXADO` (`codStatus` 57) | `dados.status` 57, `statusAnterior` 1; consulta segue 57 |
| 6 | Liquidar (ver abaixo) | `consultar_titulo` | `LIQUIDADO` | `codStatus` 13 e depois 61 (hibrido: 13 e depois 51); `vlrPagto` e `dtPagto` |

**Liquidacao.** O portal nao documenta como simular pagamento no sandbox: perguntar ao suporte. Sem isso, o
passo 6 so roda em producao, com boleto de valor baixo pago de verdade pelo responsavel. O valor em
`vlrPagto` decide a duvida de escala: se R$ 150,00 vierem como `15000`, a consulta recusa com "fora de escala"
e `vlr_pagto_em_centavos=True` resolve. Com `webhook=True`, conferir tambem que o aviso so dispara a consulta.

**Resultado.** Anotar neste documento cada achado, e so virar `validado_sandbox` depois da sequencia inteira.
Erros comuns: 401 "SSL with client authentication is required" (certificado diferente do cadastrado na
credencial), 401 "client credentials were not valid", 403 (recurso nao vinculado a credencial no portal),
`CBTT0004` em negociacao (ver terceiros).
