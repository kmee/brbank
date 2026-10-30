# brbank

Biblioteca Python assincrona para emitir e acompanhar boletos e cobrancas Pix em
bancos brasileiros por uma interface unica. Nao importa nada do Odoo: o modulo
`l10n_br_payment` e uma das faces possiveis.

Licenca MIT. Python 3.10+ (o 3.10 e o do Odoo 16.0; `brbank._compat` cobre o que so
existe no 3.11) e `cryptography>=38`, para conviver com o pyOpenSSL da imagem do Odoo. **Alfa:** so o
Sicoob (boleto e perfil Pix) foi validado em sandbox de banco (`validado_sandbox = True`); os
demais trazem `False` e o contrato vem da documentacao publica e de fontes de terceiros, com as
duvidas em `docs/<banco>-duvidas.md`.
Estado: primeira fatia (modelo canonico, contrato
de adapter, transporte, autenticacao, orquestrador e os adapters da tabela abaixo).

## Bancos suportados

Todos os adapters passam pela mesma suite de contrato (registrar, consultar, baixar) contra um
servidor falso. "Fonte do contrato" diz de onde veio o comportamento implementado: **Odoo** =
portado de um modulo que ja roda em producao; **oficial** = especificacao publicada pelo banco
(OpenAPI, WSDL ou portal); **publica e terceiros** = documentacao publica, integracoes e SDKs de
terceiros, porque o portal do banco exige login; **sandbox e swagger** = conferido no sandbox real
do banco e no swagger publicado. O detalhe e as duvidas de cada banco estao em
`docs/<banco>-duvidas.md`.

| Banco | Adapter (entry point) | Produto | Capacidades declaradas | Fonte do contrato |
| --- | --- | --- | --- | --- |
| Asaas (gateway) | `asaas` | API v3: boleto, Pix e fatura hospedada, chave de API | hibrido, alteracao, baixa, PDF, webhook, Pix cob e cobv | oficial (OpenAPI) |
| Inter (077) | `inter` | Cobranca v3, boleto hibrido | hibrido, so hibrido, baixa, PDF, webhook | Odoo |
| PinBank | `pinbank` | Boleto, envelope AES-CBC | baixa | Odoo |
| Banco do Brasil (001) | `bb` | Cobranca v2 | alteracao, baixa | homologacao |
| Banrisul (041) | `banrisul` | Cobranca v1, boleto hibrido | hibrido, alteracao, baixa, PDF | oficial |
| Bradesco (237) | `bradesco` | Cobranca convencional (sem QR Code) | baixa | publica e terceiros |
| BTG Pactual (208) | `btg` | Banking Collections, bolepix | hibrido, alteracao, baixa, PDF, webhook, emissao assincrona | oficial |
| C6 Bank (336) | `c6` | Bolepix v2, boleto hibrido | hibrido, alteracao, baixa, PDF, webhook | oficial |
| Caixa (104) | `caixa` | SIGCB, webservice SOAP | hibrido, alteracao, baixa | oficial (WSDL) |
| Itau (341) | `itau` | Cobranca v2 (CASH) e Bolecode | hibrido (com `bolecode`), alteracao, baixa | publica e terceiros |
| Safra (422) | `safra` | Cobranca v1 | so registro e consulta | publica e terceiros |
| Santander (033) | `santander` | Gestao de Boletos v2 (workspace) | hibrido (com chave Pix), alteracao, baixa, emissao assincrona | publica e terceiros |
| Sicoob (756) | `sicoob` | Cobranca v3, boleto hibrido | hibrido, alteracao, baixa, PDF | sandbox e swagger |
| Sicredi (748) | `sicredi` | Cobranca v2 | hibrido, alteracao, baixa, webhook | publica e terceiros |
| Unicred (136) | `unicred` | Cobranca, API key + usuario | baixa, PDF, webhook cifrado, emissao assincrona | oficial |

Pix (BACEN `/cob`, `/cobv`, `/webhook`) por um adapter generico, `pix_bacen`, com um perfil por PSP
(host, escopos, headers e quirks): Inter, Banco do Brasil, Sicoob, Sicredi, Itau, Bradesco,
Santander e Efi. Capacidades: Pix cob e cobv, webhook, baixa.

