# Caixa (104) webservice SIGCB: duvidas em aberto

`CaixaAdapter.validado_sandbox = False`. Nada foi executado contra a Caixa. A Caixa nao tem
sandbox publico conhecido: a validacao depende de um beneficiario com o servico de webservice
liberado pela agencia (sem isso a Caixa responde "(89) OPERACAO NAO PERMITIDA - CEDENTE INFORMADO
NAO POSSUI AUTORIZACAO PARA UTILIZAR SERVICO WEBSERVICE").

## Fontes

Em ordem de peso:

1. **Oficial (Caixa), lido em 2026-10-20:** WSDL e XSD publicados pelo barramento.
   - `https://barramento.caixa.gov.br/sibar/ManutencaoCobrancaBancaria/Boleto/Externo?wsdl`
     (operacoes INCLUI_BOLETO, ALTERA_BOLETO, BAIXA_BOLETO; soapAction `IncluiBoleto`,
     `AlteraBoleto`, `BaixaBoleto`), XSD `?xsd=xsd0` (tipos do titulo) e `?xsd=xsd4`
     (`MensagensBarramento.xsd`: HEADER, COD_RETORNO, CONTROLE_NEGOCIAL).
   - `https://barramento.caixa.gov.br/sibar/ConsultaCobrancaBancaria/Boleto?wsdl` (CONSULTA_BOLETO,
     soapAction `http://caixa.gov.br/sibar/consulta_cobranca_bancaria/boleto/NewOperation`) e XSD
     `?xsd=xsd1` (TITULO da consulta com SITUACAO, VALOR_PAGO, DATA_HORA_PAGAMENTO).
   - O manual de leiaute do webservice (PDF) nao foi encontrado em site da Caixa; circula como
     anexo no forum do ACBr (`Manual_Leiaute_Webservice.pdf`), que pede login. Nao lido.
2. **ACBr (LGPL, lido como documentacao, nenhum codigo portado):** `ACBrBoletoW_Caixa.pas`
   (envelope, hash, versoes do HEADER, ordem dos campos), `ACBrBoletoRet_Caixa.pas` (leitura do
   retorno, textos de SITUACAO), `ACBrBoletoWS.SOAP.pas` (hash = SHA-256 em base64),
   `ACBrBoletoConversao.pas` (enumeracoes) e `Particularidades.html` (secao Caixa SIGCB).
3. **Terceiros:** topico 46068 do forum ACBr ("Boletos via webserver Caixa": hash validado no
   SoapUI, respostas de sucesso e de erro 89, nosso numero com prefixo 14); README do
   `jovemnf/cef-webservice` (exemplos de resposta de INCLUI_BOLETO e CONSULTA_BOLETO, de 2018);
   README do `thiagoedson/wscaixa` (confirma SHA-256 e `COD_RETORNO == '0'`).

Antes de usar em producao: obter o manual oficial com a Caixa e rodar registrar, consultar,
alterar e baixar com um beneficiario habilitado, com valor baixo.

## Duvidas

1. **Hash AUTENTICACAO.** SHA-256 em base64 de: codigo do beneficiario (7, zeros a esquerda) +
   nosso numero (17) + vencimento DDMMAAAA + valor em centavos (15) + CPF/CNPJ do beneficiario (14,
   zeros a esquerda). Baixa e consulta zeram vencimento e valor. Fonte: ACBr e forum (duas
   implementacoes concordam; uma terceira, sem o prefixo 14, dava erro de hash). Nao ha fonte
   oficial lida. CNPJ alfanumerico entra como texto de 14 posicoes; se a Caixa aceita, e
   desconhecido (o XSD ja declara CNPJ como texto de 14).
2. **Hash na alteracao.** O ACBr usa o vencimento e o valor do titulo que esta alterando. O adapter
   consulta antes e usa vencimento novo (prorrogacao) ou atual (abatimento) e o VALOR atual. Se a
   Caixa espera outro valor (por exemplo, so os campos alterados), a alteracao volta com erro de
   autenticacao. Confirmar.
3. **Nosso numero.** 17 digitos: "14" (1 = registrada, 4 = emissao pelo beneficiario) + 15
   digitos. Gerado do lado do beneficiario (`gerar_nosso_numero`; padrao: `seu_numero` numerico).
   O XSD permite omitir NOSSO_NUMERO na inclusao, mas o hash precisa dele, e o exemplo publico de
   resposta traz `NOSSO_NUMERO` 0: por isso o `id_banco` e sempre o numero enviado. O forum cita
   que em producao o nosso numero comecaria com zeros; nao confirmado.
4. **Versoes do HEADER.** 3.0 (convencional), 3.2 (hibrido) e 5.4 (consulta), do ACBr; exemplos
   antigos usam 1.2 e 2.0. Configuraveis (`versao_manutencao`, `versao_consulta`).
5. **SOAPAction.** O adapter manda a do WSDL oficial; o ACBr manda o nome da operacao
   (`INCLUI_BOLETO`). Se o barramento rejeitar, trocar em `SOAP_ACTION`.
6. **USUARIO_SERVICO.** `SGCBS02P` (ACBr e exemplos); o ACBr troca pelo ClientID quando informado.
   Configuravel.
7. **URL de homologacao.** Nao ha URL publica conhecida (o WSDL cita `des.barramento.caixa`, rede
   interna). So producao; `url_base` e configuravel.
8. **Codigos de retorno.** Sucesso: `COD_RETORNO` 00 no SERVICO_SAIDA e 0 em CONTROLE_NEGOCIAL,
   mensagem "(0) OPERACAO EFETUADA". Qualquer outro codigo vira `Validacao` (nao repete). Nao se
   conhece codigo de barramento que indique indisponibilidade temporaria, nem o codigo de "titulo
   nao encontrado" ou de nosso numero duplicado: por isso nao ha `Duplicado` mapeado e a suite de
   contrato nao exige HTTP 404 para a Caixa (`status_inexistente=None`). SOAP Fault com HTTP 500
   segue a regra do transporte (`Indisponivel`, com retry); Fault com HTTP 200 vira `Validacao`.
9. **Situacao do titulo.** Campo SITUACAO (texto livre, 50) no TITULO da consulta; no layout antigo
   so aparece em "SITUACAO DO TITULO = ..." no RETORNO, lido como alternativa. Tabela 2026.10, de
   textos do ACBr (que compara por substring; aqui a comparacao e exata, sem acento e com espacos
   normalizados): EM ABERTO e ENVIADO AO CARTORIO = REGISTRADO; LIQUIDADO, LIQUIDADO NO CARTORIO e
   TITULO JA PAGO NO DIA = LIQUIDADO; BAIXA POR DEVOLUCAO e BAIXA POR PROTESTO = BAIXADO.
   `BAIXA POR ESTORNO` fica fora (significado nao confirmado) e vira `Validacao`, como qualquer
   texto desconhecido. O texto que a Caixa mostra apos uma baixa pedida pelo beneficiario e
   desconhecido (o servidor falso usa BAIXA POR DEVOLUCAO).
10. **Pagamento.** `VALOR_PAGO` (decimal) e `DATA_HORA_PAGAMENTO` (texto de 19 posicoes, formato
    nao documentado). O adapter aceita `AAAA-MM-DD HH:MM:SS`, `AAAA-MM-DDTHH:MM:SS` e
    `DD/MM/AAAA HH:MM:SS`; fora disso a data volta `None`.
11. **FLAG_REGISTRO** (S/N) na consulta nao e interpretado; fica na resposta bruta.
12. **Encargos.** Juros mensal = `TAXA_MENSAL` com PERCENTUAL; sem juros = `ISENTO` com VALOR 0.00
    (o XSD exige VALOR ou PERCENTUAL). Multa = PERCENTUAL. Desconto = `PERCENTUAL_ATE_DATA` com data
    = vencimento - dias (o ACBr nao manda TIPO; o XSD atual aceita). Datas de multa e juros =
    vencimento + 1 dia, escolha do adapter. Percentuais com 4 casas (como o ACBr); mais casas sao
    recusadas.
13. **POS_VENCIMENTO.** Obrigatorio. O adapter manda `DEVOLVER` com `dias_baixa_automatica` do
    titulo ou `dias_devolucao_padrao` (0, valor que o ACBr manda). O efeito de 0 dias nao esta
    documentado. Protesto e negativacao no registro nao existem no modelo canonico.
14. **Instrucoes.** PRORROGACAO e ABATIMENTO por ALTERA_BOLETO com so os campos alterados (o ACBr
    reenvia o titulo inteiro); BAIXA por BAIXA_BOLETO. PROTESTO (ALTERA com ACAO PROTESTAR exige
    numero de dias, que a `Instrucao` nao traz) e NEGATIVACAO levantam `NaoSuportado`.
15. **Truncamento.** Nome e razao social (40), logradouro + numero (40), bairro e cidade (15), como
    no XSD; a Caixa guarda o texto truncado (exemplo publico "CAMPOS DOS GOYT"). Complemento nao e
    enviado. NUMERO_DOCUMENTO (11) recebe `seu_numero`, que e recusado acima disso;
    IDENTIFICACAO_EMPRESA (25) recebe o mesmo valor. Ate 2 mensagens de 40 na ficha de compensacao;
    o recibo do pagador nao e preenchido.
16. **PAGAMENTO** (pagamento parcial, valor minimo e maximo) nao e enviado: vale o padrao do
    beneficiario na Caixa. O exemplo publico mostra `NAO_ACEITA_VALOR_DIVERGENTE` na consulta.
17. **Hibrido.** `HIBRIDO` e declarada fixa, mas depende de `hibrido=True` (TIPO HIBRIDO no titulo)
    e do Pix habilitado no beneficiario. O QR vem em `QRCODE` (inclusao e consulta).
18. **PDF.** A inclusao devolve `URL` (boletoonline.caixa.gov.br) para impressao; se ela entrega
    PDF direto e sem autenticacao e desconhecido. `PDF` nao declarada; a URL fica na resposta bruta.
19. **Webhook.** O SIGCB nao tem. Conciliacao por consulta (ou arquivo de retorno CNAB, fora do
    escopo).
20. **Seguranca do XML.** Resposta com DOCTYPE ou ENTITY e recusada antes do parse; o parse e o
    `xml.etree` da stdlib (expat nao busca entidade externa). O envelope de saida e montado com
    texto escapado, sem cliente SOAP dinamico. O hash devolvido no HEADER nao entra na resposta
    bruta.
21. **Transporte.** O `Transporte` da biblioteca so aceita corpo JSON; o envelope SOAP passa por um
    `Envelope` interno do adapter (`_CorpoXml`) que devolve o XML puro. Se o transporte ganhar corpo
    binario, trocar.
