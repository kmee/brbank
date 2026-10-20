# Safra (422) API de Cobranca: duvidas em aberto

`SafraAdapter.validado_sandbox = False`. Nada foi executado contra o Safra (nem
homologacao nem producao). Levantamento feito em 2026-10-20.

## Fontes

Por ordem de peso (nenhuma e oficial; o swagger do Safra so e entregue a cliente
credenciado):

1. **ACBr**, lido como documentacao (LGPL, nenhum codigo portado):
   `Fontes/ACBrBoleto/WS/ACBrBoletoW_Safra.pas` (URLs, token, payload de inclusao, query
   da consulta), `WS/ACBrBoletoRet_Safra.pas` (resposta de inclusao em `data`, erros
   400/404/422, parser da consulta), `ACBrBancoSafra.pas` (nosso numero de 9 digitos) e
   `Particularidades.html` (so trata do CNAB; nada da API).
2. **Forum do ACBr, topico ACBR-9304** (abril de 2026), que cita o swagger oficial
   `registro-boleto-request`:
   <https://www.projetoacbr.com.br/forum/topic/91950-acbr-9304-acbrboletow_safra-corre%C3%A7%C3%A3o-safra-correlation-id-fixo-e-tipos-divergentes-do-swagger-oficial/>
   (`Safra-Correlation-ID` unico por requisicao; `agencia`, `conta` e `documento.numero`
   como `number`; `mensagens` como lista de `{posicao: "1"|"2", descricao}`; existencia de
   exemplos Postman de "Instrucao/Baixa", sem rota publicada).
3. **Forum do ACBr, conta com digito** (agosto):
   <https://www.projetoacbr.com.br/forum/topic/92116-acbr-9304-banco-safra-altera%C3%A7%C3%B5es-no-arquivo-acbrboletow_safra/>
   (`conta` = numero sem zeros a esquerda + digito, no POST e no GET; sem isso a consulta
   devolve 503 generico).
4. **Kobana** (ajuda publica): boleto pede Client ID, usuario e senha; certificado so para
   Pix; pela API o Safra oferece "registro e obtencao de boletos".
   <https://ajuda.kobana.com.br/pt-BR/articles/8886365-como-ativar-a-conexao-via-api-com-o-banco-safra>,
   <https://ajuda.kobana.com.br/pt-BR/articles/11943602-quais-operacoes-do-banco-safra-estao-disponiveis-pela-kobana>
5. **Integracoes de terceiros no GitHub**, lidas so como confirmacao de formato:
   `JuniorMeloDev/FIDCIJJ_Full` (`src/app/lib/safraService.js`: token `password` com
   `client_id`, `Safra-Correlation-ID` com UUID novo, resposta em `data`) e
   `edubencz/karavan` (`route-cobranca-safra.camel.yaml`: le `data.codigoBarras` e recusa
   alteracao e cancelamento com "nao sao suportados pelo banco Safra").
   `paulohenriquejamsoft/AcbrBoleto` (`SafraWebService.cs`) foi descartado: o payload e
   as rotas (PUT, `/baixa`) nao batem com nenhuma outra fonte.

## O que o adapter faz

- Producao `https://api.safranegocios.com.br/gateway/cobrancas/v1`; homologacao
  (`ambiente="sandbox"`) `https://api-hml.safranegocios.com.br/gateway/cobrancas/v1`.
  Token em `/gateway/v1/oauth2/token` do mesmo host: formulario `client_id`, `username`,
  `password`, `grant_type=password`, sem escopo e sem certificado.
- `Safra-Correlation-ID` (UUID) novo a cada tentativa, inclusive nas repeticoes do retry
  (gerado no autenticador do adapter, que o transporte chama a cada tentativa).
- Registro `POST /boletos` com `{agencia, conta, documento}`; consulta
  `GET /boletos?agencia=&conta=&numero=`; `id_banco` = nosso numero com 9 digitos.
- Capacidades declaradas: nenhuma. `baixar_titulo`, `enviar_instrucao`, `parse_webhook`
  e `obter_pdf` levantam `NaoSuportado` sem chamar o banco.

## Duvidas

1. **Baixa e alteracao.** O ACBr so implementa inclusao e consulta (qualquer outra operacao
   levanta "operacao nao implementada"), a Kobana so lista registro e obtencao, e uma
   integracao de terceiros afirma que alteracao e cancelamento nao sao suportados. Por outro
   lado o topico ACBR-9304 cita exemplos Postman de "Instrucao/Baixa". Sem rota nem payload
   publicos, `BAIXA` e `ALTERACAO` nao sao declaradas. Pedir o swagger completo ao Safra.
2. **Nosso numero.** O cliente gera (`SafraConfig.gerar_nosso_numero`, ate 9 digitos; padrao
   `seu_numero` numerico) e envia em `documento.numero` como numero JSON. O ACBr envia os 9
   digitos sem DV, apesar de ter o calculo do DV (modulo 11) para o CNAB. Confirmar se a API
   espera o DV e se a faixa de numeracao e fornecida pelo banco. O adapter recusa resposta
   com `data.numero` diferente do enviado.
