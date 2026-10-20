# Unicred (136) API de Cobranca v2: duvidas em aberto

`UnicredAdapter.validado_sandbox = False`. Nada foi executado contra a Unicred.

## Fontes

- **Portal oficial "Unicred Developers"** (`developer.unicred.com.br`, plataforma Sensedia):
  exige login. As paginas "Obtendo um token de acesso", "API Guide", "Emissao de Titulos
  de Cobranca", "Consulta de Titulos de Cobranca", "PDF - Boleto", "API de Baixa para
  Titulos Emitidos" e "Retorno de Titulos de Cobranca por Webhook" foram lidas em copias
  HTML salvas do portal, publicadas no repositorio `uru-assessoria/keeping` (pasta
  `unicred/`). Os exemplos de erro da baixa tem timestamps de novembro de 2025.
- **Clientes de terceiros, para conferir:** `eduardokum/laravel-boleto`
  (`src/Api/Banco/Unicred.php`, MIT), `guilhermemcr1/unicred-php-sdk` (diz rodar em
  producao; traz respostas anonimizadas da homologacao) e `gildonei/unicred` (2019).
  Os tres concordam nas URLs, nos headers e no `id_titulo` em texto puro.
- **ACBr:** `ACBrBancoUnicredES/RS/SC.pas` sao so CNAB (contexto, nada usado). O
  `ACBrBoletoW_Unicred.pas` citado no forum do ACBr nao esta na referencia local.
- **Verificacao objetiva:** os vetores de criptografia do webhook publicados no portal
  (texto "Unicred via Webhook" e os tres cenarios de homologacao) decifram com
  `decifrar_webhook`; dois deles estao nos testes.

Antes de usar em producao: pedir a Unicred o acesso ao portal (APP com `apiKey`), a
planilha de homologacao (`id_beneficiario`, `variacao_carteira`, codigo da cooperativa) e
rodar a suite contra `https://api.e-unicred.com.br/homolog`.

## Contrato adotado

| Operacao | Chamada | Resposta |
| --- | --- | --- |
| Token | `POST /oauth2/v2/grant-token`, JSON `nomeUsuario`/`senha`, header `apiKey` | `accessToken`, `tokenExpirationTime` (s) |
| Emitir | `POST /cobranca/v2/beneficiarios/{id}/titulos` | 200, `id_titulo` em texto puro |
| Consultar | `GET .../titulos/{id_titulo}/status` | 200 JSON ou 202 sem corpo |
| PDF | `GET .../titulos/{id_titulo}` | 200 binario ou 202 |
| Baixar | `POST .../titulos/{id_titulo}/baixa`, `{"tipo": "SOLICITACAO_CEDENTE"}` | 201 `codigoInstrucao` |

Toda chamada leva `Authorization: Bearer`, `cooperativa` e `apiKey`. Sem mTLS.

## Duvidas

1. **Resposta da emissao.** O portal mostra dois exemplos: texto puro com o `id_titulo` e
   um JSON com `id_titulo` (com aspas faltando, claramente editado a mao). Os clientes de
   terceiros leem texto puro. O adapter aceita os dois.
2. **Idempotencia da emissao.** Sem `nossoNumero` (o portal recomenda deixar o banco
   gerar), um POST repetido apos timeout pode criar dois titulos. Nao se sabe se a Unicred
   recusa `seuNumero` repetido. O transporte so repete em 429/5xx/erro de rede, entao o
   risco existe nesses casos. Perguntar: ha chave de idempotencia ou recusa por
   `seuNumero`? A regra de geracao do `nossoNumero` (link "Regra de geracao" do portal)
   nao foi lida.
3. **Status da consulta.** Dominio documentado: `ABERTO`, `BAIXADO`, `LIQUIDADO`,
   `EM_PROCESSAMENTO`. Titulo recusado na CIP nao aparece no dominio da consulta (so no
   webhook, situacao `101`): como a consulta o mostra? Hoje viraria `Validacao`.
   Resposta 200 com corpo vazio (relatada em um cliente de terceiros) e tratada como
   processamento, igual ao 202.
4. **Grupo `pagamentos`.** O exemplo do portal esta malformado (`"pagamentos": "data":
   ..., "valor": ...`). O adapter aceita objeto ou lista; com varios pagamentos soma os
   valores e usa a data mais recente. Em `ABERTO` o grupo e ignorado (titulo de multiplos
   pagamentos, especie 31).
