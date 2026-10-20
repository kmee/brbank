# Bradesco (237), API Cobranca: duvidas em aberto

`BradescoAdapter.validado_sandbox = False`. Nada foi executado contra o Bradesco.

## Fontes

Por ordem de confianca. Nenhuma e a especificacao oficial lida diretamente.

1. **Portal oficial (developers.bradesco.com.br):** NAO foi possivel ler. A pagina e uma
   aplicacao JavaScript ("Marketplace"); WebFetch falhou no certificado e o curl trouxe so
   o esqueleto da pagina. A especificacao OpenAPI de cada produto fica atras de login.
2. **Collection Postman "Cobranca com QR Code Cliente atualizada"**, distribuida pelo
   Bradesco aos clientes e publicada no repositorio `matfatjoe/bradesco-api` (MIT,
   `Postman_collection.json`). Confirma hosts, token (`/auth/server-mtls/v2/token` com
   `grant_type`, `client_id` e `client_secret` no formulario), o corpo da baixa
   (`/boleto/cobranca-baixa/v1/baixar`, `codigoBaixa` 57) e o header `Authorization` com o
   token cru. E do produto **com** QR Code; a baixa e o token sao comuns aos dois produtos.
3. **Integracao em producao de terceiro** (`psgestaoecapital/erp-psgestao`, sem licenca,
   lida so como documentacao): `src/lib/banco/bradesco/index.ts`,
   `leituraCobranca.ts` e `scripts/gates/check-bradesco-liquidacao.ts`. Traz o corpo do
   registro convencional que roda em producao, o corpo da consulta e exemplos de resposta
   citados como "manual v1.6.3" (codStatus 1, 13, 57, 61; erro `CBTT0552`).
4. **SDK de terceiro** `sistem-atc/bank-sdk` (sem licenca declarada, lido so como
   documentacao): endpoints de cada microservico da cobranca convencional e com QR Code,
   descricao dos campos da resposta (`TituloDetalhado`, `TituloQrCode`) e do token.
5. **ACBr** (LGPL, nenhum codigo portado): `WS/ACBrBoletoW_Bradesco.pas`,
   `WS/ACBrBoletoRet_Bradesco.pas`, `ACBrBancoBradesco.pas`, `Particularidades.html` (sem
   secao do Bradesco para API). Tabela de especies, fluxo legado com JWT, nomes de campo
   do sacador avalista e de desconto.
6. **Forum do ACBr** (topicos 86141, 86142, 88636, 87696, 82855): codStatus "13-PAGO NO
   DIA", "51-POR ACERTO", "61-PAGO"; `vlrPagto` em ponto flutuante; erros 400/403/412.

Antes de producao: obter a especificacao no portal, rodar `tests/test_bradesco.py` e a
suite de contrato contra o sandbox (`ambiente="sandbox"`) e so entao virar
`validado_sandbox`.

## Escolhas do adapter

- **Produto:** cobranca convencional (boleto sem QR Code), que e a "API de Cobranca /
  registro de boleto". O produto com QR Code (boleto hibrido) tem endpoints proprios
  (`/boleto-hibrido/cobranca-registro/v1/gerarBoleto`, `.../cobranca-consulta-titulo/v1/consultar`,
  `.../cobranca-alteracao/v1/alteraBoletoConsulta`, `.../cobranca-lista/v1/listar`) e
  corpo com outros nomes de campo (`ctitloCobrCdent`, `vnmnalTitloCobr`...). Fica fora
  desta fatia; `HIBRIDO` nao e declarada.
- **Autenticacao:** portal Developers, `client_credentials` sobre mTLS, credenciais no
  corpo. Reaproveita `OAuth2ClientCredentials` (sem dependencia nova, sem tocar
  `auth.py`). O fluxo **legado** (token por JWT `urn:ietf:params:oauth:grant-type:jwt-bearer`
  assinado com a chave privada, `aud`/`sub`/`iat`/`exp`/`jti`/`ver`, e cada requisicao
  assinada em `X-Brad-Signature` com `X-Brad-Nonce`, `X-Brad-Timestamp`, `X-Brad-Algorithm`,
  `acess-token` e `cpf-cnpj`) NAO foi implementado: as quatro fontes recentes usam o portal
  com mTLS, e a assinatura por requisicao exigiria mudar o `Transporte` (o autenticador nao
  ve o corpo). Se algum cliente so tiver credencial legada, implementar como subclasse
  dentro do adapter, com `cryptography` (RS256 sem PyJWT).
- **Escopo OAuth:** nenhum. Nenhuma fonte manda `scope` no portal. Um topico do forum
  cita 403 "Scopes Not Granted" no produto com QR Code: a liberacao e por produto no portal.

## Duvidas

