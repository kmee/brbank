# PinBank: duvidas em aberto do contrato

Fonte unica usada: modulo Odoo `l10n_br_payment_boleto_pinbank` (kmee-odoo-addons,
branch 16.0). Nao ha documentacao oficial da API no repositorio. O que o modulo nao
esclarece ficou fora do adapter ou recusado com `Validacao`, em vez de inventado.

1. **Unidade de Juros e Multa.** O modulo envia `{"Valor": int(boleto_interest * 10000)}`
   e `int(boleto_penalty * 10000)`; o vetor de teste usa `Valor: 100`. Nao se sabe se e
   centesimos de percentual, fracao x 10000, nem se juros e ao dia ou ao mes. O adapter
   recusa titulo com multa, juros, desconto, abatimento ou baixa automatica.
2. **Desconto, abatimento e baixa automatica.** O modulo nao envia. Mesmo tratamento.
3. **Endereco do sacado.** O modulo envia so `street_name` em `Endereco`; numero e
   complemento nao vao em campo algum. O adapter segue o modulo (so o logradouro).
4. **CPF/CNPJ e CEP como inteiros.** O modulo envia `int(...)`, o que descarta zeros a
   esquerda (o vetor mostra `Cep: 8473002` na ida e `"08473002"` na volta). O adapter
   segue o modulo. CNPJ alfanumerico nao cabe em inteiro: e recusado com `Validacao`.
   Confirmar com o PinBank se aceitam string e como tratarao o CNPJ alfanumerico.
5. **Idempotencia no banco.** O modulo nao manda identificador do cliente. A resposta
   traz `IdentificadorCliente` (nulo no vetor), talvez aceito na geracao. Hoje a garantia
   contra boleto em dobro e do adapter (cache e lock por `chave_idempotencia`, no
   processo) e do `Orquestrador` (repositorio de chaves, entre processos).
6. **PDF.** `RetornarBase64` devolve o PDF so na geracao; nao ha endpoint de segunda via
   no modulo e a consulta nao traz `Base64`. O adapter nao declara `PDF`. O PDF recebido
   na geracao fica em `resposta_bruta` mascarado ("***"). Falta um campo no modelo
   canonico para entrega-lo (decisao de produto) ou confirmar endpoint de segunda via.
7. **Webhook.** O modulo so faz polling (cron de consulta). Sem contrato: `parse_webhook`
   levanta `NaoSuportado` e `WEBHOOK` nao e declarada.
8. **Resposta da baixa.** O modulo ignora o corpo da baixa e reconsulta. O adapter faz
   igual e devolve o estado consultado, que pode ainda nao ser `BAIXADO`.
9. **Status.** Mapa extraido do modulo: PENDENTE, REGISTRADO, LIQUIDADO, BAIXA. Se o
   banco tiver outros (vencido, protestado, rejeitado), o adapter levanta `Validacao`
   (nunca assume "pago").
10. **Expiracao do token.** O campo e `expire_in` (sem "s"); o autenticador aceita os dois.
    Sem escopo OAuth: o cache e por (conta, sem escopo).
11. **Chave AES = senha do usuario**, IV zero, como no modulo. O IV fixo torna o cifrado
    deterministico: a protecao real e o TLS.
12. **Erro HTTP com corpo cifrado ou nao.** O modulo le `Message` em claro nos erros;
    o adapter nao decifra respostas >= 400 e as repassa mascaradas.
13. **Valor do boleto como `Valor` em centavos e `ValorPago` em centavos** (vetor:
    `Valor: 25875`). `ValorPago` so e lido quando LIQUIDADO.
