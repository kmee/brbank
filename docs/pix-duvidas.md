# Pix BACEN generico: duvidas em aberto e quirks

Fonte do contrato: OpenAPI oficial da API Pix do BACEN (/cob, /cobv, /webhook,
/pix/{e2eid}/devolucao), conhecido do padrao publico. O comportamento por PSP
(host, URL do token, escopos, headers, quirks) foi lido nos fontes do ACBr
(`ACBrPIXCD`, licenca LGPL) como documentacao, sem portar codigo. Nenhum perfil
foi testado contra sandbox real: todos nascem com `validado_sandbox = False`.
O que a leitura nao esclarece ficou de fora ou recusado, nao inventado.

## Perfis

| Perfil | Auth | Escopo | mTLS | Quirks aplicados |
|---|---|---|---|---|
| inter | form (id+segredo) | sim | sim | nenhum |
| bb (API v2) | Basic | sim | prod | header `x-bb-portal-devx-cnpj-parceiro`, query `gw-dev-app-key`, alias `textoImagemQRcode` |
| sicoob | so `client_id` (sandbox: token fixo do portal) | sim | prod | header `client_id`, alias `brcode` -> `pixCopiaECola` |
| sicredi | Basic | sim | sim | nenhum |
| itau | form (id+segredo) | nao | prod | header `x-correlationID` (UUID por chamada) |
| bradesco | Basic, HTTP/1.1 | nao | sim | nenhum |
| santander | form, `?grant_type` na URL | nao | prod | sandbox cria por POST, baixa (PATCH) em `/api/v2` na producao |
| efi (GerenciaNet) | Basic + corpo JSON, HTTP/1.1 | nao | sim | header opcional `x-partner-token` |
| bnb | sem perfil | | | ver abaixo |

## Duvidas

1. **BNB sem perfil.** O ACBr nao tem PSP do Banco do Nordeste. `obter_perfil("bnb")`
   levanta `Validacao`. Falta documentacao do portal do BNB (host, token, escopos).
2. **Escopos.** O ACBr pede por padrao cob/cobv/pix/webhook (read e write). Nao ha
   evidencia de qual conjunto cada banco aceita; se o banco recusar um escopo, o
   token falha. Ajustar `escopos` no perfil quando o sandbox responder. Itau, Bradesco,
   Santander e Efi nao mandam escopo (como no ACBr).
3. **Idempotencia do `txid`.** O `txid` sai do SHA-256 de `conta|chave_idempotencia`
   (32 hex, dentro de 26-35). A OpenAPI nao diz qual HTTP um PSP devolve para PUT em
   txid existente; o adapter so converte em consulta o 409 (`Duplicado`). Se algum
   banco devolve 400 ou 422, o titulo falha como `Validacao` e o `Orquestrador` (que
   guarda a chave) evita a repeticao. Confirmar por banco.
4. **PUT versus POST.** O adapter sempre cria por `PUT /cob(v)/{txid}`. O ACBr converte
   POST em PUT para BB v1 e Santander, e o sandbox do Santander so aceita POST (perfil
   `metodo_criar_sandbox`). `POST /cob` com txid gerado pelo banco nao e usado.
5. **BB API v1.** So a v2 tem perfil. A v1 (`api.bb.com.br/pix/v1`) tem `/cobqrcode`
   no sandbox e delimitador `/` antes da query; nao implementado. Confirmar qual
   versao os clientes KMEE usam.
6. **Santander pre-producao e sandbox.** Existe host de pre-producao
   (`trust-pix-h.santander.com.br`), sem perfil. O sandbox devolve JSON fora do padrao
   (lista `cobs[0]`/`pix[0]`); o ACBr corrige, o adapter nao.
7. **Itau sandbox.** Exige o header `x-sandbox-token` com o token obtido; o autenticador
   atual nao expoe o token ao adapter. Nao implementado. O fluxo de certificado
   (CSR/renovacao em `/seguranca/v1/certificado`) tambem fica fora.
8. **Sicoob em producao.** O ACBr manda so `client_id`, sem segredo, e a identidade vem
   do mTLS. Confirmar que nao ha `client_secret`.
9. **HTTP/1.1.** O ACBr forca 1.1 so na chamada de token de Bradesco e Efi. Aqui o
   cliente inteiro desses perfis usa 1.1 (`Transporte(http2=False)`), mais simples e
   mais conservador. Confirmar se so o token precisa.
10. **Webhook sem assinatura.** O BACEN protege o callback por mTLS do PSP, nao por HMAC.
    `parse_webhook` nao autentica nada: o receptor precisa validar o certificado do PSP
    (fora da lib) e reconsultar antes de liquidar. A Efi documenta um header para pular
    a checagem de mTLS no cadastro do webhook (`x-skip-mtls-checking`), do conhecimento
    geral e nao da referencia: nao implementado.
11. **Webhook sem `txid`.** Pix de QR estatico chega sem `txid`; o adapter ignora (sem
    titulo para baixar). Se o produto quiser conciliar Pix avulso, falta um evento
    canonico proprio. Devolucoes dentro do corpo (`devolucoes`) tambem sao ignoradas.
12. **Valor pago parcial e devolucao.** `valor_pago` e a soma de `pix[].valor` da
    consulta, sem descontar devolucoes. Cobranca com `modalidadeAlteracao` nao e
    emitida (valor fixo), entao pagamento parcial nao e esperado.
13. **CNPJ alfanumerico.** O adapter envia `cnpj` com o valor normalizado (A-Z e digitos).
    Nao se sabe quais PSPs ja aceitam letras no campo `devedor.cnpj` (o schema antigo
    exigia 14 digitos).
14. **Endereco do devedor.** Em `cobv` o adapter manda logradouro (com numero), cidade,
    UF e CEP quando o `Pessoa` tem endereco. Alguns PSPs podem exigir todos os campos;
    nao e validado localmente.
15. **Mapeamento de encargos para `cobv`.** Multa = modalidade 2 (percentual); juros =
    modalidade 3 (percentual ao mes, dias corridos); abatimento = modalidade 1 (valor
    fixo); desconto = modalidade 2 (percentual ate a data, `vencimento - dias_antes`).
    O `valorPerc` do abatimento e um valor em reais, nao percentual, apesar do nome do
    campo no BACEN. Juros e multa em outras modalidades (dia util, ao dia) ficam fora.
16. **`REMOVIDA_PELO_PSP`.** Mapeada para `BAIXADO`. Pode ser expiracao; se o produto
    quiser distinguir, falta um status canonico.
17. **`cob` imediata.** Recusa multa, juros, desconto e abatimento; a validade e
    `expiracao_s` (padrao 86400), nao o vencimento do `Titulo`.
18. **Fora do escopo desta entrega:** consulta de Pix por `e2eid` e por periodo, listagem
    de cobrancas, lote `cobv`, `payloadlocation`, recorrencia (`rec`, `cobr`), revisao
    de valor (PATCH com `valor`) e `enviar_instrucao`.