1. **`Authorization` com ou sem `Bearer`.** A collection Postman e a integracao em
   producao mandam o token cru; ACBr e os dois SDKs mandam `Bearer <token>`. Padrao
   `Bearer`; `BradescoConfig.token_sem_bearer=True` manda cru.
2. **Formato do valor (`vlNominalTitulo`).** ACBr (`* 100`) e o SDK (`"1000"` = R$ 10,00)
   dizem centavos; a integracao em producao manda texto com duas casas (`"150.00"`). Um
   topico do forum relata erro 422 de conversao ao mudar o formato. Padrao centavos;
   `valor_em_centavos=False` manda o texto.
3. **Nomes de campo do registro.** Seguido o corpo da integracao em producao (sem
   acento: `nuNegociacao`, `dtEmissaoTitulo`, `municipioPagador`). O ACBr usa nomes com
   acento nas chaves JSON (`nuNegociacao` com cedilha e til, `dtEmissaoTitulo` e
   `municipioPagador` acentuados) e manda tambem
   `nuTitulo`, `controleParticipante`, `vlIOF` e zeros nos encargos; nao seguido.
4. **Nosso numero.** O adapter nao manda `nuTitulo`: o banco gera e devolve
   `nuTituloGerado`, que vira `id_banco` e `nosso_numero`. Sem nosso numero do cliente nao
   ha idempotencia no banco: repetir um registro apos falha de rede pode duplicar o titulo
   (a idempotencia fica no `Orquestrador`). Se o numero devolvido inclui o digito
   verificador (modulo 11 base 7 sobre carteira + nosso numero, resto 1 = "P", pelo ACBr)
   e desconhecido; o SDK diz que a consulta usa o nosso numero sem digito.
5. **`nuNegociacao` x `negociacao`.** Registro: 18 posicoes, agencia (4) + 7 zeros +
   conta (7), sem digitos (integracao em producao e ACBr). Consulta e baixa: 11 posicoes,
   agencia (4) + conta (7) (collection, ACBr, integracao). A integracao em producao permite
   sobrescrever o valor de 18 posicoes por um codigo informado pelo gerente (erro
   `CBTT0004 NENHUM REGISTRO FOI ENCONTRADO`); o adapter so deriva. Confirmar.
6. **CPF/CNPJ do beneficiario decomposto.** CNPJ: raiz 8 + filial 4 + controle 2. CPF:
   raiz 9 + filial "0" + controle 2, deduzido do exemplo de listagem da collection
   (`cpfCnpj 114383908, filial 0, controle 7`). O ACBr corta o CPF em 8 posicoes; nao
   seguido. CNPJ alfanumerico (beneficiario, pagador ou avalista) e recusado: os campos
   sao numericos em todos os exemplos e o Bradesco anunciou manutencao para o CNPJ
   alfanumerico (13/06/2026) sem formato publicado.
7. **Tipos JSON.** Consulta com numeros (integracao em producao); baixa com `cpfCnpj`,
   `filial`, `controle` e `nossoNumero` em texto (collection). A collection mistura texto e
   numero no mesmo campo entre exemplos; nao se sabe se a API e tolerante.
8. **Status (`titulo.codStatus`).** Mapeados so os codigos com significado lido em fonte:
   1 "A VENCER / VENCIDO" -> REGISTRADO; 13 "PAGO NO DIA", 61 "PAGO", 62 "PAGO EM
   CARTORIO" -> LIQUIDADO; 57 "CONFORME SEU PEDIDO" -> BAIXADO. **51 ficou fora**: o forum
   diz "POR ACERTO" (Pix pago, conferido no dia seguinte) e a integracao em producao o
   trata como baixa sem pagamento. Os demais codigos de baixa citados sem rotulo (52 a 55,
   58 a 60, 63 a 65, 70 a 73, 98) tambem viram `Validacao`. A tabela completa esta no
   manual do portal. Versao da tabela: 2026.10.
9. **Liquidacao sem valor.** 13 "PAGO NO DIA" pode vir sem `dtPagto` e `vlrPagto` (forum e
   integracao). O adapter devolve LIQUIDADO com `valor_pago=None` e `data_pagamento=None`;
   quem liquida no ERP deve esperar o valor (a integracao em producao espera a lista de
   liquidados do dia seguinte).
10. **Escala de `vlrPagto`.** Lido como reais (forum: chega em ponto flutuante; SDK:
    `float`). A integracao em producao diz que o manual nao fixa a escala e escolhe a
    leitura mais proxima de `valMoeda` (este em centavos). Confirmar no sandbox.
11. **Datas da consulta.** `dtPagto` em DDMMAAAA como numero, podendo perder o zero a
    esquerda (`1102026` = 01/10/2026); 0 = sem data. `titulo.baixa.data` e AAAAMMDD
    segundo o SDK (nao usado).
