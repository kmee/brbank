# Sicoob (756) API Pagamentos Pix v2: duvidas

`SicoobPagamentoAdapter` (`brbank.adapters.sicoob_pagamento`, entry point `sicoob` em
`brbank.adapters_pagamento`). `validado_sandbox = False`: o sandbox nao executa o pagamento por
QR (ver "Validacao no sandbox"). Nada rodou em producao nem com conta de cooperado.

## Fontes

- Swagger oficial "Pix Pagamentos" 2.0.24.14 (OpenAPI 3.0), lido em 29/10/2026 pela mesma rota
  que o portal developers.sicoob.com.br usa (`GET https://api.sicoob.com.br/portal-developers/v2/swaggers/pagamentos-pix`
  com o header `client_id` publico do proprio portal; sem ele o gateway responde 401).
- Sandbox publico (`https://sandbox.sicoob.com.br/sicoob/sandbox/pix-pagamentos/v2`), com o mesmo
  Client ID e token fixos do sandbox de boleto e Pix recebimento (ver `docs/sicoob-duvidas.md`).
- Regra do BCB: quem le e valida o JWS do QR dinamico e o PSP do pagador. O integrador so manda
  o texto do copia e cola; `brbank` confere o CRC e o formato (`ler_brcode`) antes de enviar.

## Mapa da API

| Rota | Uso no adapter | Escopo |
| --- | --- | --- |
| `POST /pagamentos/qrcode` | `pagar_lote` de `PIX_QRCODE`: paga o copia e cola, execucao direta | `pixpagamentos_escrita` |
| `POST /pagamentos` | `pagar_lote` de `PIX` por chave: consulta o DICT, devolve `endToEndId` e dono da chave | `pixpagamentos_escrita` |
| `POST /pagamentos/confirmacao` | efetiva o Pix por chave (`meioIniciacao` CHAVE, com o e2e) ou por dados bancarios (MANUAL) | `pixpagamentos_escrita` |
| `GET /pagamentos/{endToEndId}` | `consultar_pagamento` | `pixpagamentos_consulta` |
| `PUT/GET/DELETE /pagamentos/webhook` | `configurar_webhook_pagamento`, `consultar_webhook_pagamento`, `remover_webhook_pagamento` | `pixpagamentos_webhook` |
| callback `{webhookUrl}/lancamento` | `parse_webhook_pagamento` | |

OAuth2 client credentials com mTLS no realm `cooperado` (mesmo token da cobranca, sem
`client_secret`); o adapter recusa producao sem certificado.

## Como cabe no `AdapterPagamento`

- O Sicoob paga um por vez: capacidade `LOTE_LOCAL`. `pagar_lote` manda linha a linha,
  `reservar_numero_lote` gera um numero local (uuid), `consultar_lote` levanta `NaoSuportado` e o
  estado de cada linha se acompanha pelo `endToEndId` (`consultar_pagamento`) ou pelo webhook.
- Nenhuma rota tem chave de idempotencia. A flag `repeticao` vai sempre `false` (o banco recusa o
  que considerar pagamento repetido; o criterio nao esta documentado). As chamadas que movem
  dinheiro (QR e confirmacao) saem sem retry automatico (`SEM_RETRY` do transporte); sem
  resposta, a linha volta `INDETERMINADO` e as seguintes nem saem (`RECUSADO`, motivo
  `NAO_ENVIADO`). No Pix por chave o e2e ja existe antes de pagar, entao a linha indeterminada
  traz o e2e e se resolve pela consulta; no QR nao ha identificador antes da resposta, e o
  chamador confere pelo extrato ou webhook antes de reenviar.
- A iniciacao do Pix por chave nao move dinheiro e usa o retry padrao. Se o documento do
  favorecido foi informado (`DestinoPix.documento`) e o dono da chave no DICT e outro, o adapter
  nao confirma: linha `RECUSADO`, motivo `DOCUMENTO_DIVERGENTE`.

| `estado` do Sicoob | `StatusPagamento` |
| --- | --- |
| `EM_PROCESSAMENTO` | `EM_PROCESSAMENTO` (`AGENDADO` quando `dataAgendamento` e futura) |
| `FINALIZADO` | `PAGO` |
| `REJEITADO`, `NÃO_REALIZADO` | `REJEITADO` (motivo: `detalheRejeicao`) |
| callback `FINALIZADO_SUCESSO` / `FINALIZADO_REJEICAO` | `PAGO` / `REJEITADO` |

## Validacao no sandbox (29/10/2026)

Teste: `tests/integracao/test_sicoob_pagamento_sandbox.py` (mesmas variaveis do sandbox de boleto).

