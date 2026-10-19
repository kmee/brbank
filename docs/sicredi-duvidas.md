# Sicredi: duvidas em aberto do contrato

Fontes usadas: leitura do ACBr (LGPL, so como documentacao, nenhum codigo portado):
`ACBrBoletoW_Sicredi_APIV2.pas`, `ACBrBoletoRet_Sicredi_APIV2.pas`,
`ACBrBoletoW_Sicredi_APIECOMM.pas` e `Particularidades`. Busca na web
(2026-10-19) achou so material nao oficial (foruns do ACBr e conectores PHP);
o portal oficial do Sicredi nao foi acessado. Tudo abaixo precisa de confirmacao
no sandbox/manual oficial antes de `validado_sandbox = True`.

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
9. **`situacao` da consulta.** O ACBr compara por trecho (`EM CARTEIRA`, `VENCIDO`,
   `BAIXADO POR SOLICITACAO`, `LIQUIDADO`). Um forum relata que a API passou a devolver
   uma letra so (`L` liquidado, `E` aberto); mapeadas, sem fonte oficial. Outras
   situacoes (protestado, negativado, rejeitado) levantam `Validacao`.
10. **Valor e data pago**: `dadosLiquidacao.valor` e `.data` (datetime, usamos os 10
    primeiros caracteres). Se vier fora disso, `valor_pago` fica nulo.
11. **Baixa.** Responde `statusComando`/`transactionId`/`tipoMensagem`: parece comando
    assincrono. O adapter reconsulta e devolve o estado (pode ainda nao ser `BAIXADO`).
    Nao declaramos `INSTRUCAO_ASSINCRONA`; revisar com o sandbox.
12. **Registro sincrono?** A resposta traz linha digitavel e codigo de barras, entao
    devolvemos `REGISTRADO`. Nao declaramos `EMISSAO_ASSINCRONA`.
13. **PDF.** Conector PHP cita segunda via por `linhaDigitavel`, rota nao confirmada:
    nao declaramos `PDF`.
14. **Webhook.** Sem assinatura (reconsultar sempre) e so funciona em producao (PRD),
    entao o fluxo nao pode ser testado no sandbox. O ACBr nao tem webhook; o formato
    do payload (`nossoNumero`, `movimento`, `dataEvento`) e o prefixo
    `LIQUIDACAO_` de `movimento` sao suposicao, com os sufixos `PIX`, `REDE`, `COMPE`
    vindos do `tipoLiquidacao` do ACBr. Movimento desconhecido (inclusive eventos que
    nao sao liquidacao) levanta `Validacao`: o chamador deve tratar como "ignorar e
    reconsultar", ate o catalogo oficial entrar na tabela.
15. **Idempotencia.** Sem `nossoNumero` enviado, repetir o POST pode duplicar o boleto;
    ha cache e lock por `chave_idempotencia` no processo, e o `Orquestrador` cobre
    entre processos. `seuNumero` nao e unico no banco (nao confirmado).
16. **Expiracao do token** (`expires_in`) e `refresh_token`: so o `expires_in` e usado.
17. **Variante ECOMM** (`cobrancaonline.sicredi.com.br/.../ecomm/v1/boleto`): fora do
    escopo. Autentica por `POST /autenticacao` com cabecalho `token` e devolve
    `chaveTransacao`, que vai no cabecalho `token`; nao e OAuth2 password e exige
    autenticador proprio. Fica para adapter separado.
