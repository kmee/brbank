"""Boleto em PDF (recibo do pagador e ficha de compensacao), no leiaute Febraban.

Para os bancos cuja API nao entrega o PDF (Banco do Brasil e Bradesco, por exemplo). Os
dados vem do que o banco devolveu no registro (`TituloRegistrado`: linha digitavel,
codigo de barras, nosso numero, Pix do boleto hibrido), do `Titulo` e da `Carteira` do
beneficiario. Nada e calculado por banco: o codigo de barras e o do banco, conferido.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from brbank.erros import NaoSuportado, Validacao
from brbank.instrumento import (
    linha_digitavel_do_codigo,
    normalizar_codigo_barras,
    normalizar_linha_digitavel,
)
from brbank.modelos import Pessoa, Titulo, TituloRegistrado
from brbank.tempo import hoje_brasil

# Codigo de compensacao com o digito (tabela, nao calculo) e nome impresso.
BANCOS = {
    "001": ("001-9", "Banco do Brasil S.A."),
    "033": ("033-7", "Banco Santander (Brasil) S.A."),
    "041": ("041-8", "Banrisul"),
    "077": ("077-9", "Banco Inter S.A."),
    "104": ("104-0", "Caixa Econômica Federal"),
    "136": ("136-8", "Unicred"),
    "208": ("208-1", "Banco BTG Pactual S.A."),
    "237": ("237-2", "Banco Bradesco S.A."),
    "336": ("336-0", "Banco C6 S.A."),
    "341": ("341-7", "Itaú Unibanco S.A."),
    "422": ("422-7", "Banco Safra S.A."),
    "748": ("748-X", "Banco Cooperativo Sicredi S.A."),
    "756": ("756-0", "Sicoob"),
}


@dataclass(frozen=True)
class Carteira:
    """Dados do contrato de cobranca que vao impressos, como o banco os define."""

    banco: str
    agencia_codigo: str
    carteira: str = ""
    especie_documento: str = "DM"
    aceite: str = "N"
    local_pagamento: str = "Pagável em qualquer banco até o vencimento."


@dataclass(frozen=True)
class ConfigBoleto:
    margem: float = 10.0
    fonte: str = "helvetica"
    # Logo do beneficiario (caminho ou bytes de imagem) no topo do recibo.
    logo: str | bytes | None = None
    data_processamento: date | None = None
    comprimir: bool = True


@dataclass(frozen=True)
class BoletoPdf:
    titulo: Titulo
    registrado: TituloRegistrado
    carteira: Carteira
    # Sem ele, vale o `titulo.beneficiario`.
    beneficiario: Pessoa | None = None
    instrucoes: tuple[str, ...] = field(default_factory=tuple)


# --- formatacao ----------------------------------------------------------------


def _dinheiro(valor: Decimal | None) -> str:
    if valor is None:
        return ""
    inteiro, centavos = f"{Decimal(valor):.2f}".split(".")
    return f"{int(inteiro):,}".replace(",", ".") + "," + centavos


def _data(dia: date | None) -> str:
    return dia.strftime("%d/%m/%Y") if dia else ""


def _documento(valor: str) -> str:
    v = "".join(c for c in valor or "" if c.isalnum()).upper()
    if len(v) == 11:
        return f"{v[:3]}.{v[3:6]}.{v[6:9]}-{v[9:]}"
    if len(v) == 14:
        return f"{v[:2]}.{v[2:5]}.{v[5:8]}/{v[8:12]}-{v[12:]}"
    return v


def _endereco(pessoa: Pessoa) -> list[str]:
    e = pessoa.endereco
    if e is None:
        return []
    rua = ", ".join(p for p in (e.logradouro, e.numero, e.complemento) if p)
    cep = f"{e.cep[:5]}-{e.cep[5:]}" if len(e.cep) == 8 else e.cep
    return [f"{rua} - {e.bairro}".strip(" -"), f"{e.cidade}/{e.uf} - CEP {cep}"]


def linha_formatada(linha: str) -> str:
    """AAAAA.AAAAA BBBBB.BBBBBB CCCCC.CCCCCC D EEEEEEEEEEEEEE"""
    return (
        f"{linha[0:5]}.{linha[5:10]} {linha[10:15]}.{linha[15:21]} "
        f"{linha[21:26]}.{linha[26:32]} {linha[32]} {linha[33:47]}"
    )


# --- conferencia ---------------------------------------------------------------


def codigos(boleto: BoletoPdf) -> tuple[str, str]:
    """(codigo de barras, linha digitavel) conferidos entre si e com o valor.

    O banco pode devolver so um dos dois: o outro e derivado. Divergencia (digito,
    linha de outro titulo, valor diferente) e `Validacao`: boleto errado nao se imprime.
    """
    reg = boleto.registrado
    if not (reg.codigo_barras or reg.linha_digitavel):
        raise Validacao(f"titulo {reg.id_banco}: o banco nao devolveu codigo de barras")
    try:
        codigo = normalizar_codigo_barras(reg.codigo_barras or reg.linha_digitavel)
        linha = linha_digitavel_do_codigo(codigo)
        if (
            reg.linha_digitavel
            and normalizar_linha_digitavel(reg.linha_digitavel) != linha
        ):
            raise ValueError("linha digitavel nao corresponde ao codigo de barras")
    except ValueError as erro:
        raise Validacao(f"titulo {reg.id_banco}: {erro}") from erro
    if codigo[0] == "8" or len(linha) != 47:
        raise Validacao(f"titulo {reg.id_banco}: codigo de guia, nao de boleto")
    valor_codigo = Decimal(codigo[9:19]) / 100
    if valor_codigo and valor_codigo != Decimal(boleto.titulo.valor):
        raise Validacao(
            f"titulo {reg.id_banco}: valor do codigo de barras ({valor_codigo}) "
            f"difere do titulo ({boleto.titulo.valor})"
        )
    return codigo, linha


# --- leiaute -------------------------------------------------------------------


def _instrucoes(boleto: BoletoPdf) -> list[str]:
    linhas = list(boleto.instrucoes) + list(boleto.titulo.mensagens)
    enc = boleto.titulo.encargos
    if enc.multa_percentual:
        linhas.append(
            f"Após o vencimento, multa de {_dinheiro(enc.multa_percentual)}%."
        )
    if enc.juros_mensal_percentual:
        linhas.append(
            f"Após o vencimento, juros de {_dinheiro(enc.juros_mensal_percentual)}% "
            "ao mês."
        )
    return linhas


def _matriz_qr(texto: str) -> list[list[bool]]:
    import qrcode

    qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(texto)
    qr.make(fit=True)
    return qr.get_matrix()


def _cabecalho(pdf, x, y, largura, banco, direita, tamanho_direita):
    codigo, nome = BANCOS.get(banco, (banco, ""))
    pdf.texto(x, y, 52, 7, nome, 9, "B")
    pdf.line(x + 52, y, x + 52, y + 7)
    pdf.texto(x + 52, y, 22, 7, codigo, 14, "B", "C")
    pdf.line(x + 74, y, x + 74, y + 7)
    pdf.texto(x + 75, y, largura - 75, 7, direita, tamanho_direita, "B", "R")
    pdf.set_line_width(0.6)
    pdf.line(x, y + 7, x + largura, y + 7)
    pdf.set_line_width(0.2)


def _recibo(pdf, cfg, b, benef, nosso, linha, x, y, w):
    """Recibo do pagador: o comprovante que fica com quem paga."""
    if cfg.logo:
        pdf.image(cfg.logo, x=x, y=y, h=14)
    pdf.texto(x + 50, y, w - 50, 5, benef.nome, 10, "B")
    pdf.texto(x + 50, y + 5, w - 50, 4, _documento(benef.documento), 8)
    for i, texto in enumerate(_endereco(benef)):
        pdf.texto(x + 50, y + 9 + i * 3.5, w - 50, 3.5, texto, 7.5)
    y += 18
    _cabecalho(pdf, x, y, w, b.carteira.banco, "Recibo do Pagador", 10)
    y += 7
    d = 140
    t = b.titulo
    pdf.campo(
        x, y, d, 9, "Beneficiário", f"{benef.nome} - {_documento(benef.documento)}"
    )
    pdf.campo(
        x + d,
        y,
        w - d,
        9,
        "Agência / Código do beneficiário",
        b.carteira.agencia_codigo,
        "R",
        borda="LRB",
    )
    y += 9
    pdf.campo(
        x, y, d, 9, "Pagador", f"{t.pagador.nome} - {_documento(t.pagador.documento)}"
    )
    pdf.campo(x + d, y, w - d, 9, "Nosso número", nosso, "R", borda="LRB")
    y += 9
    pdf.campo(x, y, 45, 9, "Número do documento", t.seu_numero)
    pdf.campo(x + 45, y, 45, 9, "Vencimento", _data(t.vencimento), estilo="B")
    pdf.campo(x + 90, y, 50, 9, "Valor do documento", _dinheiro(t.valor), "R", "B")
    pdf.campo(x + d, y, w - d, 9, "Autenticação mecânica", "", borda="LRB")
    y += 11
    pdf.texto(x, y, w, 4, f"Linha digitável: {linha_formatada(linha)}", 8)
    pix = b.registrado.pix_copia_cola
    if pix:
        # Boleto hibrido: no recibo vao o QR e o copia e cola inteiros.
        y += 6
        pdf.qr(x + w - 30, y, 30, _matriz_qr(pix))
        pdf.texto(x, y, w - 34, 5, "Pague com Pix", 10, "B")
        pdf.texto(
            x,
            y + 5,
            w - 34,
            4,
            "Aponte a câmera do app do banco para o QR Code ou "
            "use o Pix copia e cola abaixo.",
            7.5,
        )
        pdf.paragrafo(x, y + 11, w - 34, pix, 7)


def _ficha(pdf, cfg, b, benef, nosso, codigo, linha, x, y, w):
    """Ficha de compensacao: a parte que o banco le."""
    t, c = b.titulo, b.carteira
    d = 140  # largura da coluna da esquerda; a da direita e w - d
    r = w - d
    _cabecalho(pdf, x, y, w, c.banco, linha_formatada(linha), 10.5)
    y += 7
    pdf.campo(x, y, d, 9, "Local de pagamento", c.local_pagamento)
    pdf.campo(x + d, y, r, 9, "Vencimento", _data(t.vencimento), "R", "B", "LRB")
    y += 9
    pdf.campo(
        x, y, d, 9, "Beneficiário", f"{benef.nome} - {_documento(benef.documento)}"
    )
    pdf.campo(
        x + d,
        y,
        r,
        9,
        "Agência / Código do beneficiário",
        c.agencia_codigo,
        "R",
        borda="LRB",
    )
    y += 9
    processamento = _data(cfg.data_processamento or hoje_brasil())
    colunas = [
        (28, "Data do documento", processamento),
        (37, "Número do documento", t.seu_numero),
        (20, "Espécie doc.", c.especie_documento),
        (15, "Aceite", c.aceite),
        (40, "Data do processamento", processamento),
    ]
    cx = x
    for largura, rotulo, valor in colunas:
        pdf.campo(cx, y, largura, 9, rotulo, valor)
        cx += largura
    pdf.campo(x + d, y, r, 9, "Nosso número", nosso, "R", borda="LRB")
    y += 9
    colunas = [
        (28, "Uso do banco", ""),
        (22, "Carteira", c.carteira),
        (15, "Espécie", "R$"),
        (35, "Quantidade", ""),
        (40, "Valor", ""),
    ]
    cx = x
    for largura, rotulo, valor in colunas:
        pdf.campo(cx, y, largura, 9, rotulo, valor)
        cx += largura
    pdf.campo(
        x + d, y, r, 9, "(=) Valor do documento", _dinheiro(t.valor), "R", "B", "LRB"
    )
    y += 9
    altura = 40
    pdf.line(x, y, x, y + altura)
    pdf.line(x, y + altura, x + d, y + altura)
    pdf.texto(
        x + 1,
        y + 0.3,
        d - 2,
        2.6,
        "Instruções (texto de responsabilidade do beneficiário)",
        5.5,
    )
    qr = b.registrado.pix_copia_cola
    largura_texto = d - (38 if qr else 2)
    pdf.linhas(x + 1, y + 4, largura_texto, _instrucoes(b)[:10])
    if qr:
        pdf.texto(x + d - 37, y + 1, 36, 3, "Pague com Pix", 7, "B", "C")
        pdf.qr(x + d - 35, y + 4.5, 32, _matriz_qr(qr))
    rotulos = (
        "(-) Desconto / Abatimento",
        "(-) Outras deduções",
        "(+) Mora / Multa",
        "(+) Outros acréscimos",
        "(=) Valor cobrado",
    )
    for i, rotulo in enumerate(rotulos):
        pdf.campo(x + d, y + i * 8, r, 8, rotulo, "", "R", borda="LRB")
    y += altura
    pdf.line(x, y, x, y + 16)
    pdf.line(x + w, y, x + w, y + 16)
    pdf.line(x, y + 16, x + w, y + 16)
    pdf.texto(x + 1, y + 0.3, 30, 2.6, "Pagador", 5.5)
    pagador = t.pagador
    pdf.linhas(
        x + 1,
        y + 3,
        w - 2,
        [f"{pagador.nome} - {_documento(pagador.documento)}", *_endereco(pagador)],
        8,
        3.6,
    )
    y += 16
    pdf.texto(x, y + 0.5, d, 3, "Sacador / Avalista", 5.5)
    pdf.texto(
        x + d,
        y + 0.5,
        r,
        3,
        "Autenticação mecânica - Ficha de Compensação",
        5.5,
        alinhar="R",
    )
    pdf.codigo_barras(x, y + 5, codigo)


def gerar_boleto_pdf(
    boletos: Iterable[BoletoPdf], config: ConfigBoleto | None = None
) -> bytes:
    """PDF com um boleto por pagina (A4). Requer o extra `brbank[pdf]`.

    :raises Validacao: codigo de barras ausente, invalido ou que nao bate com o titulo.
    :raises NaoSuportado: fpdf2 ou qrcode nao instalados.
    """
    try:
        from brbank.pdf._desenho import Desenho
    except ImportError as erro:
        raise NaoSuportado("boleto em PDF requer o extra brbank[pdf] (fpdf2)") from erro
    cfg = config or ConfigBoleto()
    pdf = Desenho(cfg.fonte)
    pdf.set_compression(cfg.comprimir)
    paginas = 0
    for b in boletos:
        codigo, linha = codigos(b)
        benef = b.beneficiario or b.titulo.beneficiario
        if benef is None:
            raise Validacao(f"titulo {b.registrado.id_banco}: sem beneficiario")
        nosso = b.registrado.nosso_numero or b.registrado.id_banco
        pdf.add_page()
        x, w = cfg.margem, 210 - 2 * cfg.margem
        _recibo(pdf, cfg, b, benef, nosso, linha, x, cfg.margem, w)
        pdf.corte(112, x, x + w, "Corte na linha pontilhada")
        _ficha(pdf, cfg, b, benef, nosso, codigo, linha, x, 118, w)
        paginas += 1
    if not paginas:
        raise Validacao("nenhum boleto para gerar")
    return bytes(pdf.output())
