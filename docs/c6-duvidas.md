# C6 Bank (336) API Bolepix v2: duvidas em aberto

`C6Adapter.validado_sandbox = False`. Nada foi executado contra o C6.

## Fontes

- **Oficial (fonte principal).** Especificacoes OpenAPI publicadas pelo portal
  `developers.c6bank.com.br` (a pagina e uma SPA atras do Cloudflare; os arquivos
  ficam em `/yamls/`), baixadas em 2026-10-20:
  - `bolepix.yaml` (API de Bolepix, versao 1.1.1): rotas `/v2/bank_slips`, payloads,
    status, listagem.
  - `bankslip-migration.yaml` (Guia de Migracao para a API de Bolepix, 1.1.0): mapa
    v1 -> v2, `external_reference_id` como identificador, status, notificacoes v2.
  - `authentication-api.yaml` (Autenticacao, 1.1.2): `POST /v1/auth/`, mTLS,
    `client_credentials` em formulario, token de 300 s.
  - `webhook-api.yaml` (Notificacoes, 1.0.10): servico `BANK_SLIP_PIX` e exemplos.
  - `bankslip-api.yaml` (Boleto v1, 1.7.2) e `release-notes.yaml` (1.3.11, 02/10/2026).
  - Pagina "Tratamento de Erros" (`/apis/errors`): erros no formato RFC 7807
    (`type`, `title`, `status`, `timestamp`, `correlation_id`, `detail`).
- **Referencia de comportamento LIDA** (LGPL, nenhum codigo portado, usada so como
  documentacao): ACBr, `ACBrBoletoW_C6.pas`, `ACBrBoletoRet_C6.pas`, `ACBrBancoC6.pas`
  e `Particularidades`. O ACBr fala a API **v1** (`/v1/bank_slips`, so boleto,
  carteira 15, nome do pagador cortado em 33). O adapter segue a v2 oficial.
- Terceiros (so contexto): artigo de ajuda da Omie sobre a integracao "Boleto + Pix"
  do C6 e o pacote PHP `divulgueregional/api-c6-bank` (lista de operacoes).

Antes de usar em producao: rodar a suite contra o sandbox (funciona de segunda a
sexta, das 07h as 22h, carteira 21) seguindo o "Roteiro de Testes - C6 Developers v3.0"
do portal.

## Decisoes e duvidas

1. **Produto e capacidades.** O PRD diz "C6 so bolecode". A v2 (Bolepix) emite boleto
   com ou sem Pix (sem `payment_method.pix` sai boleto puro) e a v1 e so boleto, entao
   o banco nao e "so hibrido": o adapter declara `HIBRIDO` e **nao** `SO_HIBRIDO`.
   O adapter sempre pede o Pix: `chave_pix` e obrigatoria e validada como EVP (a API
   so aceita chave aleatoria). Boleto sem Pix pela v2 ficou fora.
2. **Chave Pix recusada em silencio.** Pela especificacao, chave invalida ou nao
   cadastrada no C6 nao gera erro: a cobranca nasce sem Pix. O adapter devolve o
   titulo com `pix_copia_cola=None` e registra um aviso no log (sem PII); nao levanta
   erro porque a cobranca ja existe no banco.
3. **Identificador.** As rotas da v2 usam o `external_reference_id` (26 caracteres
   `[A-Z0-9]`), definido por quem integra; o `id` do C6 fica so na resposta bruta. O
   adapter deriva o `external_reference_id` da `chave_idempotencia`
   (`referencia_externa`: a propria chave se ja estiver no formato, senao os 26
   primeiros caracteres do SHA-256 em base32) e usa esse valor como `id_banco`.
   O guia diz que o mesmo `external_reference_id` enviado de novo devolve a cobranca
   existente: o registro e idempotente no banco. Codigo HTTP dessa resposta (201 ou
   200) nao documentado; o adapter aceita os dois.
4. **`description` obrigatoria (ate 100).** O modelo canonico nao tem descricao: o
   adapter envia o `seu_numero`. Confirmar se o C6 imprime esse texto no boleto.
5. **`your_number`.** Aceita so 1 a 10 digitos. O `seu_numero` vai como
   `your_number` apenas quando cabe nesse formato; senao fica so na `description`.
6. **Beneficiario final (sacador avalista).** A v2 nao tem campo. `Titulo.beneficiario`
   e ignorado. Confirmar com o C6 se ha forma de informar o sacador avalista.
7. **Endereco.** `address` = logradouro + ", " + numero (limite total 40);
   `neighborhood` obrigatorio desde 25/07/2026; `complement` deixou de existir na v2 e
   nao e enviado. Nome, endereco, bairro e cidade sao cortados em 40 caracteres
   (o ACBr relata que a v1 validava 33 no nome; nao se sabe se a v2 tambem). E-mail
   acima de 70 caracteres e recusado (`Validacao`) em vez de cortado.
8. **CPF/CNPJ.** `tax_id` vai sem mascara. A especificacao fala em digitos (11 ou 14);
   se aceita CNPJ alfanumerico e desconhecido.
