# Banco do Brasil: duvidas em aberto do contrato

Fontes usadas, por ordem de confianca:

1. **Homologacao do BB** (aplicacao "Em teste" da KMEE, 2026-10-28): o que foi exercitado esta
   marcado como confirmado. Como rodar, hosts e massa de teste: `docs/bb-homologacao.md`.
2. **Doc oficial e OpenAPI** do portal (apoio.developers.bb.com.br; OpenAPI de Cobrancas 2,
   Pagamentos em Lote 1.2.1 e Extratos 2.0.1 em `api.externo.bb.com.br/docs/v1/openapi`).
3. **Leitura (so como documentacao, sem portar codigo, licenca LGPL) do ACBrBoleto:**
   `WS/ACBrBoletoW_BancoBrasil_API.pas`, `WS/ACBrBoletoRet_BancoBrasil_API.pas`,
   `ACBrBancoBrasil.pas` e a secao 3.1 de `Particularidades.html`, base da primeira versao do
   adapter de Cobranca.

## Cobrancas v2 (`bb`)

`validado_sandbox = True` desde 2026-10-28: registro, consulta, prorrogacao, abatimento e
baixa na homologacao (boleto `00031285571793202880`).

Confirmado na homologacao: host `api.hm.bb.com.br/cobrancas/v2` sem mTLS; registro devolve linha
digitavel, codigo de barras e, com `indicadorPix = "S"`, `qrCode.emv` e `qrCode.txId`; consulta
traz `codigoEstadoTituloCobranca` 1 para o titulo novo e todos os campos que o adapter le (menos
`id`, para o qual o adapter usa o proprio `id_banco`). Pagador com CNPJ ficticio valido e aceito.

### Risco do PRD: chave de desenvolvedor e header de parceiro

- `gw-dev-app-key` e **query param** em toda chamada de negocio (nao em header),
  inclusive no PATCH e no POST de baixa. O adapter a monta em `_caminho`, mantem fora
  do repr da config e o transporte nunca loga URL.
- O header `x-bb-portal-devx-cnpj-parceiro` (CNPJ da software house) e opcional
  (`BBConfig.cnpj_parceiro`). Nao se sabe se o BB passara a exigi-lo.

### Duvidas

1. **Autenticacao do token.** Resolvida: a homologacao aceita Basic e credenciais no corpo.
   O padrao do adapter (corpo no sandbox, Basic em producao) segue valendo.
2. **Escopos.** Resolvida: `cobrancas.boletos-info` e `cobrancas.boletos-requisicao`
   concedidos pelo OAuth de homologacao.
3. **Nosso numero.** `numeroTituloCliente` = "000" + convenio (7 digitos, zeros a
   esquerda) + nosso numero (10 digitos). Confirmado com o convenio 3128557. Quem garante a
   unicidade da sequencia e o chamador. Convenios de 4 e 5 digitos (so CNAB) sao recusados.
4. **Duplicidade.** Nao se sabe qual codigo o BB devolve para `numeroTituloCliente`
   repetido. So o 409 vira `Duplicado`; a idempotencia real e o cache por
   `chave_idempotencia` (no processo) e o `Orquestrador`.
5. **Status do registro.** Resolvida: o registro ja traz linha e codigo de barras e a consulta
   imediata devolve o estado 1; `REGISTRADO` direto esta certo.
6. **Tabela de status.** Mapeados: 1, 2, 3, 4, 5, 8, 9, 13 (REGISTRADO), 6 (LIQUIDADO),
   7 (BAIXADO), 80 (transitorio, PENDENTE_REGISTRO). **Deliberadamente fora** (levantam
   `Validacao`): 10, 11, 12, 14 a 19 e 21. Liquidar ou baixar por palpite e pior que recusar.
   Candidatos a LIQUIDADO apos confirmacao: 11, 12, 16. O simulador de pagamento de boleto da
   homologacao respondeu 504 em 2026-10-28, entao o estado 6 ainda nao foi visto.
7. **Pagamento parcial.** `indicadorPermissaoRecebimentoParcial` nao e enviado. O
   modelo canonico nao tem o conceito.
8. **Data de emissao.** O adapter usa a data do dia (`hoje` injetavel). Aceita na homologacao.
9. **Multa.** O BB exige data de inicio; o adapter usa vencimento + 1 dia (suposicao).
   Tipo 2 = percentual. Juros: tipo 2 = taxa mensal. Desconto: tipo 2 = percentual ate
   a data, `dataExpiracao` = vencimento menos `desconto_dias_antes`. Tipos vindos do ACBr.
10. **Baixa automatica.** `dias_baixa_automatica` e recusado: a equivalencia com
    `numeroDiasLimiteRecebimento` nao e certa. Mensagem: o BB limita a 30 caracteres; acima
    disso o adapter recusa em vez de truncar em silencio.
11. **CPF/CNPJ como inteiro.** `numeroInscricao` e `cep` vao como numero (como no
    ACBr). CNPJ alfanumerico nao cabe: recusado com `Validacao`. A doc oficial ja documenta
    CNPJ alfanumerico em Cobrancas; revisar o formato quando houver massa alfanumerica.
12. **Pix no boleto (`indicadorPix`).** Resolvida para o registro: a resposta traz
    `qrCode.emv/txId/url`. A consulta de detalhe nao traz o Pix (ha `GET /boletos/{id}/pix`,
    nao implementado), e por isso `HIBRIDO` nao e declarada.
