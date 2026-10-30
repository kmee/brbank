# Bradesco (237), API de pagamentos: duvidas

`BradescoPagamentoAdapter` (`brbank.adapters.bradesco_pagamento`): Pix por chave e por dados
bancarios, boleto de cobranca, guia e conta de consumo com codigo de barras e TED.
`validado_sandbox = False`: so servidor falso. O sandbox exige organizacao cadastrada no portal e
certificado (autoassinado serve, com nome e CNPJ da empresa no CN). Nada rodou contra o banco.

**Isto corrige a pesquisa anterior.** A conclusao "o Bradesco nao tem API publica de pagamento" e a
nota de `docs/bradesco-duvidas.md` ("a especificacao fica atras de login") estao erradas: a pagina
do portal e so JavaScript, mas o proprio portal le uma API JSON aberta.

## Fontes

### Oficiais, lidas em 30/10/2026, sem login

API do Marketplace (Axway): `https://developers.bradesco.com.br/api/v1/products` lista os produtos;
`.../products/{id}/versions/{v}/assetresources` traz cada OpenAPI (`.../assetresources/{r}/data`);
`.../documents` traz manual e collection Postman. O certificado do host nao fecha a cadeia em todo
cliente Python; `curl` com o repositorio do sistema le normal.

| Produto (categoria Payments) | id | Versao | Rotas |
| --- | --- | --- | --- |
| Pix - transfers | `8a2e97a5954934d101954e3b25bb36c2` | 2.3.2 | `/v1/spi/solicitar-transferencia`, `/v1/spi/consulta/transferencia/{id}[/{e2e}]` |
| Payment of bills | `8a2e8ca29549347701954cd081122294` | 3.2.7 | `/boleto/pagamento-cobranca/v1/...` |
| Tax Payments and Consumption Bills with Barcodes | `8a2e88539549335b01954ddb496a5501` | 1.2.3 | `/pagamento/arrecadacao-via-codbarras/v1/...` |
| TED - Same-Day Bank Transfer | `8a2e855c95baabc40195cf2119c900ae` | 1.1.6 | `/transferencia/ted/v1/efetiva`, `/consulta` |

Tambem no portal: "Atualizacao para o CNPJ Alfanumerico"
(`/api/v1/documents/04bbb092da7546eb9bc31e53af8559c3/data`) e "First steps"
(`8a2e82cd96a2f2fe0196cb3013f65539`, hosts e certificados). Manuais em PDF em
`assets.bradesco/content/dam/portal-bradesco/gestao-api-payments/`.

### De terceiros (so confirmam que a API existe e e usada; nenhum campo vem deles)

- Fortes Tecnologia, "API Pagamento Bradesco"
  (`https://ajuda.fortestecnologia.com.br/kb/pt-br/article/497836/api-pagamento-bradesco`, 10/2024):
  ERP em producao com boleto, tributo com barras, TED e Pix; cadastro em producao por e-mail a
  `suporte.api@bradesco.com.br`; ativacao as tercas e quintas; diz "cancelamento de pagamento nao
  disponivel" (o ERP nao usa; a API de boleto tem a exclusao de agendamento).
- Scribd, copia do manual de 10/2023 de boleto e de Pix (titulos e trechos do buscador; so a capa e
  publica, nao foi usada).

## Mapa e encaixe no `AdapterPagamento`

| Rota | Uso |
| --- | --- |
| `POST /auth/server-mtls/v2/token` (form, mTLS) | token do boleto, guia e TED |
| `POST /auth/server/oauth/token` em `qrpix` (ver conflito 1) | token do Pix |
| `POST /v1/spi/solicitar-transferencia` | `pagar_lote` de `PIX` |
| `GET /v1/spi/consulta/transferencia/{id}` | `consultar_pagamento` de `PIX` |
| `POST .../cobranca-valida-titulo-pagamento/validaTituloPagamento` | passo 1 do boleto (vencimento, `numeroCtrlCip`) |
| `POST .../cobranca-pre-efetivacao/pre-efetivacao-pagamento` | passo 2 do boleto |
| `POST .../cobranca-efetivacao/solicitacao/executar` | passo 3 do boleto: paga |
| `POST .../cobranca-pagamento-consulta/consulta-pagamento-especifico` | `consultar_pagamento` de `BOLETO` |
| `POST .../cobranca-excluir-agendamento/exclusao/executar` | `cancelar_pagamentos` (so boleto) |
| `POST /pagamento/arrecadacao-via-codbarras/v1/pagamentoContaConsumo` | `pagar_lote` de `GUIA`: `tipoRegistro` 0 valida, 1 paga |
| `GET .../{agencia}/{conta}/{tipoConta}?tipoConsulta=3,4,5&segmentoConsulta=99&...` | `consultar_pagamento` de `GUIA` |
| `POST /transferencia/ted/v1/efetiva` | `pagar_lote` de `TRANSFERENCIA` |
| `GET /transferencia/ted/v1/consulta?numeroDocumento=&dataOperacao=` | `consultar_pagamento` de `TRANSFERENCIA` |
| `.../cobranca-parametros-pgto/executar`, `.../cobranca-agendamentos-pgto/listar`, `.../cobranca-lista-pagamento-devolvido/listar`, `.../cobranca-alterar-agendamento/alteracao/executar` | fora (limites, lista, devolvidos, alterar agendamento) |
| `.../boleto/cobranca-pagamento/v1/...` | fora: versao depreciada, sem CNPJ alfanumerico |