9. **Multa, juros e desconto (`fees`).** Multa percentual = `fine_type PERCENTAGE`,
   juros mensais = `interest_type MONTHLY_PERCENTAGE` (o C6 divide por 30 ao dia),
   ambos com `*_deadline = 0`. Na v1, `dead_line 0` significa cobrar a partir do dia
   seguinte ao vencimento; a v2 so diz "dias apos o vencimento". Confirmar.
   Desconto percentual ate N dias antes = `discount_type MONTHLY_PERCENTAGE`,
   `first_discount_value`, `first_discount_deadline = N`: o guia de migracao mapeia o
   "P" da v1 para esse nome, mas o nome sugere percentual mensal proporcional. Confirmar
   o calculo no sandbox. A v2 so tem um desconto (o segundo e o terceiro da v1 sumiram).
10. **Abatimento** nao existe na v2 (nem no registro nem no PATCH): titulo com
    `encargos.abatimento` e recusado e a instrucao `ABATIMENTO` levanta `NaoSuportado`.
    Alterar o `amount` seria outra operacao (alteracao de valor), nao abatimento.
11. **Baixa automatica.** `dias_baixa_automatica` vira `days_after_due_date` ("dias
    apos o vencimento em que a cobranca expira"). Que status o C6 da a cobranca expirada
    nao esta documentado (nao ha `EXPIRED` na v2): se aparecer, a consulta levanta
    `Validacao` ate a tabela ser atualizada.
12. **Status (tabela 2026.10).** `CREATED` -> REGISTRADO, `PAID` -> LIQUIDADO,
    `CANCELED` e `CANCELLED` (grafia da v1 e da tabela de notificacoes) -> BAIXADO,
    `WAITING_CONFIRMATION` -> REGISTRADO. Este ultimo e "pago, mas o dinheiro ainda nao
    entrou na conta": o guia manda tratar como intermediario e nao como pago
    definitivo, e o modelo canonico nao tem estado intermediario. Qualquer outro
    codigo vira `Validacao`.
13. **Valor e data do pagamento.** A consulta da v2 nao traz `payments` (so a
    listagem). Para titulo `PAID`, o adapter chama `GET /v2/bank_slips/list` com
    `external_reference_id` e `due_date_from = due_date_to = vencimento atual` (a
    listagem exige um intervalo de datas) e soma os `payments[].amount` (texto, ex.
    "123.45"); `data_pagamento` = o `payment_date` mais recente (o `credit_date` fica
    na resposta bruta). Se a consulta vier com `payments`, eles sao usados direto. Se
    a listagem nao achar o titulo, `valor_pago=None`. Confirmar se o filtro por
    vencimento acha titulo cujo vencimento mudou depois do pagamento.
14. **Alteracao.** `PATCH /v2/bank_slips/{external_reference_id}` (corrigido nas
    release notes de 18/09/2026: sem sufixo `/update`). Implementada so a
    PRORROGACAO (`due_date`); a resposta traz a cobranca atualizada. Protesto e
    negativacao nao existem na API: `NaoSuportado`.
15. **Baixa.** `PUT /v2/bank_slips/{external_reference_id}/cancel`, 204 sem corpo. Status
    `PAID` e `CANCELED` sao terminais; o erro de cancelar titulo pago chega como erro
    HTTP e vira `Validacao` pelo transporte.
16. **PDF.** `GET /v2/bank_slips/{external_reference_id}/pdf` com `Accept:
    application/pdf`. O corpo da resposta nao e descrito: o adapter aceita o binario e,
    por seguranca, JSON com `base64_pdf_file` (formato da v1).
17. **Webhook.** Servico `BANK_SLIP_PIX`, cadastrado em `/v1/webhooks/` (cadastro fora
    do adapter). Corpo: `external_id`, `date_time`, `client_id`, `partner_id`,
    `service`, `status` e `information` (JSON em texto). Sem assinatura documentada:
    reconsultar antes de liquidar. Contradicoes da propria documentacao: o guia diz que
    na v2 o `external_id` e o `external_reference_id`, mas nos exemplos ele e o `id` do
    C6; o adapter usa `information.external_reference_id`. O exemplo de pagamento em
    `webhook-api.yaml` traz `information` dentro de `information`; o do guia, nao. O
    adapter aceita os dois. Notificacao de outro servico (ex. `BANK_SLIP` da v1) e
    recusada. `valor_pago` do evento so em `PAID`.
18. **PII na resposta bruta.** O `mascarar` da biblioteca conhece as chaves em
    portugues; o C6 responde em ingles (`name`, `tax_id`, `address`). O adapter troca o
    objeto `payer` inteiro por `***` antes de guardar a resposta. Erros do banco passam
    pelo `mascarar` do transporte (CPF/CNPJ em texto livre sao mascarados). Sugestao
    para o nucleo: incluir chaves em ingles no `mascarar`.
19. **Escopo do token.** O pedido de token nao leva `scope`; a resposta traz os escopos
    da credencial (ex. `bankslip.write bankslip.read`). Token de 300 s, renovado pelo
    cache por `expires_in`; a doc tambem fala em expiracao por 10 minutos de
    inatividade.
20. **Cabecalhos `partner-software-name` e `partner-software-version`** sao opcionais e
    so vao quando `C6Config.software_nome`/`software_versao` estao preenchidos.
21. **Cota.** Limite de requisicoes nao documentado (`requisicoes_por_segundo=None`).
22. **Duplicidade e erros.** Nao ha codigo de erro conhecido para duplicidade (o C6
    devolve a cobranca existente). Erros seguem RFC 7807; o adapter nao interpreta o
    `type` (o transporte mapeia por status HTTP).