12. **Erros.** A consulta pode responder com o envelope `{status, transacao, mensagem,
    causa}` e sem `titulo` (ex.: `status 412`, `causa "CBTT0552 - TITULO INEXISTENTE"`).
    Nao se sabe se o HTTP vem 200 ou 412; o adapter trata os dois (`Validacao` com o
    `status` do envelope em `status_http`). O envelope de sucesso traz `status` 200 na
    consulta e 0 na baixa (SDK), por isso so `status >= 400` no corpo e erro. Duplicidade
    ("Boleto ja cadastrado" existe na collection, sem codigo) nao e mapeada para
    `Duplicado`.
13. **Encargos.** Multa e juros so vao quando existem (`percentualMulta` + `qtdeDiasMulta`,
    `percentualJuros` + `qtdeDiasJuros`, 1 dia apos o vencimento), porque o Bradesco
    recusa percentual sem os dias (`CBTT0505 INFORME TODOS OS CAMPOS PARA MULTA`, relatado
    pela integracao). Que `percentualJuros` seja mensal vem do ACBr (codigo 2 = percentual
    ao mes). **Desconto e abatimento sao recusados** (`Validacao`): os nomes
    (`percentualDesconto1`, `dataLimiteDesconto1`, `vlAbatimento`) so aparecem no ACBr,
    cujos nomes do registro divergem da integracao em producao.
14. **Mensagens.** `listaMsgs: [{mensagem}]`, ate 4 de 80 caracteres, so pela integracao
    em producao.
15. **Sacador avalista** (`Titulo.beneficiario`): `nomeSacadorAvalista`,
    `cdIndCpfcnpjSacadorAvalista`, `nuCpfcnpjSacadorAvalista` e endereco
    (`logradouroSacadorAvalista`, `cepSacadorAvalista`, `complementoCepSacadorAvalista`,
    `bairroSacadorAvalista`, `municipioSacadorAvalista`, `ufSacadorAvalista`), pelo ACBr
    sem acento e pelos nomes da resposta do SDK (`cnpjCpfSacadorAvalista`,
    `municipioSacadorAvalista`). Confirmar.
16. **Texto.** Acentos removidos e campos cortados no tamanho (nome 70, logradouro 40,
    numero 10, bairro 40, municipio 30, seu numero 25), como ACBr e integracao. Complemento
    e e-mail do pagador nao sao enviados (sem campo confirmado).
17. **Especie.** `cdEspecieTitulo` pela tabela de siglas do ACBr (DM = 2...), em
    `BradescoConfig.especie`.
18. **Alteracao.** NAO declarada. O produto convencional usa `PUT /boleto/cobranca-altera/v1/alterar`
    com `dadosTitulo`, mas o unico exemplo de corpo e um teste do SDK (data AAAAMMDD), e o
    ACBr usa outro caminho (`/boleto/cobranca-registro/v1/alterar-titulo`, PATCH, corpo em
    lista). Protesto e negativacao: `POST /boleto/cobranca-protesto-negativacao/v1/executar`
    (SDK), nao implementado. Prorrogacao, abatimento e protesto levantam `NaoSuportado`.
19. **Baixa.** `codigoBaixa` 57 (ACBr e collection); a consulta depois devolve codStatus 57.
    A resposta da baixa traria `dados.status` e `dados.statusAnterior` (SDK); o adapter
    devolve BAIXADO sem reconsultar.
20. **Webhook.** Existe cadastro (`POST /boleto/cobranca-webhook/v1/cadastrar`, campos
    `documento`, `versaoLayout`, `tipoCadastro`, `utilizaWebhook`, `urlEnvio`, `tipoAviso`),
    mas o formato do aviso que o Bradesco envia nao aparece em nenhuma fonte. `WEBHOOK`
    nao declarada; `parse_webhook` levanta `NaoSuportado`. Alternativa para conciliar em
    lote: listas de liquidados (`/boleto/cobranca-lista/v1/listar`), pendentes e baixados.
21. **PDF.** Nao ha endpoint de PDF no produto convencional (a consulta serve de 2a via com
    linha digitavel e codigo de barras). `PDF` nao declarada.
22. **Dado pessoal na resposta.** A resposta traz `nomeSacado`, `cnpjSacado`, `endSacado`,
    `cepSacado`, `enderecoEma` e afins, que o `mascarar` do core nao conhece por nome. O
    adapter mascara toda chave com trecho de dado pessoal (sacado, pagador, avalista,
    cedente, cpf, cnpj, nome, endereco, cep...) antes de guardar `resposta_bruta`.
23. **Cota.** Desconhecida; `requisicoes_por_segundo=None`.
24. **Certificado.** mTLS com certificado ICP-Brasil A1 (RSA, 2048 bits ou mais) segundo
    manual do desenvolvedor citado por terceiros; o sandbox aceita autoassinado. O
    certificado vale tambem no pedido de token (mesma conexao).