13. **Baixa.** Confirmada: resposta com `dataBaixa`, `horarioBaixa` e
    `numeroContratoCobranca`; o adapter devolve `BAIXADO` direto. A consulta logo depois
    ainda mostra o estado 1; minutos depois vem o 7 (BAIXADO) com `codigoTipoBaixaTitulo` 1.
    Quem precisa do estado do banco reconsulta depois.
14. **Alteracao (PATCH).** Implementados: nova data de vencimento e abatimento. Cancelar
    abatimento, desconto, juros, multa, valor nominal, protesto e negativacao ficaram fora.
    PROTESTO e NEGATIVACAO levantam `NaoSuportado`.
15. **PDF.** Nao ha endpoint de segunda via; `PDF` nao e declarada.
16. **Webhook.** O BB tem webhook de baixa operacional, mas a homologacao nao dispara
    liquidacao real. `parse_webhook` levanta `NaoSuportado`.
17. **Listagem (`GET /boletos` com filtros).** Fora da primeira fatia. Na homologacao, a
    listagem com `indicadorSituacao=A` respondeu 503 "problema tecnico".
18. **Homologacao.** Resolvida: o ambiente "sandbox" do adapter aponta para a homologacao
    (`api.hm` e `oauth.hm`), a unica que funciona para aplicacao "Em teste".
19. **Instrucao logo apos o registro.** O BB recusa prorrogacao, abatimento e baixa com
    `4923876.1` ("Boleto em processamento, registrado ha menos de 30 minutos"). O adapter
    transforma essa recusa em `Indisponivel` (retryable), sem retry no transporte: quem chama
    agenda a nova tentativa. Na homologacao a janela passou de uma hora: registro as ~12:50,
    recusa as 13:23 e aceite as 14:30. A prorrogacao aparece na consulta seguinte; o
    abatimento e a baixa sao processados de forma assincrona (minutos).

## Pix v2 (perfil `bb` do `pix_bacen`)

`validado_sandbox = True` desde 2026-10-28: cob e cobv registradas, consultadas, removidas e
liquidadas pelo simulador. Host `api.hm.bb.com.br/pix/v2`, sem mTLS; o `api-pix.hm` exige
certificado. A homologacao nao dispara webhook.

## Pagamentos em Lote v1 (`bb_pagamento`)

`validado_sandbox = True`: boleto, guia, Pix e transferencia entre contas BB aceitos, consultados,
liberados e consultados de novo na homologacao (host comum, sem mTLS). Em producao so o host mTLS.

Confirmado na homologacao:

- O numero da requisicao vem de `GET /proximos-numeros-requisicao` e e unico por contrato. O
  adapter reserva antes de enviar; com o numero fixo, o retry do transporte nao duplica.
- A consulta do lote e por tipo (`/lotes-boletos/{n}/solicitacao` e afins). A generica
  `/{n}/solicitacao` responde "Modalidade de pagamento invalida" para boleto, e
  `/requisicoes/{n}` responde 404.
- Os nomes mudam por tipo e por operacao: id em `codigoIdentificadorPagamento`,
  `identificadorPagamento`, `identificadorTransferencia` ou `codigoPagamento` (este, na
  consulta de lote de guias, nao esta no OpenAPI); aceite em `indicadorAceite` ou
  `indicadorMovimentoAceito`; erros em `erros`, `errors` ou `errorCodes` (zeros sao "sem erro").
  O id vem como texto no POST de boletos e como numero na consulta.
- `estadoPagamento` vem em maiusculas (CONSISTENTE, REJEITADO, PAGO). O boleto de teste e
  compartilhado e sai REJEITADO com motivo 295 quando outra aplicacao ja o pagou.

Duvidas:

1. **BLOQUEADO.** A doc lista o estado sem dizer se volta a andar; o adapter recusa com
   `Validacao` em vez de adivinhar.
2. **Poupanca.** O `tipoConta` para poupanca (Pix por dados bancarios e transferencia) nao foi
   confirmado; o adapter levanta `NaoSuportado`.
3. **Numero repetido.** Nao se sabe o codigo do BB para `numeroRequisicao` ja usado; se nao vier
   409, um timeout seguido de retry pode chegar como `Validacao`. O caminho seguro e consultar o
   lote pelo numero reservado.
4. **Finalidade da TED.** Obrigatoria para outro banco; vem da configuracao (tabela do BB), nao
   de cada pagamento.
5. **Webhook de pagamento.** Existe (estado PAGO de transferencias, Pix, boletos e guias), nao
   implementado.
6. **Liberacao.** A doc avisa que liberar antes de a remessa estar validada cria uma nova
   tentativa a cada 5 minutos; na homologacao a liberacao respondeu sucesso na hora.

## Extratos v2 (`bb_extrato`)

`validado_sandbox = False`: os hosts da v2 exigem certificado de cliente ate na homologacao, e a
aplicacao de teste ainda nao tem certificado. Contrato do OpenAPI 2.0.1. A v1 sai do ar em
20/11/2026 e nao e suportada.

Duvidas:

1. **404.** A doc diz "conta errada ou sem lancamentos"; o adapter devolve pagina vazia, entao
   conta errada nao se distingue de dia sem movimento.
2. **Linhas de saldo.** Historico 0 (saldo anterior ou parcial) e 999 (saldo final) viram
   `SaldoExtrato`; confirmar se ha outros historicos de saldo.
3. **Descricao.** O OpenAPI so traz `textoDescricaoSubHistorico`; o adapter aceita tambem
   `textoDescricaoHistorico`, citado na doc.
4. **Identidade.** `textoIdentificadorUnicoTransacao` so existe a partir de D-1; o id do
   lancamento e o hash dos campos estaveis (ver `brbank.extrato`).
