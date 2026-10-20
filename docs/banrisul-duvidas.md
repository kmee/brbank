# Banrisul (041) API Cobranca v1: duvidas em aberto

`BanrisulAdapter.validado_sandbox = False`. Nada foi executado contra o Banrisul.

## Fontes

- **Oficial (fonte principal):** documentacao da API Cobranca no portal de
  desenvolvedores do Banrisul,
  <https://developers.banrisul.com.br/pages/docs/clientes-banrisul/api-cobranca-v1.html>,
  versao 1.6.1 de 26/08/2026. A pagina e publica e embute a especificacao OpenAPI
  (Redocly); lida em 20/10/2026. Dela vieram URLs, headers (`bergs-beneficiario`,
  `bergs-ambiente`), escopo `boletos`, campos e padroes do registro, tabela de
  `situacao_banrisul`/`situacao_pagamento`/`situacao_cip`, `retorno` 01/02/04, formato de
  erro (`ocorrencias[{codigo, mensagem, complemento}]`), algoritmo do DV do nosso numero,
  emissao do PDF, webhook e os exemplos do sandbox.
- **Referencia LIDA (LGPL, nenhum codigo portado, so documentacao):** ACBr,
  `ACBrBoletoW_Banrisul.pas`, `ACBrBoletoRet_Banrisul.pas`, `ACBrBancoBanrisul.pas` e
  `Particularidades`. Usado para a URL de token de producao e para conferir a leitura de
  `operacoes` na consulta.
- **Terceiros:** wiki CIGAM ("Open Banking para Banco Banrisul": codigo do beneficiario de
  13 digitos, credenciais pelo portal) e forum do ACBr (bug de paginacao na listagem).

Antes de usar em producao: rodar a suite contra o sandbox (beneficiario de teste
`0010000001088`, credenciais pelo e-mail `gestao_sistemas_cobranca_operacional@banrisul.com.br`)
e cumprir a homologacao do banco (primeiro registro em producao com `ambiente` `T`, depois
cinco registros com retorno e PDF enviados por e-mail ao banco).

## Duvidas

1. **URL de token de producao.** A especificacao oficial so traz a do sandbox
   (`https://apidev.banrisul.com.br/auth/oauth/v2/token`). A de producao
   (`https://api.banrisul.com.br/auth/oauth/v2/token`) vem do ACBr. Confirmar.
2. **Credenciais no corpo ou em Basic.** A doc diz que o pedido de token leva `client_id`,
   `client_secret` e `grant_type` no corpo; a collection Postman do banco usa Basic. O
   adapter manda no corpo (`OAuth2ClientCredentials`) com `scope=boletos`.
3. **mTLS.** A doc nao exige certificado de cliente, mas a tabela de erros tem `OCWS0003
   Certificado invalido`. O certificado e opcional em `BanrisulConfig`. Confirmar.
4. **Status `B` x `L`.** A doc define `B = Baixado por pagamento` e `L = Liquidado`, e a
   listagem trata `situacao_titulo=B` como "baixados/liquidados". O adapter mapeia os dois
   para `LIQUIDADO`. O exemplo oficial de `B` so traz `operacoes.data_baixa`, sem valor:
   nesse caso `valor_pago` e `data_pagamento` vem `None`. Confirmar com o banco se `B` sempre
   significa dinheiro recebido.
5. **Titulo ativo com `situacao_pagamento`.** Mapeados: 1, 3, 5, 6, 7 e 8 -> `REGISTRADO`;
   2 (pago no dia) -> `LIQUIDADO`; 4 (baixado no dia) -> `BAIXADO`. Pago no dia ainda nao tem
   `operacoes` ("a partir do dia seguinte ao pagamento"), entao o valor vem `None` ate o dia
   seguinte. 3 (credito retido) fica como `REGISTRADO` porque o credito pode nao se
   confirmar. 6 (em cartorio) e 8 (negativado) seguem cobraveis, mas o canonico nao tem
   estado para isso. Codigo fora da lista vira `Validacao`.
6. **Status sem mapeamento.** `R` (reembolsado), `T` (transferido para CL) e `P`
   (protestado) existem na doc oficial e ficam fora da tabela: viram `Validacao` ate haver
   decisao sobre o equivalente canonico.
7. **Valor pago.** `operacoes` tem `valor_pagamento` ("incluindo descontos ou acrescimos"),
   `valor_cobrado` ("cobrado do pagador") e `valor_creditado_debitado`. No exemplo oficial de
   liquidado com juros de 10,00 o `valor_pagamento` e 8000.00 e o `valor_cobrado` 8010.00. O
   adapter usa `valor_cobrado` e, se faltar, `valor_pagamento`. Confirmar.
