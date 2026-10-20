# Itau (341) API de Cobranca e Bolecode: duvidas em aberto

`ItauAdapter.validado_sandbox = False`. Nada foi executado contra o Itau.

## Fontes

- Documentacao oficial: o portal `devportal.itau.com.br` so entrega a especificacao das
  APIs com login (WebFetch em 2026-10-20 devolveu so menu e o aviso "necessario login").
  Nenhum campo deste adapter foi conferido no swagger oficial.
- Referencia de comportamento LIDA (LGPL, nenhum codigo portado, usada so como
  documentacao de endpoints, campos e quirks): ACBr, `ACBrBoletoW_Itau_API.pas`,
  `ACBrBoletoRet_Itau_API.pas`, `ACBrBancoItau.pas` e `Particularidades`.
- Forum do ACBr (relatos de usuarios, nao oficiais):
  - topico 92306, "Descontinuacao do endpoint atual da API Bolecode" (maio a agosto de
    2026): endpoint novo do Bolecode, formato de valor de 17 digitos;
    https://www.projetoacbr.com.br/forum/topic/92306-aten%C3%A7%C3%A3o-descontinua%C3%A7%C3%A3o-do-endpoint-atual-da-api-bolecode-bolecode-pix-do-ita%C3%BA-nova-api-de-boleto-hibrido-dispon%C3%ADvel-para-testes/
  - topico 93570, erro "Versao nao implementada ou descontinuada pelo banco" no endpoint
    antigo; https://www.projetoacbr.com.br/forum/topic/93570-ita%C3%BA-bolecode-pix-%E2%80%94-previs%C3%A3o-de-suporte-no-svn-e-uso-da-unit-provis%C3%B3ria/
  - topico 80004 (out a dez de 2024), exemplo de payload do portal e o campo
    `quantidade_dias_apos_vencimento`; https://www.projetoacbr.com.br/forum/topic/80004-mudan%C3%A7as-necess%C3%A1rias-em-acbrboletow_itau_api/

Antes de usar em producao: obter o swagger das tres APIs (cash_management v2,
boletoscash v2 e recebimentos-pix v1) e rodar a suite contra o sandbox.

## Duvidas

1. **Endpoint do Bolecode.** O ACBr lido usa `pix_recebimentos_conciliacoes/v2/boletos_pix`,
   que o Itau descontinuou em 2026 (topicos 92306 e 93570). O adapter usa o novo,
   `https://pix-pj.api.itau.com/recebimentos-pix/v1` + `boletos-pix`; o caminho
   `boletos-pix` so aparece num log de sandbox publicado no forum. Base e caminho ficam
   configuraveis (`url_bolecode`, `caminho_bolecode`). O payload enviado ao endpoint novo e
   o do antigo (sem envelope `data`, com `dados_qrcode.chave`): se o novo mudou campos,
   nao se sabe.
2. **Quatro hosts.** Emissao CASH, alteracao e baixa em `api.itau.com.br/cash_management/v2`;
   consulta em `secure.api.cloud.itau.com.br/boletoscash/v2`; Bolecode em
   `pix-pj.api.itau.com/recebimentos-pix/v1`; token em `sts.itau.com.br/api/oauth/token`
   (sandbox `devportal.itau.com.br/api/jwt`). O perfil `itau` do `pix_bacen` usa outro
   token de producao (`sts.itau.com.br/as/token.oauth2`): qual vale para boleto nao foi
   confirmado. Todos configuraveis (`url_cobranca`, `url_consulta`, `url_token`).
3. **Token.** `client_credentials` com `client_id` e `client_secret` no formulario, sem
   escopo (como o ACBr). mTLS com o certificado dinamico do Itau em producao.
4. **`x-itau-correlationID`.** O ACBr manda o proprio client_id nesse header; o adapter manda
   um UUID novo por chamada (o nome indica correlacao e o perfil Pix do Itau usa UUID).
   Injetavel (`correlacao=`) para teste. Confirmar se o Itau aceita qualquer valor.
5. **Sandbox nao registra.** Fora de producao o ACBr manda `etapa_processo_boleto =
   validacao` (CASH) ou `simulacao` (Bolecode), que so valida o payload; a consulta
   seguinte nao acha o titulo. O forum (92306) diz que o sandbox do Bolecode novo nao
   estava disponivel em maio de 2026. Configuravel em `etapa_processo`. O sandbox Pix do
   Itau exige o header `x-sandbox-token`; se o de boleto exige, nao se sabe (nao enviado).
6. **Nosso numero.** O beneficiario gera (8 digitos, carteira com 3), como no ACBr:
   `gerar_nosso_numero` na config, padrao `seu_numero` numerico. `id_banco` = nosso
   numero; o caminho de alteracao e baixa = agencia (4) + conta (7) + DAC (1) + carteira
   (3) + nosso numero (8). Se o nosso numero devolvido na resposta tem DAC, nao se sabe:
   o adapter usa o valor devolvido como veio.
7. **Nosso numero reaproveitado.** Segundo o ACBr, o Itau pode reaproveitar o nosso
   numero 45 dias depois de liquidado, e a consulta devolve os dois titulos. O adapter
   le o ultimo item da lista. Confirmar a ordem.
