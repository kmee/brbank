# Sicoob (756) API de Cobranca v3: duvidas em aberto

`SicoobAdapter.validado_sandbox = False`. Nada foi executado contra o Sicoob.

## Fontes

- Referencia de comportamento LIDA (LGPL, nenhum codigo portado, usada so como
  documentacao de endpoints, campos e quirks): ACBr, `ACBrBoletoW_Sicoob_V3.pas`,
  `ACBrBoletoRet_Sicoob_V3.pas`, `ACBrBancoSicoob.pas` e `Particularidades`.
- Documentacao oficial: o portal `developers.sicoob.com.br` nao entrega a especificacao
  sem login (WebFetch devolveu so o titulo da pagina). Nao foi possivel conferir o swagger.
- Terceiros lidos: thread do forum ACBr sobre a migracao para a v3 (URLs, mudancas de
  formato) e README do `mangati/sicoob-sdk` (escopo `boletos_inclusao`, `pdfBoleto`).

Antes de usar em producao: obter o swagger v3 e rodar a suite contra o sandbox
(token fixo do sandbox fica no portal, entra em `SicoobConfig.token_sandbox`).

## Duvidas

1. **Corpo do POST /boletos: objeto ou lista?** O ACBr monta um objeto; o swagger v3 pode
   exigir lista. O adapter envia objeto e aceita `resultado` como objeto ou lista (e
   `resultado.boleto` aninhado). Confirmar.
2. **Escopos.** So `boletos_inclusao` tem o nome confirmado em fonte de terceiros.
   `boletos_consulta` e `boletos_alteracao` sao suposicao, por isso ficam em
   `SicoobConfig.escopos` (chaves `incluir`, `consultar`, `alterar`).
3. **Token sem `client_secret`.** O ACBr envia so `client_id`, `scope`, `grant_type`
   (a autenticacao e o certificado mTLS, que tambem vale na chamada de token). Seguido.
   Token expira em ~300s segundo o SDK de terceiros; o cache usa `expires_in` da resposta.
4. **Nosso numero como id.** Consulta, baixa e alteracao usam `nossoNumero` na URL. O
   ACBr calcula e anexa o digito verificador antes de montar a URL; nao se sabe se o
   `nossoNumero` devolvido no registro ja o inclui. O adapter usa o valor devolvido como
   veio. Confirmar.
5. **Consulta.** `GET /boletos?numeroCliente&codigoModalidade&nossoNumero`; 204 (sem
   conteudo) vira `Validacao`. Campo de situacao: `situacaoBoleto` com textos como
   "Em Aberto", "Liquidado", "Baixado", "CANCELADO". Lista completa desconhecida.
6. **Status.** Mapeados so EM ABERTO, LIQUIDADO, BAIXADO, CANCELADO. `VENCIDO` e
   `EXPIRADO` existem como constantes no ACBr sem uso: ficam fora (viram `Validacao`)
   ate haver contrato. Um boleto vencido e em aberto ainda e cobravel? Confirmar.
7. **Valor e data do pagamento.** Nao ha campo numerico: o valor vem de texto em
   `listaHistorico[tipoHistorico=6].descricaoHistorico` apos "R$". O adapter interpreta
   formato pt-BR e, se falhar, devolve `valor_pago=None`. Formato real desconhecido.
8. **PDF.** Fora: o ACBr envia `gerarPdf=false` e so le `pdfBoleto` no registro. Existe
   provavelmente um endpoint de segunda via, nao confirmado. `PDF` nao e declarada.
9. **Webhook.** Nao ha contrato conhecido para boleto. `WEBHOOK` nao declarada,
   `parse_webhook` levanta `NaoSuportado`. O ACBr usa polling por movimentacao.
10. **Movimentacao (fora do escopo).** `POST /boletos/movimentacoes`, consulta da
    solicitacao e download de arquivo zip em base64 (siglas ENTR, PROR, AVENC, VENC,
    LIQUI, BAIX; janela de 2 dias). Util para conciliar em lote; nao implementado.
11. **Alteracao.** `PATCH /boletos/{nn}` com `prorrogacaoVencimento`, `abatimento` (e,
    na referencia, `desconto`, `jurosMora`, `multa`, `seuNumero`); protesto em
    `POST /boletos/{nn}/protestos`; negativacao em `/negativacoes` com verbos variados
    (nao implementada). O adapter cobre PRORROGACAO, ABATIMENTO, PROTESTO e BAIXA e devolve
    o estado reconsultado (a API nao devolve dados na alteracao).
12. **Desconto.** O modelo canonico traz percentual + dias antes; foi mapeado para
    `tipoDesconto=2` (percentual ate a data informada), data = vencimento - dias. A
    alternativa `5` (percentual por antecipacao em dias corridos) pode ser mais fiel.
13. **Multa e juros.** Percentual de multa = `tipoMulta 2`; juros mensal = `tipoJurosMora 2`.
    Data de inicio (`dataMulta`, `dataJurosMora`) = vencimento + 1 dia, escolha do adapter
    (o ACBr reaproveita a data de juros). Sem encargo: `tipoMulta 0`, `tipoJurosMora 3`.
14. **Campos obrigatorios nao deduzidos.** `codigoEspecieDocumento` (sem padrao: vem da
    config, tabela do banco), `codigoModalidade` (padrao 1, simples com registro),
    `numeroParcela` (1), `aceite` (false), emissao e distribuicao pelo banco
    (`banco_emite_e_distribui`). `dataEmissao` e a data do dia, injetavel nos testes.
15. **Baixa automatica por dias** nao tem campo conhecido: o titulo com
    `dias_baixa_automatica` e recusado. Protesto e negativacao no registro (`codigoProtesto`,
    `numeroDiasProtesto`, `codigoNegativacao`) nao existem no modelo canonico.
16. **CPF/CNPJ.** Enviados sem mascara; se a API aceita CNPJ alfanumerico e
    desconhecido. CEP so digitos. Endereco = logradouro + numero (a API nao tem campo
    separado na referencia); complemento nao e enviado.
17. **Cota.** O ACBr limita a 2 envios por segundo; adotado como padrao
    (`requisicoes_por_segundo=2.0`). Limite real desconhecido.
18. **Erros.** O corpo de erro traz `mensagens[{codigo, mensagem}]` segundo a referencia;
    o adapter nao interpreta (o transporte mascara e repassa). Duplicidade nao
    mapeada para `Duplicado` por falta de codigo conhecido.
19. **Hibrido.** `HIBRIDO` e declarada fixa, mas depende de `cadastrar_pix=True`
    (`codigoCadastrarPIX=1`), que exige Pix habilitado na cooperativa. O QR vem em `qrCode`.
