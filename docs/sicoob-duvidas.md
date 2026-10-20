# Sicoob (756) API de Cobranca v3: duvidas

`SicoobAdapter.validado_sandbox = True` desde 2026-10-20 (ver "Validacao no sandbox").
Nada foi executado em producao nem com conta de cooperado.

## Fontes

- Referencia de comportamento LIDA (LGPL, nenhum codigo portado, usada so como
  documentacao de endpoints, campos e quirks): ACBr, `ACBrBoletoW_Sicoob_V3.pas`,
  `ACBrBoletoRet_Sicoob_V3.pas`, `ACBrBancoSicoob.pas` e `Particularidades`.
- Swagger da v3 (API version 3.0.8.0): o portal `developers.sicoob.com.br` so o mostra com
  login; foi lido no espelho publico `open-fidias/sicoob-cobranca-bancaria-api`
  (`openapi.json`, cliente gerado a partir do swagger oficial). Serve de contrato para o que o
  sandbox nao confere (tamanhos maximos, listas de codigos).
- Sandbox publico (`https://sandbox.sicoob.com.br/sicoob/sandbox/cobranca-bancaria/v3`), com
  o Client ID e o Access token (Bearer) fixos que o Sicoob mostra na pagina Sandbox do portal,
  iguais para todos os desenvolvedores e reproduzidos em SDKs publicos (por exemplo
  `romeugodoi/sicoob-php-sdk`). Os valores nunca entram no repo: o teste le de
  `BRBANK_SICOOB_SANDBOX_CLIENT_ID` e `BRBANK_SICOOB_SANDBOX_TOKEN`.
- Terceiros lidos: thread do forum ACBr sobre a migracao para a v3 e README do
  `mangati/sicoob-sdk` (escopo `boletos_inclusao`, `pdfBoleto`).

## Validacao no sandbox (2026-10-20)

Teste: `tests/integracao/test_sicoob_sandbox.py` (`uv run pytest -m sandbox tests/integracao`
com as duas variaveis definidas). Exercitado com o adapter, sem atalhos:

| Chamada | Resultado real |
| --- | --- |
| `POST /boletos` (corpo do adapter) | 200, `resultado` objeto com `nossoNumero` inteiro, `linhaDigitavel`, `codigoBarras`, `qrCode` |
| `GET /boletos?numeroCliente&codigoModalidade&nossoNumero` | 200, `situacaoBoleto` "Em Aberto" -> REGISTRADO |
| `PATCH /boletos/{nn}` prorrogacao e abatimento | 204 sem corpo; adapter reconsulta |
| `POST /boletos/{nn}/protestos` | 204 sem corpo |
| `GET /boletos/segunda-via?...&gerarPdf=true` | 200, `resultado.pdfBoleto` em base64 (PDF) |
| `POST /boletos/{nn}/baixar` | 204 sem corpo -> BAIXADO |

O que o sandbox e: um mock que **valida a requisicao contra o swagger** (tipos, obrigatorios,
formato de data, corpo objeto) e responde **sempre o mesmo exemplo** (`nossoNumero`
-2147483648, `seuNumero` "1235512", datas de 2018). Nao guarda estado. Logo ficam provados
rota, metodo, cabecalhos, corpo, codigo HTTP e leitura da resposta; ficam SEM prova a
transicao de estado (baixado depois da baixa, liquidado), o OAuth/mTLS de producao e os
escopos. O token nem e conferido: qualquer `Bearer` passa, mas sem `Authorization` ou sem o
header `client_id` vem 401.

### Divergencias achadas e corrigidas

1. **`codigoEspecieDocumento` e sigla, nao numero.** O adapter mandava inteiro
   (`codigo_especie_documento: int`); o sandbox respondeu 400 e o swagger confirma string de
   ate 3 letras (DM, DS, NP, FAT, OU...). Agora `SicoobConfig.codigo_especie_documento: str`,
   validado no construtor (numero ou sigla fora do padrao vira `Validacao`).
2. **`codigoCadastrarPIX` sem Pix e 2.** O swagger lista 0 padrao, 1 com Pix, 2 sem Pix; o
   adapter mandava 0 quando `cadastrar_pix=False`, o que deixa a decisao com o contrato. Agora
   manda 2. (O sandbox aceita os dois; a correcao vem do swagger.)
3. **`tipoHistorico` string ou numero.** O swagger declara string; o exemplo do sandbox traz
   numero. A liquidacao agora compara pelo texto ("6").
4. **PDF existe.** `GET /boletos/segunda-via?gerarPdf=true` devolve `pdfBoleto` em base64:
   `obter_pdf` implementado e `PDF` declarada. O exemplo do sandbox tem padding em excesso
   (`=` sobrando), que a decodificacao estrita recusa; o adapter decodifica tolerante e exige
   o cabecalho `%PDF`.
5. **Tamanhos do swagger.** O sandbox nao confere tamanho, a producao confere: `seuNumero`
   acima de 18 posicoes e recusado antes da chamada (cortar quebraria a conciliacao); nome,
   endereco, bairro e cidade do pagador sao cortados em 50/40/30/40, nome do beneficiario
   final em 40 e cada mensagem de instrucao em 40.