5. **Pix (hibrido).** `qrCodePix` so vem se a cooperativa habilitar no cadastro do
   beneficiario. Por depender de configuracao fora da API, `HIBRIDO` nao e declarada; o
   QR vai em `pix_copia_cola` quando vier.
6. **Baixa assincrona.** A API aceita a instrucao (201) e processa depois; o adapter
   devolve `BAIXADO` na hora (como os demais) e declara `INSTRUCAO_ASSINCRONA`. A consulta
   pode mostrar `ABERTO` por um tempo. `justificativa` e `valorRecebido` nao sao enviados.
7. **Alteracao de vencimento e outras instrucoes.** So o SDK de terceiros usa
   `POST .../alteracao-vencimento` (`novaDataVencimento`); nao ha pagina oficial lida.
   `ALTERACAO` nao e declarada e prorrogacao, abatimento, protesto e negativacao levantam
   `NaoSuportado`. Pedir a documentacao das instrucoes.
8. **Encargos.** Juros mensal = codigo `3` (taxa mensal), multa percentual = codigo `2`,
   `dataInicio` = vencimento + 1 dia (escolha do adapter). Sem encargo vai `5`/`3`
   (isento) para nao herdar o padrao do cadastro do cooperado. Desconto: a API so tem
   valor fixo (`indicador 1`); o percentual do modelo vira valor (centavos, meio para
   cima) ate vencimento menos `desconto_dias_antes`.
9. **Sem campo na emissao:** abatimento, baixa automatica por dias e beneficiario final
   (sacador avalista). Os dois primeiros sao recusados com `Validacao`; o beneficiario
   final e ignorado (o portal cita "beneficiario original" so numa mensagem de erro).
   Protesto e negativacao automaticos existem na API mas nao no modelo canonico.
10. **Pagador.** `nomeFantasia` e obrigatorio para PJ e o modelo nao tem: vai a razao
    social. Textos sao truncados aos limites do dicionario (nome 40, logradouro 60,
    bairro 40, cidade 50, complemento 30, mensagens 6 x 80); `numero` acima de 5
    caracteres e recusado (truncar mudaria o endereco). CNPJ alfanumerico: aceito pela
    API? Desconhecido.
11. **Token.** Validade de 2700 s no exemplo; o adapter renova com margem de 60 s e pede
    um token novo em vez de usar o `refreshToken` (endpoint `/oauth2/v2/refresh-token`
    so aparece no SDK de terceiros).
12. **Webhook.** Corpo cifrado (AES-256-CBC, PKCS5, base64). IV = 16 primeiros caracteres
    da chave de autenticacao; chave AES = 32 primeiros caracteres da chave de criptografia;
    as duas sao UUIDs que o cooperado cadastra no CobExpress, uma inscricao por conta
    corrente. O adapter confere o `x-api-key` quando recebe os cabecalhos. O evento:
    `codigoMovimento` 01, 06, 07 e 09 (o retorno "Parcial" do portal) = `LIQUIDADO`;
    `03` com instrucao `01` (remessa rejeitada) = `REJEITADO`; o resto sai de
    `titulo.codigoSituacao` (tabela versionada `TABELA_SITUACAO_WEBHOOK`). Situacao `07`
    (titulo deletado) fica fora ate saber se equivale a baixa. "Liquidacao em condicional"
    (`07`) e tratada como liquidacao: o webhook nunca confirma, reconsulte.
13. **Resposta ao webhook.** Quem recebe responde 202 com um UUID v4 em texto puro, sem
    aspas, em ate 5 s, senao a inscricao fica off-sync. Qual UUID (um novo ou o
    `uuidRequisicaoWebhook`) o portal nao diz; o SDK de terceiros gera um novo. A
    biblioteca so interpreta; a resposta e do servico que recebe.
14. **Homologacao do webhook.** Na homologacao a Unicred so dispara o evento de emissao;
    os outros eventos se testam com os cURL do portal.
15. **PII.** O `mascarar` da biblioteca nao conhece `nomeRazaoSocial` nem
    `numeroDocumento`: o adapter tira o bloco `pagador` inteiro da `resposta_bruta`.
    Vale ensinar essas chaves ao `brbank.mascara` (mudanca no nucleo, fora deste adapter).
16. **Limites de taxa.** O portal so cita 429 e 413 por perfil do token, sem numeros.
    `requisicoes_por_segundo` fica `None` por padrao.