| Chamada | Resultado real |
| --- | --- |
| `POST /pagamentos` com `{"chave": ...}` | 200, exemplo fixo (`endToEndId` "stringstring...", `proprietario` "string") |
| `POST /pagamentos/confirmacao` (CHAVE e MANUAL, corpo do adapter) | 200, exemplo fixo com `estado` "NÃO_REALIZADO" e `endToEndId` "string" |
| `POST /pagamentos/confirmacao` com `valor` "1.99" | **400**: o valor da confirmacao e com virgula ("1,99"), como no swagger |
| `GET /pagamentos/{e2e de 32 posicoes}` | 200, exemplo fixo; e2e de outro tamanho da 400 no gateway |
| `POST /pagamentos/qrcode` | **sempre 400** com o exemplo do swagger ("Dados do campo 'origem' precisam ser preenchidos manualmente"), para qualquer corpo, inclusive um corpo valido e um vazio |
| `PUT /pagamentos/webhook` | 200 sem corpo; com `Accept: application/json` o firewall responde **200 com uma pagina HTML "Request Rejected"** |
| `GET` / `DELETE /pagamentos/webhook` | 200 com exemplo / 204 |

O mock valida o corpo contra o swagger (obrigatorios, padroes) e devolve o exemplo. Na rota do QR
o unico exemplo do swagger e o de erro, entao o sucesso do pagamento por copia e cola nao tem
prova no sandbox: por isso `validado_sandbox` segue `False`. Ficam provados rota, autenticacao,
cabecalhos e corpo do Pix por chave e por dados bancarios, o formato do erro (Problema, RFC 7807)
e o webhook.

### Divergencias achadas e corrigidas

1. **Valor com virgula na confirmacao, com ponto no QR.** O swagger declara padroes diferentes
   (`^[0-9]{1,13}([,][0-9]{1,2})?$` na confirmacao, `^\d{1,16}\.\d{2}$` no QR); o sandbox
   confirmou o 400 com ponto na confirmacao. O adapter usa um formato por rota.
2. **Firewall no PUT do webhook.** Com o `Accept: application/json` que o transporte manda por
   padrao, o PUT volta 200 com HTML de bloqueio. O adapter manda `Accept: */*` nessa chamada, e
   uma resposta que nao e JSON vira `Validacao` (nunca sucesso falso).
3. **`origem` obrigatoria.** O swagger diz que e opcional na confirmacao (vale a do certificado),
   mas o exemplo de erro do proprio banco diz que precisa ser preenchida. O adapter sempre manda.

## Duvidas

1. **QR em producao.** Nada exercitado: confirmar com cooperado o retorno do QR estatico e do
   dinamico (imediato e com vencimento), os valores de `juros`, `multa`, `desconto` e
   `abatimento`, e se o `endToEndId` vem na resposta (o adapter trata resposta 200 sem e2e como
   `INDETERMINADO`).
2. **Duplicidade.** Como o banco aplica `repeticao: false` (mesmo QR? mesmo valor e destino em
   janela de tempo?) e o que responde: nao documentado.
3. **Conta de origem.** `conta` vai com o DV colado (`conta_com_digito`, padrao `True`); confirmar.
   O ISPB padrao e o do Banco Sicoob (02038232); cooperativa com ISPB proprio no Pix informa o
   dela (`SicoobPagamentoConfig.ispb` ou `ContaBancaria.ispb` da conta de debito).
4. **`dataVencimento` e `codMun` no QR.** Opcionais; o adapter manda `Pagamento.vencimento` e
   `SicoobPagamentoConfig.codigo_municipio` quando informados. O efeito (feriado municipal,
   calculo de encargos) nao esta descrito.
5. **Agendamento.** `dataAgendamento` vai quando a data e futura. Nao ha rota para cancelar um Pix
   agendado: `CANCELAMENTO` nao e declarada.
6. **Webhook.** O callback nao e assinado (o swagger nao fala em mTLS de volta); o exemplo usa
   `status` e o schema `estado`: o adapter le os dois. Quem recebe deve reconsultar antes de dar
   baixa.
7. **Outras APIs de pagamento do Sicoob** (fora deste adapter, swaggers no mesmo portal):
   "Cobranca Bancaria Pagamentos" v4 (`/pagamentos/v4`: pagar boleto por codigo de barras,
   comprovante, cancelar agendamento, DDA), "Convenios Pagamentos" v2 (arrecadacao por codigo de
   barras: guias) e "SPB Transferencias" v2 (TED). Cada uma e um passo seguinte (boleto, guia e
   transferencia) com o mesmo padrao de lote local.
