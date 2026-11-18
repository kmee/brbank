# Sicredi: duvidas em aberto do contrato

Fontes usadas: leitura do ACBr (LGPL, so como documentacao, nenhum codigo portado):
`ACBrBoletoW_Sicredi_APIV2.pas`, `ACBrBoletoRet_Sicredi_APIV2.pas`,
`ACBrBoletoW_Sicredi_APIECOMM.pas` e `Particularidades`. Busca na web
(2026-10-19) achou so material nao oficial (foruns do ACBr e conectores PHP);
o portal oficial do Sicredi nao foi acessado. Tudo abaixo precisa de confirmacao
no sandbox/manual oficial antes de `validado_sandbox = True`.

**Fonte oficial publica achada em 2026-11-11:** `developers.sicredi.com.br/public` publica o
OpenAPI "API de Cobranca Sicredi v3.9.1" (cada pagina aceita o sufixo `.md`). Dela vem o detalhe
da liquidacao da consulta (0.0.14): `dadosLiquidacao.juros`, `multa`, `desconto` e `abatimento` e
`dataPrevisaoPagamento` ("data prevista de credito na conta do beneficiario").

**Revisto em 18/11/2026 com a mesma fonte** (indice em `developers.sicredi.com.br/public/llms.txt`):
- `situacao` da consulta: o enum tem 16 valores (`EM CARTEIRA`, `EM CARTEIRA PIX`, `VENCIDO`,
  `LIQUIDADO` e `LIQUIDADO PIX`/`REDE`/`COMPE`/`CARTORIO`/`CHEQUE`, `BAIXADO POR SOLICITACAO`,
  `PROTESTADO`, `EM CARTORIO`, `AGUARDANDO ENTRADA EM CARTORIO`, `AGUARDANDO SUSTACAO DE CARTORIO`,
  `NEGATIVADO`, `REJEITADO`), todos na `TABELA_STATUS` 2026.11. Antes, a liquidacao detalhada por
  canal era recusada: em producao o boleto pago nao baixaria a fatura. Protesto, cartorio e
  negativacao ficam REGISTRADO (o guia "Situacoes do boleto" diz que o titulo segue sem pagamento;
  pago no cartorio, chega `LIQUIDADO CARTORIO`). O canal sai do sufixo (`CANAIS_LIQUIDACAO`).
- Webhook: eventos da contratacao `LIQUIDACAO_PIX`, `_REDE`, `_COMPE_H5`, `_H6`, `_H8`,
  `_CARTORIO` e `ESTORNO_LIQUIDACAO_REDE` (este so manda reconsultar). Somente producao, sem
  assinatura padrao (o guia manda proteger o endpoint por cabecalho e reconsultar). O corpo do
  aviso de cobranca continua sem documentacao.
- PDF: `GET /cobranca/boleto/v1/boletos/pdf?linhaDigitavel=` devolve o PDF (2a via); o adapter
  declara `PDF` e consulta a linha digitavel antes.
- Sandbox: o guia de inicio ("Retornos do Sandbox sao default") diz que a consulta por nosso numero
  e a impressao devolvem dados pre-montados, nao os do cadastro. Na base local de testes, todas as
  faturas receberam o mesmo boleto (R$ 1,77, linha digitavel 74891121150023100718882848251011287560000000177,
  digitos verificadores conferidos e validos) e a consulta respondeu LIQUIDADO. PDF, codigos e
  liquidacao reais so em producao.
- Sandbox, 18/11/2026, direto pelo adapter: o registro devolve sempre o nosso numero 211001292
  com codigo de barras de DV geral 7 (o certo e 8, R$ 99,89) e linha digitavel com DV do 2o campo
  9 (o certo e 7) que nem corresponde ao codigo de barras (R$ 0,50, outro campo livre); a consulta
  devolve outro boleto (211002310, R$ 1,77, campo livre de cooperativa 0718, posto 82, beneficiario
  84825), LIQUIDADO com valor 90; e o PDF dessa linha digitavel responde HTTP 401 "Codigo de
  beneficiario diferente do beneficiario do usuario" (o usuario de teste e 6789/03/12345), com ou
  sem `Content-Type` e `codigoBeneficiario`. Ou seja, o PDF nao tem como ser testado no sandbox.
  Levado ao chamado do Sicredi.

**Sandbox, 15/11/2026 (Odoo pelo `l10n_br_payment_brbank`):** token aceito, registro recusado com
HTTP 400 "O campo especie documento contem um valor invalido": o `especieDocumento` e obrigatorio
(Manual da API de Cobranca 3.9, campo 29) e o adapter nao o enviava. Desde a 0.0.15 vai
`SicrediConfig.especie`, padrao `DUPLICATA_MERCANTIL_INDICACAO`, conferido contra o dominio do
manual; com ele o sandbox registrou. Valores fixos do sandbox (manual 3.9): cooperativa `6789`, posto
`03`, beneficiario `12345`, usuario `123456789`, senha `teste123`.