3. **Tipos de `agencia` e `conta`.** O swagger (segundo o ACBR-9304) diz `number`; o ACBr
   envia texto e a integracao de terceiros tambem. Enviamos numero. A agencia: 4 ou 5 digitos
   (com DV)? `SafraConfig.agencia` vai como configurada, sem zeros a esquerda no JSON.
4. **Consulta em homologacao.** O ACBr envia `agencia=` e `conta=` vazios quando o ambiente
   e homologacao; o adapter envia sempre os valores configurados (a conta sem zeros a
   esquerda e com o digito, como no POST). Confirmar na homologacao.
5. **Formato da consulta.** So o ACBr descreve: lista em `content`, com `nossoNumero`,
   `seuNumero`, `situacao`, `codigoBarras`, `linhaDigitavel`, `valorNominal`,
   `valorTotalRecebimento`, `dataHoraSituacao`. O parser do ACBr parece adaptado do Inter
   (mesmas constantes de situacao), entao pode nao refletir a API real. Resposta sem
   `content` levanta `Validacao`.
6. **Tabela de status (`2026.10`).** `EMABERTO` e `VENCIDO` -> registrado, `PAGO` ->
   liquidado, `CANCELADO` e `EXPIRADO` -> baixado. Comparacao sem acento, sem espaco e em
   maiuscula. Qualquer outro valor levanta `Validacao`. O catalogo real de `situacao` e
   desconhecido.
7. **Valor e data pagos.** `valorTotalRecebimento` e `dataHoraSituacao` (10 primeiros
   caracteres), so quando liquidado. O ACBr usa `valorNominal` como valor pago tambem para
   cancelado e expirado; nao seguimos. Se faltar, `valor_pago` fica nulo.
8. **Linha digitavel.** A resposta do registro so traz `data.codigoBarras` (ACBr e
   terceiros). A linha digitavel e derivada pela regra FEBRABAN (modulo 10 por campo) e so
   quando o codigo tem 44 digitos; na consulta, `linhaDigitavel` prevalece se vier.
9. **Multa, juros e desconto: recusados com `Validacao`.** O ACBr manda
   `multa: {dataMulta, taxaMulta, dataJuros, taxaJuros}` e `desconto: {data, valor,
   tipoDesconto}` (1 valor, 2 percentual, 3 e 5 por antecipacao); a integracao de terceiros
   manda `juros: {tipoJuros: "TAXAMENSAL"|"ISENTO", valor, data}` e
   `multa: {tipoMulta: "PERCENTUAL"|"ISENTO", percentual, data}`. Formatos incompativeis:
   decidir pelo swagger.
10. **Campos que o ACBr manda e o adapter omite:** `quantidadeDiasProtesto` (o modelo nao
    tem protesto), `campoLivre`, `fidc`, `danfe` (vazios no ACBr). `diasDevolucao` so vai
    quando o titulo tem `dias_baixa_automatica` ou a config tem `dias_devolucao` (o ACBr fixa
    90). Se a homologacao exigir algum deles, incluir.
11. **`codigoMoeda` e `carteira`.** Enviamos `documento.codigoMoeda = 9` (ACBr). A integracao
    de terceiros manda `codigoMoeda: "09"` na raiz e `documento.carteira: "01"`. Sem
    `carteira` no adapter.
12. **Especie.** `SafraConfig.especie` obrigatoria, 2 digitos (ACBr: 01 DM, 02 NP, 03 NS, 05
    recibo, 09 DS). Lista completa desconhecida.
13. **Limites e truncamento.** Como o ACBr: `numeroCliente` 10 (o `seu_numero` e cortado; o
    `TituloRegistrado` guarda o completo), nome 40, email 50, logradouro 40 (logradouro,
    numero e complemento juntos por virgula), bairro 10, cidade 15, mensagem 72. Quantidade
    maxima de mensagens desconhecida; `posicao` configuravel (`"1"` recibo, padrao do ACBr;
    `"2"` ficha).
14. **CNPJ alfanumerico.** `numeroDocumento` vai como esta no modelo; o ACBr filtra so
    digitos. Aceitacao pelo Safra nao confirmada.
15. **Erros.** 400/415 com `violacoes[]` (`codigo`, `parametro`, `razao`, `propriedade`,
    `valor`), 404 com `codigo`/`mensagem`, 422 com `code`/`message` (ACBr). Todos viram
    `Validacao`; 503 generico vira `Indisponivel` com retry, mas o forum relata 503 tambem para
    conta mal formatada (repetir nao resolve). Codigo de duplicidade desconhecido: o adapter
    nao converte nada em `Duplicado`.
16. **Idempotencia.** Como o nosso numero vem do cliente, repetir o POST com o mesmo numero
    deve ser recusado pelo banco (nao confirmado). Ha cache e trava por
    `chave_idempotencia` no processo; o `Orquestrador` cobre entre processos.
17. **Boleto hibrido (Pix), PDF e webhook.** Nenhuma fonte cita QR Pix no boleto da API de
    cobranca, endpoint de PDF ou webhook. Nao declarados. O Pix do Safra e outro produto
    (certificado + client_secret, segundo a Kobana), fora deste adapter.
18. **Expiracao do token.** Usa `expires_in`; o ACBr manda `refresh_token=` vazio no pedido,
    que omitimos.
