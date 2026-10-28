# Banco do Brasil: homologacao (aplicacao "Em teste")

Como validar o brbank contra o BB de verdade. Exercitado em 2026-10-28 com a aplicacao
"Em teste" da KMEE no Portal Developers BB. Fontes: doc oficial do portal
(apoio.developers.bb.com.br, APIs 5, 23, 41, 80) e os OpenAPI oficiais
(`api.externo.bb.com.br/docs/v1/openapi/{id}/{versao}`), conferidos na homologacao.

## Credencial

O portal baixa um arquivo com `appKey` (a `gw-dev-app-key`), `clientID`, `clientSecret`,
`registrationAccessToken` (gestao do cadastro da aplicacao, nao usado aqui) e `basic`. O
arquivo nunca entra no repositorio. Os testes leem pelo caminho:

```bash
BRBANK_BB_HM_CREDENCIAIS=~/Downloads/credenciais-aplNNNNNN.txt \
    uv run pytest -m sandbox tests/integracao/test_bb_homologacao.py
```

OAuth: `https://oauth.hm.bb.com.br/oauth/token`, `client_credentials`, credenciais em Basic ou
no corpo (as duas funcionam), `scope` obrigatorio (sem ele: `invalid_scope`). O host
`oauth.sandbox` / `api.sandbox` serve certificado de outro dominio: nao usar.

## Hosts e mTLS

| API | Host de homologacao | mTLS na homologacao | Producao |
| --- | --- | --- | --- |
| Cobrancas v2 | `api.hm.bb.com.br/cobrancas/v2` | nao | `api.bb.com.br/cobrancas/v2`, sem mTLS |
| Pix v2 | `api.hm.bb.com.br/pix/v2` | nao (o `api-pix.hm` exige) | `api-pix.bb.com.br/pix/v2`, mTLS |
| Pagamentos em Lote v1 | `api.hm.bb.com.br/pagamentos-lote/v1` | nao (o `pagamentos-lote.mtls.api.hm` exige) | `pagamentos-lote.mtls.api.bb.com.br/v1`, mTLS |
| Extratos v2 | `extratos.mtls.api.hm.bb.com.br/v2` | sim | `extratos.mtls.api.bb.com.br/v2`, mTLS |
| BB Pay v2 | `checkout.mtls.api.hm.bb.com.br/v2` | sim | `checkout.mtls.api.bb.com.br/v2`, mTLS |

Sem certificado, os hosts mTLS respondem 400 "No client certificate provided". O certificado de
cliente e A1 de AC ICP-Brasil (de preferencia e-CNPJ) com EKU "Client Authentication", TLS 1.2,
cadastrado na aplicacao (Certificados, "Para consumir APIs", "Ambiente de teste") so com a
cadeia publica em PEM; leva ate 30 minutos para valer. A doc nao preve certificado autoassinado.

Webhook (o BB chamando o nosso servidor) e outro sentido: o servidor precisa de certificado de
AC confiavel e de validar o certificado cliente do BB. A partir de 04/11/2026 o certificado
cliente do webhook de producao passa a ser emitido pela PKI BB; quem recebe webhook inclui a
cadeia nova mantendo a antiga na transicao. Em homologacao o BB nao dispara webhook de
liquidacao (cliente de teste unico); o portal tem a "Sandbox Webhook" para simular o payload.

## Escopos concedidos a uma aplicacao com as APIs marcadas

- Cobrancas: `cobrancas.boletos-info`, `cobrancas.boletos-requisicao`.
- Pix: `cob`, `cobv`, `pix`, `webhook`, `lotecobv`, `payloadlocation` (`.read` e `.write`).
- Pagamentos em Lote: `pagamentos-lote.*` (lotes, boletos, guias, transferencias, transferencias
  Pix, pix, pagamentos, cancelar). `devolvidos-info` nao foi concedido.
- Extratos: `extrato-info`. BB Pay: `checkout.solicitacoes-*`, `checkout.participantes-*`.

## Massa de teste

Fixa e compartilhada por todas as aplicacoes (sujeita a interferencia). Os CNPJs abaixo nao
existem na Receita (BrasilAPI 404 em 2026-10-28).

- Cobrancas: convenio 3128557, carteira 17, variacao 35. O pagador pode ser qualquer CNPJ ou
  CPF valido.
- Pix: chave EVP `9e881f18-cc66-4fc7-8f2c-a795dbb2bfc1` (recebedor CNPJ 95127446000198).
- Pagamentos em Lote: conta pagadora agencia 1607, conta 99738672, DV X, contrato 731030.
  Destinos: boleto `00195313800000015000000003051284190526000117` (R$ 15,00, beneficiario
  95752207000129), guia `83630000000641400052836100812355200812351310` (R$ 64,14), Pix para
  `hmtestes2@bb.com.br`, `+5511985732102` ou conta 551 / 43814-6 (documento 95127446000198),
  transferencia para conta 551 / 60840-8 (CNPJ 84526081000158).
- Extratos: header `x-br-com-bb-ipa-mciteste` casado com agencia e conta: `704950857` com
  452 / 123873, `26968930` com 551 / 5087, `178961031` com 1505 / 1348.
- BB Pay: convenios 98825 e 99216.

## Simulador de pagamento (so homologacao)

Chave fixa publicada pelo BB (`gw-app-key=95cad3f03fd9013a9d15005056825665`, nao e segredo):

- Boleto: `GET https://api.hm.bb.com.br/testes-portal-desenvolvedor/v1/boletos-cobranca/{linha}/pagar`.
  Em 2026-10-28 respondeu 504 do gateway.
- Pix: `POST .../testes-portal-desenvolvedor/v1/boletos-pix/pagar` com `{"pix": "<copia e cola>"}`.
  Funcionou: a cobranca passou a `CONCLUIDA` em ate 20 segundos e o `PixBacenAdapter` devolveu
  `LIQUIDADO` com o valor pago.

## Resultado de 2026-10-28

| API | Adapter | Resultado |
| --- | --- | --- |
| Cobrancas v2 | `bb` | registro e consulta ok (estado 1, linha, codigo de barras e QR Pix); instrucao recusada com `4923876.1` (boleto em processamento) por mais de uma hora depois do registro; depois prorrogacao, abatimento e baixa aceitos (estado 7 minutos depois) |
| Pix v2 | `pix_bacen`, perfil `bb` | cob e cobv: registrar, consultar, remover e liquidar pelo simulador |
| Pagamentos em Lote v1 | `bb_pagamento` | boleto, guia, Pix (e-mail, telefone, conta) e transferencia entre contas BB: aceite por linha, consulta do lote, liberacao e consulta do pagamento |
| Extratos v2 | `bb_extrato` | so servidor falso: falta o certificado de cliente |
| BB Pay v2 | nenhum | falta o certificado de cliente; entra como `Checkout` do `AdapterRecorrencia` |
| DDA | nenhum | o BB nao tem API de DDA no portal |
