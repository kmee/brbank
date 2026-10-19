# Banco do Brasil: duvidas em aberto do contrato

Fontes usadas, por ordem de confianca:

1. **Doc oficial (developers.bb.com.br / apoio.developers.bb.com.br):** NAO foi
   possivel ler. A pagina de referencia carrega por JavaScript e o WebFetch devolveu
   so "Aguarde...". WebSearch nao trouxe a pagina oficial. Tudo abaixo precisa ser
   conferido contra ela e contra o sandbox antes de producao.
2. **Leitura (so como documentacao, sem portar codigo, licenca LGPL) do ACBrBoleto:**
   `WS/ACBrBoletoW_BancoBrasil_API.pas`, `WS/ACBrBoletoRet_BancoBrasil_API.pas`,
   `ACBrBancoBrasil.pas` e a secao 3.1 de `Particularidades.html`. Dela vem: URLs,
   `gw-dev-app-key` em query, paths, nomes de campo do payload e da resposta, datas
   `DD.MM.YYYY`, composicao do `numeroTituloCliente`, tabela de `codigoEstadoTituloCobranca`,
   campos de alteracao (PATCH) e o header `x-bb-portal-devx-cnpj-parceiro`.
3. **Fonte de terceiro (wiki CIGAM, via WebSearch):** escopos
   `cobrancas.boletos-requisicao` e `cobrancas.boletos-info`.

O adapter nasce com `validado_sandbox = False`.

## Risco do PRD: chave de desenvolvedor e header de parceiro

- `gw-dev-app-key` e **query param** em toda chamada de negocio (nao em header),
  inclusive no PATCH e no POST de baixa. O adapter a monta em `_caminho`, mantem fora
  do repr da config e o transporte nunca loga URL.
- O header `x-bb-portal-devx-cnpj-parceiro` (CNPJ da software house) e opcional
  (`BBConfig.cnpj_parceiro`). Nao se sabe se o BB passara a exigi-lo.

## Duvidas

1. **Autenticacao do token.** O ACBr usa credenciais no corpo no sandbox e Basic
   (`client_id:client_secret`) em producao. O adapter replica isso por padrao
   (`BBConfig.credenciais_no_corpo=None`) e deixa forcar. Confirmar no oficial.
2. **Escopos.** Fonte de terceiro, nao oficial. O escopo depende dos servicos marcados
   no app do portal.
3. **Nosso numero.** `numeroTituloCliente` = "000" + convenio (7 digitos, zeros a
   esquerda) + nosso numero (10 digitos), pela montagem do ACBr. Para convenio de 6
   digitos o ACBr aplica a mesma regra na API. O modelo canonico nao tem nosso numero:
   quem chama passa `BBConfig.gerar_nosso_numero` ou usa `seu_numero` numerico de ate
   10 digitos. Quem garante a unicidade da sequencia e o chamador. Convenios de 4 e 5
   digitos (so CNAB) sao recusados.
4. **Duplicidade.** Nao se sabe qual codigo o BB devolve para `numeroTituloCliente`
   repetido. So o 409 vira `Duplicado`; a idempotencia real e o cache por
   `chave_idempotencia` (no processo) e o `Orquestrador`.
5. **Status do registro.** A resposta traz linha digitavel e codigo de barras, entao o
   adapter devolve `REGISTRADO` (nao `PENDENTE_REGISTRO`). Nao confirmado.
6. **Tabela de status.** Mapeados: 1, 2, 3, 4, 5, 8, 9, 13 (REGISTRADO), 6 (LIQUIDADO),
   7 (BAIXADO), 80 (transitorio, PENDENTE_REGISTRO). **Deliberadamente fora** (levantam
   `Validacao`): 10 (baixado/pago em cartorio), 11 e 12 (liquidado com protesto ou
   cartorio), 14 (em liquidacao), 15 e 21 (agendado), 16 (creditado), 17 (pago em
   cheque), 18 e 19 (pago parcialmente). Sem a doc oficial nao da para saber se cada um
   e dinheiro recebido; liquidar ou baixar por palpite e pior que recusar. Candidatos a
   LIQUIDADO apos confirmacao: 11, 12, 16.
7. **Pagamento parcial.** `indicadorPermissaoRecebimentoParcial` nao e enviado. O
   modelo canonico nao tem o conceito.
8. **Data de emissao.** O modelo nao tem `dataEmissao`; o adapter usa a data do dia
   (`hoje` injetavel). Confirmar se o BB aceita emissao posterior a hoje ou anterior.
9. **Multa.** O BB exige data de inicio; o adapter usa vencimento + 1 dia (suposicao).
   Tipo 2 = percentual. Juros: tipo 2 = taxa mensal. Desconto: tipo 2 = percentual ate
   a data, `dataExpiracao` = vencimento menos `desconto_dias_antes`. Tipos vindos do ACBr.
10. **Baixa automatica.** `dias_baixa_automatica` e recusado: a equivalencia com
    `numeroDiasLimiteRecebimento` (prazo de recebimento apos o vencimento) nao e
    certa. Mensagem: o BB limita a 30 caracteres; acima disso o adapter recusa em vez
    de truncar em silencio.
11. **CPF/CNPJ como inteiro.** `numeroInscricao` e `cep` vao como numero (como no
    ACBr). CNPJ alfanumerico nao cabe: recusado com `Validacao`. Perguntar ao BB.
12. **Pix no boleto (`indicadorPix`).** Configuravel. Quando "S" a resposta de registro
    traz `qrCode.emv/txId/url`, que o adapter expoe em `pix_copia_cola` e `txid`. A
    consulta de detalhe nao traz o Pix (ha `GET /boletos/{id}/pix`, nao implementado), e
    por isso `HIBRIDO` nao e declarada.
13. **Baixa.** Resposta com `dataBaixa` e `horarioBaixa`; o adapter devolve `BAIXADO`
    direto, sem reconsultar.
14. **Alteracao (PATCH).** Implementados: nova data de vencimento
    (`indicadorNovaDataVencimento` + `alteracaoData`) e abatimento (incluir se o titulo
    nao tem `valorAbatimentoTituloCobranca`, alterar se tem; o adapter consulta antes).
    Cancelar abatimento, desconto, juros, multa, valor nominal, protesto e negativacao
    existem na API mas ficaram fora (o modelo `Instrucao` nao carrega os dados, ou o
    efeito nao esta claro). PROTESTO e NEGATIVACAO levantam `NaoSuportado`.
15. **PDF.** Nao ha endpoint de segunda via no que foi lido; `PDF` nao e declarada.
16. **Webhook.** Nada no contrato lido. `parse_webhook` levanta `NaoSuportado`.
17. **Listagem (`GET /boletos` com filtros)** e a API de consulta por periodo ficaram
    fora da primeira fatia.
18. **Homologacao.** O ACBr tem um terceiro ambiente (`api.hm.bb.com.br`); o adapter so
    aceita `sandbox` e `producao`.