O Asaas e o primeiro gateway: um adapter de cobranca por API key, sem convenio bancario, que serve
boleto, Pix ou a fatura hospedada (`billing_type` da configuracao). Ele tambem implementa o protocolo
opcional de **recorrencia** (`brbank.recorrencia`, capacidade `RECORRENCIA`): `criar_assinatura`,
`alterar_assinatura`, `suspender`, `reativar`, `encerrar`, `listar_cobrancas` e, para cartao sem dado
de cartao na biblioteca, `criar_checkout` (pagina hospedada, capacidade `CHECKOUT`). O gateway gera a
cobranca de cada ciclo e avisa pelo webhook de cobranca (`EventoCanonico.id_recorrencia`); os eventos
de assinatura e de checkout chegam por `parse_webhook_recorrencia`. Fora desta lista por enquanto:
BNB, os demais gateways (Mercado Pago, PagBank e Stone, sob demanda), o boleto hibrido do Bradesco e
a variante ECOMM do Sicredi. Banco novo = um adapter com `BankAdapter`
e um entry point em `brbank.adapters`.

## Pagamento e extrato

`brbank.pagamento` define o contrato opcional `AdapterPagamento` (capacidades
`CapacidadePagamento`): pagamento so em lote (`LotePagamento` de um tipo: boleto, guia, Pix ou
transferencia), aceite e estado por linha, `reservar_numero_lote` antes do envio (o chamador
grava o numero e resolve um timeout consultando, nunca reenviando), `liberar_lote`,
`consultar_lote`, `consultar_pagamento` e `cancelar_pagamentos`. Os adapters de pagamento ficam
no entry point `brbank.adapters_pagamento` (`listar_adapters_pagamento`,
`obter_classe_pagamento`): o Odoo descobre o que cada banco suporta pelas
`capacidades_pagamento` da classe, sem instanciar.

**Pix copia e cola** (`TipoPagamento.PIX_QRCODE`, capacidade `PIX_QRCODE`): o `Pagamento` leva o
BR Code inteiro em `brcode` (conferido por `ler_brcode`; o valor tem de bater com o do QR
estatico que tem valor) e `pagar_com_juros_multa` para QR de cobranca com vencimento. O banco do
pagador le e valida o QR (e o JWS do dinamico); a biblioteca so manda o texto.

**Lote local** (capacidade `LOTE_LOCAL`): banco que paga um por vez tambem implementa
`pagar_lote`, mas envia linha a linha; o numero do lote e local, `consultar_lote` nao existe e cada
linha se acompanha por `consultar_pagamento` ou webhook. Linha sem resposta no envio de um
pagamento que executa na hora volta `INDETERMINADO` (pode ter sido pago: conferir antes de
reenviar) e as seguintes nao saem (`RECUSADO`, motivo `NAO_ENVIADO`). As chamadas que movem
dinheiro sem chave de idempotencia no banco saem sem retry automatico.

| Banco | Adapter (`brbank.adapters_pagamento`) | Tipos | Lote | Validacao |
| --- | --- | --- | --- | --- |
| Banco do Brasil | `bb` (`bb_pagamento`, Pagamentos em Lote v1) | boleto, guia, Pix (chave e conta), transferencia | do banco, com liberacao | homologacao (2026-10-28) |
| Sicoob | `sicoob` (`sicoob_pagamento`, Pagamentos Pix v2) | Pix copia e cola, Pix por chave e por conta | local | sandbox parcial: o QR nao executa no sandbox (`docs/sicoob-pagamento-duvidas.md`) |
| Inter | `inter` (`inter_pagamento`, Banking v2) | Pix copia e cola, Pix por chave e por conta, boleto, guia | local | so servidor falso (`docs/inter-pagamento-duvidas.md`) |
| Santander | `santander` (`santander_pagamento`, Payment Hub 1.1.7) | boleto, guia, Pix copia e cola, Pix por chave e por conta, transferencia | local, com autorizacao por `autorizar_lote` | so servidor falso, contrato oficial (`docs/santander-pagamento-duvidas.md`) |
| Itau | `itau` (`itau_pagamento`, SISPAG v1) | Pix copia e cola, Pix por chave e por conta | local | so servidor falso, contrato de terceiros: a especificacao exige login (`docs/itau-pagamento-duvidas.md`) |
| Bradesco | `bradesco` (`bradesco_pagamento`, APIs do portal Developers) | Pix por chave e por conta, boleto, guia, transferencia | local, paga na hora, sem liberacao | so servidor falso, contrato oficial (`docs/bradesco-pagamento-duvidas.md`) |

O BB nao tem pagamento por QR na API de lote (so chave e conta).

`brbank.extrato` define `AdapterExtrato`: `listar_lancamentos(conta, desde, ate, cursor)`
paginado, lancamentos (`LancamentoExtrato`, valor positivo com sinal C/D) separados das linhas
de saldo. No Odoo o consumidor e um provider do `account_statement_import_online` da OCA.
Primeiro adapter: `brbank.adapters.bb_extrato` (Extratos v2 do BB, mTLS, ainda sem validacao).
Como validar contra o BB: `docs/bb-homologacao.md`.