O servidor falso (`tests/sicoob_falso.py`) passou a reproduzir o sandbox: 400 para corpo em
lista, especie numerica, obrigatorio ausente e nosso numero nao numerico; 401 sem `client_id`;
`nossoNumero` inteiro; 204 sem corpo na baixa, alteracao e protesto; 204 na consulta de nosso
numero inexistente (swagger); segunda via com `pdfBoleto`.

## Duvidas

1. ~~**Corpo do POST /boletos: objeto ou lista?**~~ Resolvida: objeto. Lista vira 400 no
   sandbox. O adapter segue aceitando `resultado` objeto, lista ou `resultado.boleto`, mas o
   sandbox devolve objeto direto.
2. **Escopos.** O sandbox nao usa OAuth, entao nada mudou: so `boletos_inclusao` tem nome
   confirmado em fonte de terceiros; `boletos_consulta` e `boletos_alteracao` seguem suposicao
   configuravel em `SicoobConfig.escopos`. Confirmar com credencial de cooperado.
3. **Token sem `client_secret`.** Seguido (a autenticacao de producao e o certificado mTLS).
   Nao exercitado no sandbox, que usa token fixo.
4. **Nosso numero como id.** Parcial: o registro devolve `nossoNumero` inteiro e as rotas
   aceitam inteiro (texto nao numerico vira 400). Se o valor ja inclui o digito verificador
   nao da para saber no mock (o exemplo e -2147483648). O adapter usa o valor devolvido.
5. ~~**Consulta.**~~ Resolvida: `GET /boletos?numeroCliente&codigoModalidade&nossoNumero`, 200
   com `resultado`; o swagger preve 204 sem conteudo para nada encontrado, que vira
   `Validacao(status_http=204)`.
6. **Status.** O swagger lista so "Em Aberto", "Baixado" e "Liquidado" (o exemplo usa "Em
   Aberto"). CANCELADO continua mapeado por precaucao; VENCIDO e EXPIRADO seguem fora. Um
   boleto vencido e em aberto ainda e cobravel? Confirmar.
7. **Valor e data do pagamento.** Parcial: o exemplo do sandbox mostra o formato pt-BR no texto
   (`"... - R$ 0,75"`, num historico de tarifa) e `dataHistorico` AAAA-MM-DD. O texto exato do
   historico de liquidacao (tipo 6) nao aparece no mock.
8. ~~**PDF.**~~ Resolvida: segunda via com `gerarPdf=true` (ver acima).
9. **Webhook.** O swagger da v3 tem `/webhooks` (cadastro, reativacao, solicitacoes), mas nao
   descreve o corpo do callback. `WEBHOOK` segue nao declarada.
10. **Movimentacao (fora do escopo).** `POST /boletos/movimentacoes` e download existem no
    swagger; nao implementado.
11. **Alteracao.** Confirmado o formato: `PATCH /boletos/{nn}` com `numeroCliente`,
    `codigoModalidade` e um bloco por alteracao (`prorrogacaoVencimento`, `abatimento`,
    `desconto`, `multa`, `jurosMora`, `seuNumero`, `especieDocumento`, `pix`,
    `valorNominal`...), resposta 204. Protesto em `POST /boletos/{nn}/protestos` (204);
    cancelar e desistir do protesto sao `PATCH`/`DELETE` na mesma rota (nao implementados).
12. **Desconto.** O swagger lista 0 a 6; o adapter usa 2 (percentual ate a data). A
    alternativa 5 (percentual por antecipacao, dias corridos) continua possivel.
13. **Multa e juros.** Codigos confirmados no swagger (multa 0/1/2, juros 1/2/3). `dataMulta`
    e `dataJurosMora` devem ser maiores que o vencimento: o adapter usa vencimento + 1.
14. **Campos obrigatorios nao deduzidos.** `codigoEspecieDocumento` (sigla, da config),
    `codigoModalidade` (padrao 1), `numeroParcela` (1), `aceite` (false), emissao e
    distribuicao pelo banco. `dataEmissao` e a data do dia.
15. **Baixa automatica por dias** nao tem campo no swagger (existe `dataLimitePagamento`):
    o titulo com `dias_baixa_automatica` continua recusado.
16. **CPF/CNPJ.** Enviados sem mascara; CNPJ alfanumerico desconhecido. CEP so digitos (o
    sandbox nao confere formato do CEP).
17. **Cota.** Limite real desconhecido; padrao 2 por segundo.
18. **Erros.** Confirmado `{"mensagens": [{"codigo", "mensagem"}]}` no 400; o adapter nao
    interpreta o codigo. Erro de rota/gateway vem em outro formato
    (`{"httpCode", "httpMessage", "moreInformation"}`). Duplicidade sem codigo conhecido.
19. **Hibrido.** `qrCode` volta no registro e na consulta; `descricaoRejeicaoPix` (swagger)
    explica quando o QR nao foi gerado. O adapter ainda nao o le.
