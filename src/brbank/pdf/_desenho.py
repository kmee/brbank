"""Primitivas de desenho do boleto sobre o fpdf2 (unidade: milimetro)."""

from __future__ import annotations

from fpdf import FPDF

# Rotulo pequeno no canto superior do campo, valor embaixo (padrao Febraban).
TAM_ROTULO = 5.5
TAM_VALOR = 9.0
# Codigo de barras: 103 mm de largura e 13 mm de altura. O fpdf2 usa barra larga = 3
# finas; 44 digitos + inicio + fim somam 406 barras finas.
LARGURA_BARRAS = 103.0
ALTURA_BARRAS = 13.0
UNIDADES_BARRAS = 406


def latin1(texto: object) -> str:
    """As fontes padrao do PDF so tem Latin-1: o que sobrar vira '?'."""
    return str(texto or "").encode("latin-1", "replace").decode("latin-1")


class Desenho(FPDF):
    def __init__(self, fonte: str = "helvetica") -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.fonte = fonte
        self.set_auto_page_break(False)
        self.set_line_width(0.2)

    def texto(self, x, y, w, h, texto, tamanho=TAM_VALOR, estilo="", alinhar="L"):
        self.set_font(self.fonte, estilo, tamanho)
        self.set_xy(x, y)
        self.cell(w, h, latin1(texto), align=alinhar)

    def campo(self, x, y, w, h, rotulo, valor="", alinhar="L", estilo="", borda="LB"):
        """Caixa com rotulo em cima e valor embaixo; `borda` com L, R, B e T."""
        if "L" in borda:
            self.line(x, y, x, y + h)
        if "R" in borda:
            self.line(x + w, y, x + w, y + h)
        if "B" in borda:
            self.line(x, y + h, x + w, y + h)
        if "T" in borda:
            self.line(x, y, x + w, y)
        self.texto(x + 1, y + 0.3, w - 2, 2.6, rotulo, TAM_ROTULO)
        self.texto(x + 1, y + h - 4.6, w - 2, 4.2, valor, TAM_VALOR, estilo, alinhar)

    def paragrafo(self, x, y, w, texto, tamanho=7.0, entrelinha=3.0):
        """Texto que quebra sozinho na largura `w` (ex.: o Pix copia e cola)."""
        self.set_font(self.fonte, "", tamanho)
        self.set_xy(x, y)
        self.multi_cell(w, entrelinha, latin1(texto), wrapmode="CHAR")

    def linhas(self, x, y, w, linhas, tamanho=7.5, entrelinha=3.4):
        self.set_font(self.fonte, "", tamanho)
        for i, linha in enumerate(linhas):
            self.set_xy(x, y + i * entrelinha)
            self.cell(w, entrelinha, latin1(linha))

    def corte(self, y, x0, x1, legenda):
        self.set_dash_pattern(dash=1, gap=1)
        self.line(x0, y, x1, y)
        self.set_dash_pattern()
        self.texto(x1 - 60, y - 3, 60, 3, legenda, TAM_ROTULO, alinhar="R")

    def codigo_barras(self, x, y, codigo):
        self.set_fill_color(0)
        largura_larga = LARGURA_BARRAS / UNIDADES_BARRAS * 3
        self.interleaved2of5(codigo, x, y, w=largura_larga, h=ALTURA_BARRAS)

    def qr(self, x, y, lado, matriz):
        """QR Code em vetor, a partir da matriz booleana do pacote qrcode."""
        modulo = lado / len(matriz)
        self.set_fill_color(0)
        for i, linha in enumerate(matriz):
            for j, preto in enumerate(linha):
                if preto:
                    self.rect(x + j * modulo, y + i * modulo, modulo, modulo, "F")