## Boleto, guia e Pix copia e cola

`brbank.instrumento` tem funcoes puras, sem rede, para o instrumento de um pagamento:
`normalizar_linha_digitavel` e `normalizar_codigo_barras` conferem os DVs do boleto (47 digitos) e
da guia de arrecadacao (48 digitos, modulo 10 ou 11 pelo identificador de valor),
`codigo_barras_da_linha` e `linha_digitavel_do_codigo` convertem um no outro, `tipo_codigo` diz se
e boleto ou guia, e `ler_brcode` le o Pix copia e cola (confere o CRC16 e devolve chave, URL do QR
dinamico, valor, nome e cidade do recebedor e txid), e `vencimento_do_boleto` tira o vencimento
do fator do codigo de barras (com o reinicio do fator em 22/02/2025). Codigo que nao confere
levanta `ValueError`; nada e inventado.

## DDA (boletos a pagar)

O lado de pagar: `brbank.dda` define o contrato opcional `AdapterDda` (capacidades `CapacidadeDda`:
ativacao, consulta, resumo, alteracao, webhook), no mesmo padrao da recorrencia. O `BtgAdapter` o
implementa: `ativar_dda`, `desativar_dda`, `listar_debitos(cnpj, desde, cursor)` (paginado),
`resumo_debitos`, `alterar_debito(cnpj, id, oculto=...)` e `parse_webhook_dda` (eventos
`authorized-direct-debits.*`; `parse_webhook` da cobranca passou a ignorar familias desconhecidas).
Cada boleto vira um `DebitoDda` (identidade: CNPJ do pagador mais linha digitavel normalizada).
Rotas, suposicoes e duvidas: `docs/btg-dda-duvidas.md`.

Para quem renova token em varios processos (Odoo com varios workers), `BtgConfig` aceita um
`armazem` (`brbank.auth.ArmazemToken`: `ler`, `gravar` e uma `trava` entre processos) e
`ler_refresh_token` (le o refresh token vigente dentro da trava, porque o BTG pode rotacionar). O
consentimento OAuth tem `url_autorizacao(...)` e `trocar_codigo(...)` (Authorization Code com PKCE
S256; `gerar_pkce()` gera o par).

O simulador da API do BTG, para testes e e2e (so stdlib):
`python -m brbank.simulador --porta 8899 --semente demo`. Ver `docs/simulador.md`.

**Validacao em sandbox.** Um adapter (ou perfil Pix) so passa a `validado_sandbox = True` depois
de rodar o ciclo contra o ambiente real do banco. Hoje: `sicoob` e o perfil Pix `sicoob`
(2026-10-20), o `bb`, o perfil Pix `bb` e o `bb_pagamento` (2026-10-28, homologacao do BB com
credencial de aplicacao "Em teste", em `tests/integracao/test_bb_homologacao.py`). Os outros seguem `False`: Inter e PinBank herdam a logica de modulos Odoo em
producao, mas o adapter da biblioteca ainda nao rodou contra o banco. O `sicoob_pagamento`
rodou contra o sandbox em 2026-10-29 (Pix por chave, por conta, consulta e webhook), mas segue
`False` porque a rota do QR no sandbox devolve sempre o exemplo de erro.

Os testes contra sandbox ficam em `tests/integracao/`, marcados `sandbox`, fora da execucao padrao
e pulados sem credencial. A credencial vem so de variavel de ambiente, nunca do repo:

```bash
BRBANK_SICOOB_SANDBOX_CLIENT_ID=... BRBANK_SICOOB_SANDBOX_TOKEN=... \
    uv run pytest -m sandbox tests/integracao
```

O Sicoob publica credencial de teste para todos (Client ID e token fixos na pagina Sandbox do
portal, para boleto e Pix). O BB exige uma aplicacao "Em teste" no portal: o teste le o arquivo
de credencial que o portal baixa (`BRBANK_BB_HM_CREDENCIAIS`, ver `docs/bb-homologacao.md`),
que nunca entra no repo. Banrisul, BTG, C6, Inter, Itau, Santander, Sicredi e Efi exigem
cadastro de aplicacao ou conta (o sandbox responde 401/403 sem credencial) e ficam para quando
houver credencial. O sandbox do Sicoob valida a requisicao contra o swagger, mas responde
exemplos fixos (boleto) ou aleatorios (Pix): prova rota, corpo, cabecalhos e codigos HTTP, nao a
transicao de estado nem o OAuth/mTLS de producao. Detalhe em `docs/sicoob-duvidas.md` e
`docs/pix-duvidas.md`.

## Uso