## O que o adapter faz (confirmado so pelo ACBr)

- Base `https://api-parceiro.sicredi.com.br` (sandbox com prefixo `/sb`). Token:
  `POST /auth/openapi/token`, form `username`, `password`, `scope`,
  `grant_type=password`, cabecalhos `x-api-key` e `context: COBRANCA`.
- Chamadas com `x-api-key`, `cooperativa` (agencia) e `posto`; baixa e instrucoes
  tambem com `codigoBeneficiario` no cabecalho.
- Registro `POST /cobranca/boleto/v1/boletos`; consulta `GET /boletos?codigoBeneficiario=&nossoNumero=`;
  baixa `PATCH /boletos/{nn}/baixa` (corpo `{}`); prorrogacao `PATCH .../data-vencimento`;
  abatimento `PATCH .../conceder-abatimento`.

## Duvidas

1. **Escopo do token.** O ACBr le o escopo da configuracao; `cobranca` veio de um
   conector PHP. Configuravel em `SicrediConfig.escopo`.
2. **nossoNumero.** O ACBr calcula no cliente (9 digitos, formato AA/BNNNNN-D segundo um
   forum). Nao confirmado se o banco gera quando omitido. O adapter nao envia; se o
   sandbox exigir, falta a formula (ano, byte de geracao, DV modulo 11) e o controle
   da sequencia, que e do chamador.
3. **Unidade do juros** (`tipoJuros` VALOR/PERCENTUAL, `juros`): ao dia ou ao mes?
   Recusado com `Validacao`. O ACBr repassa `ValorMoraJuros` cru.
4. **Multa** enviada como percentual (`multa`, como o `PercentualMulta` do ACBr).
   **Desconto** enviado como `tipoDesconto=PERCENTUAL`, `valorDesconto1` e
   `dataDesconto1 = vencimento - dias`; semantica do percentual nao confirmada.
5. **Abatimento no registro** e **baixa automatica**: a V2 do ACBr nao envia;
   recusados. Abatimento existe como instrucao (`conceder-abatimento`).
6. **Valores como numero JSON** (float na fronteira, 2 casas), como o ACBr.
7. **Endereco.** `endereco` = `logradouro,numero` em 40 posicoes (logradouro cortado);
   cidade 25, nome 40, email 40, mensagens 4 x 80: limites do ACBr, que trunca em
   silencio (o adapter tambem). Telefone nao existe no modelo canonico.
8. **CNPJ alfanumerico**: o Sicredi aceita? Enviado como esta em `documento`.
9. **`situacao` da consulta.** Resolvida pelo enum do OpenAPI 3.9.1 (ver acima). As letras
   `L` e `E` de um forum seguem mapeadas por compatibilidade, sem fonte oficial.
10. **Valor e data pago**: `dadosLiquidacao.valor` e `.data` (datetime, usamos os 10
    primeiros caracteres). Se vier fora disso, `valor_pago` fica nulo.
11. **Baixa.** Responde `statusComando`/`transactionId`/`tipoMensagem`: parece comando
    assincrono. O adapter reconsulta e devolve o estado (pode ainda nao ser `BAIXADO`).
    Nao declaramos `INSTRUCAO_ASSINCRONA`; revisar com o sandbox.
12. **Registro sincrono?** A resposta traz linha digitavel e codigo de barras, entao
    devolvemos `REGISTRADO`. Nao declaramos `EMISSAO_ASSINCRONA`.
13. **PDF.** Resolvida: `GET /boletos/pdf?linhaDigitavel=` (OpenAPI 3.9.1). No sandbox vem o
    PDF do boleto pre-montado.
14. **Webhook.** Sem assinatura (reconsultar sempre) e so funciona em producao, entao o
    fluxo nao pode ser testado no sandbox. Os eventos sao os da contratacao (OpenAPI 3.9.1,
    na `TABELA_WEBHOOK`); o formato do corpo (`nossoNumero`, `movimento`, `dataEvento`)
    continua suposicao: pergunta do chamado ao Sicredi. Movimento desconhecido levanta
    `Validacao`: o chamador trata como "ignorar e reconsultar".
15. **Idempotencia.** Sem `nossoNumero` enviado, repetir o POST pode duplicar o boleto;
    ha cache e lock por `chave_idempotencia` no processo, e o `Orquestrador` cobre
    entre processos. `seuNumero` nao e unico no banco (nao confirmado).
16. **Expiracao do token** (`expires_in`) e `refresh_token`: so o `expires_in` e usado.
17. **Variante ECOMM** (`cobrancaonline.sicredi.com.br/.../ecomm/v1/boleto`): fora do
    escopo. Autentica por `POST /autenticacao` com cabecalho `token` e devolve
    `chaveTransacao`, que vai no cabecalho `token`; nao e OAuth2 password e exige
    autenticador proprio. Fica para adapter separado.