Produto "Direct debit" e "Vehicle Debt" nao entram: nao sao pagamento a fornecedor.

Capacidades: `PIX`, `BOLETO`, `GUIA`, `RESERVA_NUMERO` (numero local), `LOTE_LOCAL`, `CANCELAMENTO`
(so agendamento de boleto) e `TRANSFERENCIA` (ver "Mudanca pedida").

## O que nao existe na documentacao publica

| Item | Consequencia |
| --- | --- |
| Pix copia e cola: o produto de Pix nao tem campo para o BR Code, embora a tabela de erros cite `E1141` e `BBE17` | `PIX_QRCODE` nao declarada. Perguntar no suporte do portal se ha produto ou versao com QR |
| Aprovacao em dois passos: nada no OpenAPI nem no manual | `LIBERACAO` nao declarada: **a chamada paga na hora**. Perguntar se a alcada do Net Empresa vale para a API |
| Lote no banco | `LOTE_LOCAL`; `consultar_lote` levanta `NaoSuportado` |
| Webhook de pagamento: o manual do Pix cita o certificado para webhook, mas o produto nao tem rota | `WEBHOOK` nao declarada; acompanhar por consulta |
| Agendamento de Pix: sem campo de data | Linha com `data` diferente de hoje e recusada antes de chamar o banco |
| Agendamento de TED: vedado no manual | Idem |
| Cancelamento de Pix, TED e guia | Sem rota. O OpenAPI da guia cita `tipoConsulta` "02 - Remover", sem contrato |
| Cota (`rate limit`): os OpenAPI mandam ler o manual, que nao traz | 1 requisicao por segundo por padrao, o manual proibe consulta em rajada |
| TED: id do cliente para consulta | Sem resposta na efetivacao nao ha como consultar: `INDETERMINADO` sem `id_banco`, so o extrato resolve |
| Corpo da resposta da efetivacao por erro de duplicidade e formato do erro 422 da guia e do TED | Lidos de forma tolerante (`causa`, `correlationId`, `retorno`, `codigoDeRetorno`) |

Enquanto o sandbox nao roda, a rota CNAB (Multipag) da localizacao Odoo continua sendo a opcao em
producao para pagamento em lote com alcada no banco. Quem responde: suporte do portal Developers,
`suporte.api@bradesco.com.br` e `suportetransacoes_api@bradesco.com.br` (erros negociais).

## Conflitos entre as fontes oficiais

1. **Token do Pix.** Manual 2.0.0 (06/07/2026) e collection: `POST /auth/server/oauth/token`, form
   com `client_id`, `client_secret` e `scope` vazio. OpenAPI "Token - OAuth Pix MTLS" 2.0.0:
   `POST /v2/oauth/token` com `Authorization: Basic`. Padrao: o manual, sem `scope`. O caminho e
   configuravel (`caminho_token_pix`); Basic exigiria outro autenticador.
2. **`valorTitulo` da efetivacao do boleto (o mais perigoso).** Manual: `100.99`. OpenAPI (integer,
   "9(13)V9(2)") e dois exemplos de sucesso da collection: `348974` e `10000`; outro exemplo da mesma
   collection: `3489.74`. A pre-efetivacao e sempre decimal. Padrao: reais, porque ler errado vira
   recusa por valor divergente (2112, CBCA0146), enquanto mandar centavos a um banco que le reais
   pagaria cem vezes mais num titulo de valor livre. `boleto_valor_em_centavos=True` troca. **Conferir
   no sandbox antes de producao.**
3. **`Authorization` com ou sem `Bearer`.** Manuais falam em Bearer Token; as collections mandam o
   token cru. Mesma duvida da cobranca. Padrao `Bearer`; `token_sem_bearer=True` manda cru.
4. **`tipoChave` de documento.** Manual e collection: `CPFCNPJ`. OpenAPI: `CPF-CNPJ`. Usado `CPFCNPJ`.
5. **Corpo do `POST` de Pix no OpenAPI** repete a resposta (exige `status`, `motivo`, `dataCriacao`,
   `pagador.cpfCnpj`). Seguido o manual e a collection, que batem entre si.
6. **`idtransacao` x `idTransacao`:** o pedido e os exemplos usam minusculo, a tabela de saida usa
   maiusculo. Nao se depende do campo da resposta: o id e o enviado.
7. **HTTP 202 do Pix:** o OpenAPI diz "transacao rejeitada"; a tabela de erros, "em processamento"
   (`E1466`, `S0001`). Vale o campo `status` do corpo.