8. **Formato de valores.** Emissao: texto de 17 digitos em centavos (`valor_total_titulo`,
   `valor_titulo`, `valor_abatimento`) e percentuais em 12 digitos com 5 casas
   (`000000200000` = 2%). O suporte do Itau confirmou ao forum (92306) o formato de 17
   digitos no Bolecode novo, embora a documentacao diga `12345.56`. Alteracao: decimal em
   texto (`"2.50"`), como no ACBr. Consulta: `valor_pago_total_cobranca` como decimal
   (`"150.50"`); texto de 17 digitos e lido como centavos.
9. **Consulta.** `GET boletos?id_beneficiario&codigo_carteira&nosso_numero&view=specific`
   (o ACBr diz que `view=full` nao traz pagamentos). Lista vazia vira `Validacao`; se o
   Itau devolve 404 ou 200 com `data: []` para titulo inexistente, nao se sabe (o servidor
   falso devolve 404).
10. **Status.** `situacao_geral_boleto` (e os codigos curtos do ACBr). Mapeados: `Em
    Aberto`, `E`, `Aguardando Pagamento` -> REGISTRADO; `Pago`, `Paga`, `L`, `BL`, `Baixa
    por ter sido liquidado` -> LIQUIDADO; `Baixado`, `Baixada`, `B`, `BA` (baixa para
    acerto), `BC` (baixa por protesto) -> BAIXADO. Fora (viram `Validacao`): `Pagamento
    devolvido`, `EA`, `TM`, `TS`. A tabela de status veio de e-mail do suporte do Itau ao
    ACBr ("nao tem nos manuais online"), nao de documento. `baixa.motivo_baixa = "Baixa por
    ter sido liquidado"` forca LIQUIDADO.
11. **Valor pago do Bolecode.** Pago por Pix (status `BL`) pode vir sem
    `pagamentos_cobranca` (o ACBr diz que o Bolecode e baixado e nao devolve valores):
    `valor_pago=None`. Conciliar pela API Pix (`pix_bacen`, perfil `itau`) ou pela
    francesinha `boletos/v3/francesas` (nao implementada).
12. **Alteracao.** `PATCH boletos/{id}/{campo}`: `data_vencimento` e `abatimento`
    implementados. O ACBr tambem usa `desconto`, `juros`, `multa`, `seu_numero`,
    `data_limite_pagamento`, `valor_nominal` e `protesto` (`codigo_tipo_protesto` 1, 4 ou
    9 com `quantidade_dias_protesto`). PROTESTO e NEGATIVACAO ficam `NaoSuportado`: a
    `Instrucao` canonica nao tem quantidade de dias. A alteracao nao devolve o titulo: o
    adapter reconsulta.
13. **Baixa.** `PATCH boletos/{id}/baixa` com corpo `{}` (ACBr).
14. **Baixa automatica.** Instrucao `codigo_instrucao_cobranca 8` (baixar apos XX dias). O
    ACBr manda `quantidade_dias_instrucao_cobranca`; o forum (80004) mostra o Itau
    recusando com 422 e exigindo `quantidade_dias_apos_vencimento`. Conflito sem
    resolucao: titulo com `dias_baixa_automatica` e recusado.
15. **Multa, juros e desconto.** Multa percentual `codigo_tipo_multa 02`, sem multa `03`
    (o ACBr sempre manda o objeto); juros percentual mensal `codigo_tipo_juros 90`, omitido
    sem juros; dias apos o vencimento = 1 (escolha do adapter). O exemplo do portal no
    forum usa `data_multa` e `data_juros` em vez de `quantidade_dias_*`: qual vale hoje,
    nao se sabe. Desconto percentual ate a data: `codigo_tipo_desconto 02` fora do array
    `descontos` (mudanca pedida pelo Itau ao ACBr em 11/2024).
16. **Campos fixos herdados do ACBr.** `tipo_boleto "a vista"`, `texto_uso_beneficiario
    "0"`, `indicador_pagamento_parcial false`, `quantidade_maximo_parcial 0`,
    `desconto_expresso false` (o ACBr manda o texto `"False"`; o adapter manda booleano).
    Especie pela tabela do ACBr (`DM` = `01`).
17. **Seu numero.** O ACBr fala em 10 posicoes obrigatorias na alteracao do seu numero. Na
    emissao o limite nao foi confirmado e o adapter nao corta nem recusa.
18. **Mensagens.** Sem campo conhecido para `Titulo.mensagens`: nao sao enviadas.
19. **Sacador avalista.** `Titulo.beneficiario` vai em `sacador_avalista` (pessoa e, se
    houver, endereco). Se o Itau exige endereco do sacador, nao se sabe.
20. **PDF.** Nao ha endpoint de PDF conhecido para boleto: `PDF` nao declarada.
21. **Webhook.** Sem contrato conhecido para boleto: `WEBHOOK` nao declarada,
    `parse_webhook` levanta `NaoSuportado`. O ACBr concilia por consulta e francesinha.
22. **Erros e duplicidade.** O corpo de erro traz `codigo`, `mensagem` e `campos[]`
    (ACBr); o adapter nao interpreta. Nosso numero repetido nao tem codigo conhecido e nao
    vira `Duplicado`.
23. **Cota.** Limite de requisicoes desconhecido; `requisicoes_por_segundo` sem padrao.
24. **Hibrido x CASH.** `bolecode=True` (padrao) exige `chave_pix` e declara `HIBRIDO`; com
    `bolecode=False` a instancia nao declara `HIBRIDO`. `SO_HIBRIDO` nao e declarada.