```python
from brbank import Orquestrador
from brbank.adapters.inter import InterAdapter, InterConfig

config = InterConfig(
    conta="matriz",
    client_id="...",
    client_secret="...",
    conta_corrente="123456",
    ambiente="sandbox",
    certificado_pem=cert_bytes,
    chave_pem=chave_bytes,  # vindos do cofre
)
adapter = InterAdapter(config)
orquestrador = Orquestrador(adapter, concorrencia=10)

registrado = await orquestrador.emitir(titulo)  # idempotente por chave
resultados = await orquestrador.emitir_em_varios(lote)  # falha isolada por titulo
```

Um banco novo e um pacote que registra o adapter no entry point `brbank.adapters`:

```toml
[project.entry-points."brbank.adapters"]
inter = "brbank.adapters.inter:InterAdapter"
pinbank = "brbank.adapters.pinbank:PinbankAdapter"
sicoob = "brbank.adapters.sicoob:SicoobAdapter"

sicredi = "brbank.adapters.sicredi:SicrediAdapter"

pix_bacen = "brbank.adapters.pix_bacen:PixBacenAdapter"
```

O `PinbankAdapter` usa OAuth2 `password` e payload cifrado AES-CBC (`EnvelopeAesCbc`),
registra, consulta e baixa por `NossoNumero`, e nao tem webhook nem PDF. Contrato em
aberto: `docs/pinbank-duvidas.md`.

O `SicoobAdapter` (Cobranca Bancaria v3, boleto hibrido) usa OAuth2 `client_credentials`
sem `client_secret` (a identidade e o mTLS), header `client_id`, id do titulo = nosso
numero, registra, consulta, baixa, envia instrucao (prorrogacao, abatimento, protesto) e
baixa o PDF da segunda via. Sem webhook. A especie do documento e a sigla (`"DM"`, `"DS"`...),
nao numero. **`validado_sandbox = True`** (2026-10-20, sandbox publico do Sicoob); OAuth/mTLS
e escopos de producao seguem sem prova. Duvidas: `docs/sicoob-duvidas.md`.

O `BancoDoBrasilAdapter` (`bb`, API Cobranca v2, convenio de 6 ou 7 digitos) usa OAuth2
`client_credentials` e a chave `gw-dev-app-key` em query param; registra, consulta, baixa,
prorroga e abate, sem webhook nem PDF. Nasce com `validado_sandbox = False`: ver
`docs/bb-duvidas.md`.

O `BradescoAdapter` (`bradesco`, API Cobranca convencional do portal Bradesco Developers)
usa OAuth2 `client_credentials` sobre mTLS (o fluxo legado com JWT nao e coberto); registra,
consulta e baixa pelo nosso numero gerado pelo banco. Sem alteracao, PDF nem webhook
declarados; desconto e abatimento no registro sao recusados ate haver contrato. Nasce com
`validado_sandbox = False`: ver `docs/bradesco-duvidas.md`.

O `SicrediAdapter` (API de Cobranca v2, OAuth2 `password`) registra, consulta, baixa e
altera (prorrogacao e abatimento); webhook sem assinatura exige reconsulta. Nasce com
`validado_sandbox = False`; contrato em aberto: `docs/sicredi-duvidas.md`.

O `PixBacenAdapter` fala a API Pix do BACEN (`/cob`, `/cobv`, `/webhook`, devolucao) e
muda de banco por `PerfilPsp`: `inter`, `bb`, `sicoob`, `sicredi`, `itau`, `bradesco`,
`santander` e `efi` (BNB sem perfil). So o perfil `sicoob` tem `validado_sandbox = True`; os
demais esperam o sandbox. O `txid` vem da chave de idempotencia (e e sempre o `id_banco`, mesmo
que o PSP devolva outro), o copia-e-cola e o `txid` chegam em
`TituloRegistrado`, e `CONCLUIDA` sem Pix na consulta nao liquida. Duvidas e quirks:
`docs/pix-duvidas.md`.

```python
config = PixBacenConfig(
    perfil="sicoob",
    conta="matriz",
    chave_pix="...",
    client_id="...",
    ambiente="producao",
    certificado_pem=cert,
    chave_pem=chave,
)
adapter = PixBacenAdapter(config)
```

`brbank.criar_adapter("inter", config)` resolve pelo nome e
`adapter.suporta(Capacidade.PDF)` consulta as capacidades em runtime.

## Regras

- Valores em `Decimal` (float e recusado). Percentuais em unidade de percentual.
- Nome, documento e endereco ficam fora do `repr`; log, erro e resposta bruta
  passam por `brbank.mascara.mascarar`.
- Retry so para 429, 5xx e erro de transporte. Status desconhecido no banco
  levanta erro, nunca vira "pago".
- Webhook gera `EventoCanonico` e nao confirma nada: reconsulte antes de liquidar.