8. **`versao` da exclusao de agendamento:** manual e OpenAPI dizem 2 (fixo); o exemplo diz 1. Usado 2.
9. **`versaoLayout` da consulta de boleto:** OpenAPI "1"; manual e collection tambem "001". Usado "1".
10. **`numeroFilial` do TED:** tabela e collection 5 posicoes (`00002`); exemplo do manual 4 (`0002`).
    Usado 5; `000023` e recusado na collection.
11. **Token do TED e da guia nas collections:** `/auth/server-mtls/v1/token`; manuais e OpenAPI, `v2`.
    Usado `v2`.
12. **URL do TED no aviso do CNPJ alfanumerico:** `/ted/v1/transferencia/consulta` passa a
    `/ted/v2/...`; manual e OpenAPI usam `/transferencia/ted/v1/consulta`. Mantido o do manual. Migrar
    quando o portal publicar a versao.
13. **`situacaoPagamento` da consulta:** a TABLE 1 esta na lista; a consulta especifica so diz "payment
    status". Assumida a mesma tabela.

## Escolhas do adapter

- **`INDETERMINADO`, nunca `RECUSADO`:** sem resposta, 429, 5xx, 2xx ilegivel, status desconhecido e os
  codigos que nao provam que o dinheiro ficou. Pix: `M0240` (ja concluida), `M0241` (em andamento),
  `M0108`, `4120`, `E4120`, `4121`, `5001`, `E1402`. Boleto: `CBCA0108`, `2135` (duplicidade, tambem na
  validacao e na pre-efetivacao), `CBCA0037`, `2264` (tempo excedido). Guia: `057`, `006`.
- **Antes de pagar:** banco fora do ar na validacao ou na pre-efetivacao volta `RECUSADO` com
  `NAO_ENVIADO` e para o lote, porque nada saiu da conta.
- **Ids:** Pix, UUID5 da conta e da chave (32 hexadecimais). Boleto, a chave se for numerica de ate 9
  digitos, senao um numero derivado: 9 digitos colidem cedo em volume alto (cerca de uma colisao a cada
  45 mil chaves), e colisao vira recusa, nao pagamento duplo. **Prefira chave numerica sequencial no
  boleto.** Guia, 9 posicoes em base 36. O banco nao aceita id repetido; depois de `RECUSADO`, uma nova
  tentativa precisa de chave nova, e nao se sabe se ele libera o id de transacao recusada (duvida 3).
- **`id_banco` opaco:** ver o docstring. A conta nao entra no id para nao vazar em log; por isso a
  configuracao leva agencia e conta e o lote de outra conta e recusado.
- **Boleto:** `beneficiario_documento` e obrigatorio no contrato mas nao tem campo na API; nao e enviado
  nem conferido. `formaPagamento` fixo 2, debito em poupanca e recusado. Portador = titular da conta
  (`pagador_documento`): o FAQ diz que so pagamento por intermediario usa documento diferente.
- **`dataQuitacao` futura** (titulo pago em dia nao util) devolve `AGENDADO`.
- **Status do boleto, 9 "Schedule reversed":** tratado como `CANCELADO`, sem certeza de que nunca houve
  debito.
- **TED:** `finalidade` 5 (fornecedores) por padrao, `ted_finalidade` troca. So para outros bancos: o
  manual veda destino no Bradesco. Status 23 volta `EM_PROCESSAMENTO`: a devolucao pode levar 60 minutos.
- **Pix:** pagador sempre por agencia e conta, sem o digito (o manual nao tem campo de digito).
  Documento do recebedor junto com a chave faz o banco conferir o dono da chave (`AP0039`).
- **Texto cortado no tamanho do campo:** descricao 30 (Pix), extrato e descricao 32, nome 60 (Pix) e 80
  (TED).

## Duvidas para o sandbox

1. Todas as divergencias acima, a comecar pelo `valorTitulo`.
2. A conta do pagador Pix leva digito? (`pagador.conta`: exemplo `41000`, sem campo de digito.)
3. Depois de `RECUSADO`, o mesmo id pode ser reenviado? O `id_banco` e por conta ou por CNPJ?
4. `pagamentoValorDiferentes` e `valorCobrado`: o adapter paga `Pagamento.valor` e deixa o banco recusar.
5. O nome de `nomeClienteRemetente` e obrigatorio? A tabela diz sim; o exemplo e a collection nao mandam.
6. Formato do `numeroCtrlCip` quando vem "0" na pre-efetivacao de titulo Bradesco.
7. Consulta da guia: o `regSaida` vazio e a forma de "nao achei" ou vem erro? O adapter trata os dois.

## Mudanca pedida em arquivo compartilhado

O TED exige o nome do destinatario e o contrato so tem `DestinoPix.nome`. Em `src/brbank/pagamento.py`,
depois de `favorecido_documento`:

```python
    # Nome do favorecido: o Bradesco o exige no TED.
    favorecido_nome: str | None = Field(default=None, max_length=80, repr=False)
```

Sem esse campo, `TRANSFERENCIA` fica fora de `capacidades_pagamento` (a classe olha o modelo) e o codigo
de TED so roda nos testes, por subclasse.