8. **Webhook sem situacao.** O callback oficial so traz `nosso_numero`, `codigo_barras`,
   `linha_digitavel` e `beneficiario.codigo`; nao tem assinatura (a doc recomenda filtrar o
   IP de origem 201.55.240.0/20 e sempre reconsultar). `WEBHOOK` nao e declarada,
   `parse_webhook` levanta `NaoSuportado` e `referencias_webhook` devolve os nossos numeros
   (recusa aviso de outro beneficiario). O cadastro da URL em producao e por e-mail ao banco;
   no sandbox ha `POST /boletos/testar_webhook` (nao implementado). Lista ou objeto unico no
   corpo: a doc so mostra objeto unico; o adapter aceita os dois.
9. **Homologacao (`T`).** Com `homologacao=True` o registro manda `ambiente: "T"` e baixa,
   alteracao e emissao mandam `bergs-ambiente: T`. O banco so valida e devolve `retorno`
   "04" (no registro, nosso numero `0000000000`). O adapter levanta `Validacao` com a resposta
   (mascarada) para nao fingir registro. Para o pacote de homologacao exigido pelo banco
   (requisicao, retorno e PDF em claro), capturar fora da biblioteca.
10. **Idempotencia do registro.** Sem `gerar_nosso_numero` o banco gera o nosso numero, e o
    retry do transporte em 5xx/503 pode registrar o titulo duas vezes. Com o gerador, o
    adapter manda o nosso numero com os DVs e o erro FEBRABAN `09` (nosso numero duplicado)
    vira `Duplicado(id_banco=...)`, que o orquestrador transforma em consulta. Nao se sabe o
    HTTP exato do `09` (400 ou 422); o adapter olha o codigo em qualquer recusa. O nosso
    numero so pode ser reusado no dia seguinte a baixa do titulo antigo.
11. **Retorno `01` no registro.** "Registrado so na base Banrisul, ainda nao centralizado na
    CIP": o adapter trata como `REGISTRADO`. O titulo pode nao ser pagavel em outros bancos ate
    a centralizacao; `situacao_cip` vem na consulta e nao e interpretada.
12. **Boleto hibrido.** `HIBRIDO` e declarada fixa, mas depende de `hibrido=True` e de o
    convenio ter Pix. O QR so existe com `hibrido.situacao = "A"`; `P` (pendente) e `N` (nao
    criado) devolvem `pix_copia_cola=None` e o integrador reconsulta. Hibrido exige
    `pag_parcial.autoriza = 1`, que o adapter sempre manda (pagamento parcial nao e suportado).
13. **Abatimento soma.** No `PATCH ?tipo_alteracao=04` o valor e SOMADO a um abatimento ja
    concedido (doc oficial). `Instrucao.valor` vai como incremento, nao como valor final.
14. **Prorrogacao.** `PATCH ?tipo_alteracao=06`; juros e multa sem data acompanham o novo
    vencimento (o adapter nunca manda data neles). Com data fixa anterior ao novo vencimento
    o banco rejeita.
15. **Instrucoes fora da API.** Protesto e negativacao so existem no registro (`protesto`
    codigos 1, 3, 7, 8), nao como alteracao: `PROTESTO` e `NEGATIVACAO` levantam
    `NaoSuportado`. O registro nao manda `protesto` (o modelo canonico nao tem o campo): vale
    a instrucao permanente do convenio.
16. **Encargos.** Juros mensal -> codigo 2 com `taxa` (2 casas); multa percentual -> codigo 2
    com `taxa` de UMA casa decimal (padrao oficial `^\d{1,4}\.\d{1}$`), percentual com mais
    casas e recusado em vez de arredondado; desconto -> codigo 2 (percentual ate a data),
    data = vencimento menos `desconto_dias_antes`. Sem juros manda `codigo 3` (isento), que e
    obrigatorio. Baixa automatica -> `baixa {codigo 1, prazo}` (ate 99 dias).
17. **Cortes de tamanho.** Nome e endereco do pagador e do sacador cortados em 40, cidade em
    15 (limites oficiais). O endereco e "logradouro numero complemento"; nao ha campo de
    bairro. Caracteres fora do conjunto aceito pela doc (ex.: `o`/`a` ordinais) seguem como
    vieram e o banco recusa com 400. `seu_numero` acima de 13 caracteres ou com caractere
    fora do padrao e recusado antes do envio. `id_titulo_empresa` nao e enviado.
18. **Sacador.** `Titulo.beneficiario` (beneficiario final) vira `sacador`, que exige
    endereco e CEP; sem endereco o titulo e recusado.
19. **Especie.** So 02, 04, 31, 32 e 99 sao aceitas pelo padrao oficial; vem da config sem
    padrao. Valor de IOF (carteira 8051), rateio e notas fiscais nao sao enviados.
20. **Erros.** O transporte traduz HTTP (404 -> `Validacao`, 5xx/503 -> `Indisponivel`). O
    corpo `ocorrencias` so e lido para o `09`. 401 renova o token uma vez (a doc diz que token
    expirado responde 401).
21. **Cota.** A doc nao publica limite de requisicoes: `requisicoes_por_segundo=None`.
22. **Listagem.** `GET /boletos` (paginada, ate 8000 por paginacao padrao, ou por
    `proximo_nosso_numero`) nao foi implementada: serviria para conciliacao em lote.
