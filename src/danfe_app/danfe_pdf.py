"""Geração do PDF do DANFE (NF-e modelo 55, retrato A4) a partir do XML."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lxml import etree
from reportlab.graphics.barcode.code128 import Code128
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

NS = {"n": "http://www.portalfiscal.inf.br/nfe"}

MOD_FRETE = {
    "0": "0-Emitente", "1": "1-Destinatário", "2": "2-Terceiros",
    "3": "3-Próprio Rem.", "4": "4-Próprio Dest.", "9": "9-Sem Frete",
}


# Expressões regulares removidas das informações adicionais do produto
# (config.json: "remover_do_texto_produto").
FILTROS_INF_AD_PROD: list[str] = []


class ErroXml(Exception):
    pass
def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v).strip()) if v not in (None, "") else None
    except InvalidOperation:
        return None


def fmt_num(v, casas=2, vazio="") -> str:
    d = _dec(v)
    if d is None:
        return vazio
    s = f"{d:,.{casas}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_doc(v: str) -> str:
    v = "".join(c for c in (v or "") if c.isdigit())
    if len(v) == 14:
        return f"{v[:2]}.{v[2:5]}.{v[5:8]}/{v[8:12]}-{v[12:]}"
    if len(v) == 11:
        return f"{v[:3]}.{v[3:6]}.{v[6:9]}-{v[9:]}"
    return v


def fmt_cep(v: str) -> str:
    v = (v or "").strip()
    return f"{v[:5]}-{v[5:]}" if len(v) == 8 else v


def fmt_fone(v: str) -> str:
    v = (v or "").strip()
    if len(v) == 10:
        return f"({v[:2]}) {v[2:6]}-{v[6:]}"
    if len(v) == 11:
        return f"({v[:2]}) {v[2:7]}-{v[7:]}"
    return v


def fmt_data(v: str) -> str:
    v = (v or "").strip()
    return f"{v[8:10]}/{v[5:7]}/{v[0:4]}" if len(v) >= 10 else v


def fmt_hora(v: str) -> str:
    v = (v or "").strip()
    return v[11:19] if len(v) >= 19 else ""


def fmt_chave(ch: str) -> str:
    return " ".join(ch[i:i + 4] for i in range(0, len(ch), 4))
@dataclass
class Item:
    codigo: str
    descricao: str
    ncm: str
    cst: str
    cfop: str
    unidade: str
    qtd: str
    v_unit: str
    v_total: str
    bc_icms: str
    v_icms: str
    v_ipi: str
    aliq_icms: str
    aliq_ipi: str


@dataclass
class NotaFiscal:
    chave: str
    d: dict = field(default_factory=dict)
    itens: list[Item] = field(default_factory=list)
    duplicatas: list[tuple[str, str, str]] = field(default_factory=list)
    protocolo: str = ""
    autorizada: bool = False

    @property
    def homologacao(self) -> bool:
        return self.d.get("tpAmb") == "2"


def _t(el, caminho: str) -> str:
    if el is None:
        return ""
    r = el.find(caminho, NS)
    return (r.text or "").strip() if r is not None and r.text else ""


def ler_nfe(xml: bytes) -> NotaFiscal:
    try:
        raiz = etree.fromstring(xml)
    except etree.XMLSyntaxError as e:
        raise ErroXml(f"XML mal formado: {e}") from e
    inf = raiz if raiz.tag == f"{{{NS['n']}}}infNFe" else raiz.find(".//n:infNFe", NS)
    if inf is None:
        raise ErroXml("o arquivo não contém uma NF-e (infNFe não encontrado)")

    nf = NotaFiscal(chave=(inf.get("Id") or "")[3:])
    ide, emit, dest = inf.find("n:ide", NS), inf.find("n:emit", NS), inf.find("n:dest", NS)
    tot = inf.find("n:total/n:ICMSTot", NS)
    transp, adic = inf.find("n:transp", NS), inf.find("n:infAdic", NS)
    ee = emit.find("n:enderEmit", NS) if emit is not None else None
    ed = dest.find("n:enderDest", NS) if dest is not None else None
    tr = transp.find("n:transporta", NS) if transp is not None else None
    vei = transp.find("n:veicTransp", NS) if transp is not None else None

    d = nf.d
    for k in ("nNF", "serie", "natOp", "tpNF", "tpAmb", "tpEmis", "mod"):
        d[k] = _t(ide, f"n:{k}")
    d["dhEmi"] = _t(ide, "n:dhEmi") or _t(ide, "n:dEmi")
    d["dhSaiEnt"] = _t(ide, "n:dhSaiEnt") or _t(ide, "n:dSaiEnt")

    d["emit_nome"] = _t(emit, "n:xNome")
    d["emit_fant"] = _t(emit, "n:xFant")
    d["emit_doc"] = _t(emit, "n:CNPJ") or _t(emit, "n:CPF")
    d["emit_ie"], d["emit_iest"] = _t(emit, "n:IE"), _t(emit, "n:IEST")
    end = f"{_t(ee, 'n:xLgr')}, {_t(ee, 'n:nro')}"
    if _t(ee, "n:xCpl"):
        end += f" - {_t(ee, 'n:xCpl')}"
    d["emit_end1"] = end
    d["emit_end2"] = f"{_t(ee, 'n:xBairro')} - CEP {fmt_cep(_t(ee, 'n:CEP'))}"
    d["emit_end3"] = f"{_t(ee, 'n:xMun')} - {_t(ee, 'n:UF')}" + \
        (f"   Fone: {fmt_fone(_t(ee, 'n:fone'))}" if _t(ee, "n:fone") else "")

    d["dest_nome"] = _t(dest, "n:xNome")
    d["dest_doc"] = _t(dest, "n:CNPJ") or _t(dest, "n:CPF") or _t(dest, "n:idEstrangeiro")
    d["dest_ie"] = _t(dest, "n:IE")
    end = f"{_t(ed, 'n:xLgr')}, {_t(ed, 'n:nro')}" if ed is not None else ""
    if _t(ed, "n:xCpl"):
        end += f" - {_t(ed, 'n:xCpl')}"
    d["dest_end"] = end
    d["dest_bairro"], d["dest_cep"] = _t(ed, "n:xBairro"), fmt_cep(_t(ed, "n:CEP"))
    d["dest_mun"], d["dest_uf"], d["dest_fone"] = _t(ed, "n:xMun"), _t(ed, "n:UF"), fmt_fone(_t(ed, "n:fone"))

    for k in ("vBC", "vICMS", "vBCST", "vST", "vProd", "vFrete", "vSeg", "vDesc",
              "vOutro", "vIPI", "vNF", "vTotTrib", "vII", "vFCP", "vICMSUFDest"):
        d[k] = _t(tot, f"n:{k}")

    d["mod_frete"] = MOD_FRETE.get(_t(transp, "n:modFrete"), _t(transp, "n:modFrete"))
    d["transp_nome"] = _t(tr, "n:xNome")
    d["transp_doc"] = _t(tr, "n:CNPJ") or _t(tr, "n:CPF")
    d["transp_ie"], d["transp_end"] = _t(tr, "n:IE"), _t(tr, "n:xEnder")
    d["transp_mun"], d["transp_uf"] = _t(tr, "n:xMun"), _t(tr, "n:UF")
    d["placa"], d["placa_uf"], d["rntc"] = _t(vei, "n:placa"), _t(vei, "n:UF"), _t(vei, "n:RNTC")
    vols = transp.findall("n:vol", NS) if transp is not None else []
    soma = lambda tag, casas: fmt_num(sum((_dec(_t(v, tag)) or 0) for v in vols), casas) \
        if any(_t(v, tag) for v in vols) else ""
    d["qVol"], d["pesoB"], d["pesoL"] = soma("n:qVol", 0), soma("n:pesoB", 3), soma("n:pesoL", 3)
    d["esp"] = _t(vols[0], "n:esp") if vols else ""
    d["marca"] = _t(vols[0], "n:marca") if vols else ""
    d["nVol"] = _t(vols[0], "n:nVol") if vols else ""

    d["infCpl"] = _t(adic, "n:infCpl")
    d["infAdFisco"] = _t(adic, "n:infAdFisco")

    cobr = inf.find("n:cobr", NS)
    if cobr is not None:
        fat = cobr.find("n:fat", NS)
        d["fat"] = (_t(fat, "n:nFat"), _t(fat, "n:vOrig"), _t(fat, "n:vDesc"), _t(fat, "n:vLiq"))
        for dup in cobr.findall("n:dup", NS):
            nf.duplicatas.append((_t(dup, "n:nDup"), fmt_data(_t(dup, "n:dVenc")), fmt_num(_t(dup, "n:vDup"))))

    for det in inf.findall("n:det", NS):
        p, imp = det.find("n:prod", NS), det.find("n:imposto", NS)
        icms_grp = imp.find("n:ICMS", NS) if imp is not None else None
        icms = icms_grp[0] if icms_grp is not None and len(icms_grp) else None
        cst = (_t(icms, "n:orig") + (_t(icms, "n:CST") or _t(icms, "n:CSOSN"))) if icms is not None else ""
        ipi = imp.find("n:IPI/n:IPITrib", NS) if imp is not None else None
        desc = _t(p, "n:xProd")
        inf_ad = _t(det, "n:infAdProd")
        for padrao in FILTROS_INF_AD_PROD:
            inf_ad = re.sub(padrao, "", inf_ad, flags=re.S).strip()
        if inf_ad:
            desc += "\n" + inf_ad
        nf.itens.append(Item(
            codigo=_t(p, "n:cProd"), descricao=desc, ncm=_t(p, "n:NCM"), cst=cst,
            cfop=_t(p, "n:CFOP"), unidade=_t(p, "n:uCom"),
            qtd=fmt_num(_t(p, "n:qCom"), 4), v_unit=fmt_num(_t(p, "n:vUnCom"), 4),
            v_total=fmt_num(_t(p, "n:vProd")), bc_icms=fmt_num(_t(icms, "n:vBC")),
            v_icms=fmt_num(_t(icms, "n:vICMS")), v_ipi=fmt_num(_t(ipi, "n:vIPI")),
            aliq_icms=fmt_num(_t(icms, "n:pICMS")), aliq_ipi=fmt_num(_t(ipi, "n:pIPI")),
        ))

    prot = raiz.find(".//n:protNFe/n:infProt", NS)
    if prot is not None:
        nf.autorizada = _t(prot, "n:cStat") in ("100", "150")
        dh = _t(prot, "n:dhRecbto")
        nf.protocolo = f"{_t(prot, 'n:nProt')} - {fmt_data(dh)} {fmt_hora(dh)}".strip(" -")
    return nf
LARG_PAG, ALT_PAG = A4
MARGEM = 7  # mm
LARGURA = 210 - 2 * MARGEM  # 196 mm
LIMITE_INF = 297 - MARGEM
ALT_ADIC = 31  # título + quadro de dados adicionais

COLUNAS = [  # (rótulo, largura mm, alinhamento, atributo)
    ("CÓDIGO", 18, "L", "codigo"), ("DESCRIÇÃO DO PRODUTO / SERVIÇO", 48, "L", "descricao"),
    ("NCM/SH", 13, "C", "ncm"), ("O/CST", 9, "C", "cst"), ("CFOP", 9, "C", "cfop"),
    ("UN", 8, "C", "unidade"), ("QUANT.", 14, "R", "qtd"), ("VALOR UNIT.", 15, "R", "v_unit"),
    ("VALOR TOTAL", 15, "R", "v_total"), ("B.CÁLC. ICMS", 13, "R", "bc_icms"),
    ("VALOR ICMS", 11, "R", "v_icms"), ("VALOR IPI", 9, "R", "v_ipi"),
    ("ALÍQ. ICMS", 7, "R", "aliq_icms"), ("ALÍQ. IPI", 7, "R", "aliq_ipi"),
]
FONTE_ITEM = 6
LINHA_ITEM = 2.5  # mm


class DanfeRenderer:
    def __init__(self, nf: NotaFiscal, logo: str | Path | None = None, marca_dagua: str | None = None):
        self.nf = nf
        self.logo = Path(logo) if logo and Path(logo).exists() else None
        self.marca = marca_dagua
        if marca_dagua is None:
            if nf.homologacao:
                self.marca = "SEM VALOR FISCAL"
            elif not nf.protocolo:
                self.marca = "SEM PROTOCOLO DE AUTORIZAÇÃO"
        self.c: canvas.Canvas | None = None

    # Coordenadas: y é medido a partir do topo da página, em mm.
    def _y(self, y: float) -> float:
        return ALT_PAG - y * mm

    def _texto(self, x, y, txt, fonte="Helvetica", tam=7, alinh="L", larg=None):
        c = self.c
        c.setFont(fonte, tam)
        if alinh == "C" and larg:
            c.drawCentredString((x + larg / 2) * mm, self._y(y), txt)
        elif alinh == "R" and larg:
            c.drawRightString((x + larg) * mm, self._y(y), txt)
        else:
            c.drawString(x * mm, self._y(y), txt)

    def _ajustar(self, txt, larg_mm, fonte, tam, minimo=5):
        while tam > minimo and stringWidth(txt, fonte, tam) > larg_mm * mm:
            tam -= 0.5
        while txt and stringWidth(txt, fonte, tam) > larg_mm * mm:
            txt = txt[:-2] + "…"
        return txt, tam

    def campo(self, x, y, w, h, rotulo, valor="", alinh="L", tam=8, negrito=False):
        c = self.c
        c.rect(x * mm, self._y(y + h), w * mm, h * mm)
        self._texto(x + 0.7, y + 2.0, rotulo, tam=5)
        if valor:
            fonte = "Helvetica-Bold" if negrito else "Helvetica"
            txt, t = self._ajustar(str(valor), w - 1.4, fonte, tam)
            self._texto(x + 0.7, y + h - 1.4, txt, fonte, t, alinh, w - 1.4)

    def caixa_texto(self, x, y, w, h, rotulo, texto, tam=6.5):
        self.c.rect(x * mm, self._y(y + h), w * mm, h * mm)
        self._texto(x + 0.7, y + 2.0, rotulo, tam=5)
        linhas_disp = lambda t: int((h - 3.2) / (t * 0.42))
        while True:
            linhas = []
            for par in (texto or "").splitlines() or [""]:
                linhas += simpleSplit(par, "Helvetica", tam, (w - 1.6) * mm) or [""]
            if len(linhas) <= linhas_disp(tam) or tam <= 5:
                break
            tam -= 0.5
        cab = linhas_disp(tam)
        if len(linhas) > cab:
            linhas = linhas[:cab]
            linhas[-1] = linhas[-1][:-4] + " (…)"
        for i, ln in enumerate(linhas):
            self._texto(x + 0.8, y + 3.2 + (i + 1) * tam * 0.42 - 0.4, ln, tam=tam)

    def titulo(self, y, txt) -> float:
        self._texto(MARGEM, y + 2.4, txt, "Helvetica-Bold", 6)
        return y + 3

    def marca_dagua(self):
        if not self.marca:
            return
        c = self.c
        c.saveState()
        c.setFillColorRGB(0.82, 0.82, 0.82)
        c.translate(LARG_PAG / 2, ALT_PAG / 2)
        c.rotate(52)
        tam = 46 if len(self.marca) < 20 else 30
        c.setFont("Helvetica-Bold", tam)
        c.drawCentredString(0, 0, self.marca)
        c.restoreState()
    def canhoto(self, y) -> float:
        d, x = self.nf.d, MARGEM
        txt = (f"RECEBEMOS DE {d['emit_nome']} OS PRODUTOS E/OU SERVIÇOS CONSTANTES DA NOTA FISCAL "
               f"ELETRÔNICA INDICADA AO LADO. EMISSÃO: {fmt_data(d['dhEmi'])}  DEST./REM.: {d['dest_nome']}  "
               f"VALOR TOTAL: R$ {fmt_num(d['vNF'])}")
        self.c.rect(x * mm, self._y(y + 8.5), 160 * mm, 8.5 * mm)
        linhas = simpleSplit(txt, "Helvetica", 6, 158 * mm)[:3]
        for i, ln in enumerate(linhas):
            self._texto(x + 1, y + 2.6 + i * 2.5, ln, tam=6)
        self.campo(x, y + 8.5, 40, 8.5, "DATA DE RECEBIMENTO")
        self.campo(x + 40, y + 8.5, 120, 8.5, "IDENTIFICAÇÃO E ASSINATURA DO RECEBEDOR")
        self.c.rect((x + 160) * mm, self._y(y + 17), 36 * mm, 17 * mm)
        self._texto(x + 160, y + 6, "NF-e", "Helvetica-Bold", 11, "C", 36)
        self._texto(x + 160, y + 11, f"Nº {self._numero()}", "Helvetica-Bold", 9, "C", 36)
        self._texto(x + 160, y + 15, f"SÉRIE {d['serie'].zfill(3)}", "Helvetica-Bold", 9, "C", 36)
        self.c.saveState()
        self.c.setDash(2, 2)
        self.c.line(x * mm, self._y(y + 19), (x + LARGURA) * mm, self._y(y + 19))
        self.c.restoreState()
        return y + 21

    def _numero(self):
        n = self.nf.d["nNF"].zfill(9)
        return f"{n[:3]}.{n[3:6]}.{n[6:]}"

    def cabecalho(self, y, folha, total) -> float:
        d, x, c = self.nf.d, MARGEM, self.c
        # emitente
        c.rect(x * mm, self._y(y + 32), 80 * mm, 32 * mm)
        self._texto(x + 0.7, y + 2.0, "IDENTIFICAÇÃO DO EMITENTE", tam=5)
        tx, tw = x + 1, 78
        if self.logo:
            try:
                img = ImageReader(str(self.logo))
                iw, ih = img.getSize()
                esc = min(22 / iw, 24 / ih)
                c.drawImage(img, (x + 1.5) * mm, self._y(y + 4 + ih * esc), iw * esc * mm, ih * esc * mm,
                            mask="auto")
                tx, tw = x + 25, 54
            except Exception:  # logo inválido não impede o DANFE
                pass
        nome = simpleSplit(d["emit_nome"], "Helvetica-Bold", 9, tw * mm)[:3]
        yy = y + 7
        for ln in nome:
            self._texto(tx, yy, ln, "Helvetica-Bold", 9, "C", tw)
            yy += 3.6
        yy += 1
        for ln in (d["emit_end1"], d["emit_end2"], d["emit_end3"]):
            for sub in simpleSplit(ln, "Helvetica", 7, tw * mm)[:2]:
                self._texto(tx, yy, sub, tam=7, alinh="C", larg=tw)
                yy += 3
        # DANFE
        bx = x + 80
        c.rect(bx * mm, self._y(y + 32), 30 * mm, 32 * mm)
        self._texto(bx, y + 5, "DANFE", "Helvetica-Bold", 12, "C", 30)
        self._texto(bx, y + 8, "Documento Auxiliar da", tam=6, alinh="C", larg=30)
        self._texto(bx, y + 10.3, "Nota Fiscal Eletrônica", tam=6, alinh="C", larg=30)
        self._texto(bx + 3, y + 15, "0 - ENTRADA", tam=6.5)
        self._texto(bx + 3, y + 18, "1 - SAÍDA", tam=6.5)
        c.rect((bx + 21) * mm, self._y(y + 19), 6 * mm, 6 * mm)
        self._texto(bx + 21, y + 17.5, d["tpNF"], "Helvetica-Bold", 11, "C", 6)
        self._texto(bx, y + 23.5, f"Nº {self._numero()}", "Helvetica-Bold", 8, "C", 30)
        self._texto(bx, y + 27, f"SÉRIE {d['serie'].zfill(3)}", "Helvetica-Bold", 8, "C", 30)
        self._texto(bx, y + 30.5, f"FOLHA {folha}/{total}", tam=7, alinh="C", larg=30)
        # chave + código de barras
        kx = x + 110
        c.rect(kx * mm, self._y(y + 13), 86 * mm, 13 * mm)
        bc = Code128(self.nf.chave, barWidth=1, barHeight=10 * mm, quiet=False, humanReadable=False)
        bc = Code128(self.nf.chave, barWidth=(82 * mm) / bc.width, barHeight=10 * mm,
                     quiet=False, humanReadable=False)
        bc.drawOn(c, (kx + 2) * mm, self._y(y + 11.5))
        self.campo(kx, y + 13, 86, 7, "CHAVE DE ACESSO", fmt_chave(self.nf.chave), "C", 8, True)
        c.rect(kx * mm, self._y(y + 32), 86 * mm, 12 * mm)
        self._texto(kx, y + 25, "Consulta de autenticidade no portal nacional da NF-e", tam=7, alinh="C", larg=86)
        self._texto(kx, y + 28.2, "www.nfe.fazenda.gov.br/portal ou no site da Sefaz Autorizadora",
                    tam=7, alinh="C", larg=86)
        y += 32
        self.campo(x, y, 110, 7, "NATUREZA DA OPERAÇÃO", d["natOp"])
        self.campo(x + 110, y, 86, 7, "PROTOCOLO DE AUTORIZAÇÃO DE USO", self.nf.protocolo, "C", 8, True)
        y += 7
        self.campo(x, y, 65, 7, "INSCRIÇÃO ESTADUAL", d["emit_ie"])
        self.campo(x + 65, y, 65, 7, "INSCRIÇÃO ESTADUAL DO SUBST. TRIBUT.", d["emit_iest"])
        self.campo(x + 130, y, 66, 7, "CNPJ / CPF", fmt_doc(d["emit_doc"]))
        return y + 7

    def destinatario(self, y) -> float:
        d, x = self.nf.d, MARGEM
        y = self.titulo(y, "DESTINATÁRIO / REMETENTE")
        self.campo(x, y, 120, 7, "NOME / RAZÃO SOCIAL", d["dest_nome"])
        self.campo(x + 120, y, 46, 7, "CNPJ / CPF", fmt_doc(d["dest_doc"]), "C")
        self.campo(x + 166, y, 30, 7, "DATA DA EMISSÃO", fmt_data(d["dhEmi"]), "C")
        y += 7
        self.campo(x, y, 100, 7, "ENDEREÇO", d["dest_end"])
        self.campo(x + 100, y, 45, 7, "BAIRRO / DISTRITO", d["dest_bairro"])
        self.campo(x + 145, y, 21, 7, "CEP", d["dest_cep"], "C")
        self.campo(x + 166, y, 30, 7, "DATA DA SAÍDA/ENTRADA", fmt_data(d["dhSaiEnt"]), "C")
        y += 7
        self.campo(x, y, 80, 7, "MUNICÍPIO", d["dest_mun"])
        self.campo(x + 80, y, 40, 7, "FONE / FAX", d["dest_fone"])
        self.campo(x + 120, y, 10, 7, "UF", d["dest_uf"], "C")
        self.campo(x + 130, y, 36, 7, "INSCRIÇÃO ESTADUAL", d["dest_ie"])
        self.campo(x + 166, y, 30, 7, "HORA DA SAÍDA/ENTRADA", fmt_hora(d["dhSaiEnt"]), "C")
        return y + 7

    def _alt_fatura(self) -> float:
        if self.nf.duplicatas:
            return 3 + 6 * ((len(self.nf.duplicatas) + 3) // 4)
        return 3 + 6 if self.nf.d.get("fat") else 0

    def fatura(self, y) -> float:
        x = MARGEM
        if self.nf.duplicatas:
            y = self.titulo(y, "FATURA / DUPLICATAS")
            for i, (num, venc, val) in enumerate(self.nf.duplicatas):
                cx, cy = x + (i % 4) * 49, y + (i // 4) * 6
                self.c.rect(cx * mm, self._y(cy + 6), 49 * mm, 6 * mm)
                self._texto(cx + 1, cy + 2.4, f"Núm.: {num}", tam=6)
                self._texto(cx + 1, cy + 5, f"Venc.: {venc}", tam=6)
                self._texto(cx + 27, cy + 5, f"R$ {val}", "Helvetica-Bold", 6.5, "R", 21)
            return y + 6 * ((len(self.nf.duplicatas) + 3) // 4)
        if self.nf.d.get("fat"):
            n, vo, vd, vl = self.nf.d["fat"]
            y = self.titulo(y, "FATURA")
            self.campo(x, y, LARGURA, 6, "",
                       f"Nº {n}   Valor original: R$ {fmt_num(vo)}   Desconto: R$ {fmt_num(vd, vazio='0,00')}   "
                       f"Valor líquido: R$ {fmt_num(vl)}", tam=7)
            return y + 6
        return y

    def impostos(self, y) -> float:
        d, x = self.nf.d, MARGEM
        y = self.titulo(y, "CÁLCULO DO IMPOSTO")
        w = LARGURA / 6
        l1 = [("BASE DE CÁLC. DO ICMS", "vBC"), ("VALOR DO ICMS", "vICMS"), ("BASE DE CÁLC. ICMS S.T.", "vBCST"),
              ("VALOR DO ICMS SUBST.", "vST"), ("V. APROX. TRIBUTOS", "vTotTrib"), ("VALOR TOTAL PRODUTOS", "vProd")]
        l2 = [("VALOR DO FRETE", "vFrete"), ("VALOR DO SEGURO", "vSeg"), ("DESCONTO", "vDesc"),
              ("OUTRAS DESPESAS", "vOutro"), ("VALOR TOTAL IPI", "vIPI"), ("VALOR TOTAL DA NOTA", "vNF")]
        for linha in (l1, l2):
            for i, (rot, k) in enumerate(linha):
                self.campo(x + i * w, y, w, 7, rot, fmt_num(d.get(k), vazio="0,00"), "R", 8, k == "vNF")
            y += 7
        return y

    def transporte(self, y) -> float:
        d, x = self.nf.d, MARGEM
        y = self.titulo(y, "TRANSPORTADOR / VOLUMES TRANSPORTADOS")
        self.campo(x, y, 70, 7, "NOME / RAZÃO SOCIAL", d["transp_nome"])
        self.campo(x + 70, y, 30, 7, "FRETE POR CONTA", d["mod_frete"], "C", 7)
        self.campo(x + 100, y, 22, 7, "CÓDIGO ANTT", d["rntc"], "C")
        self.campo(x + 122, y, 20, 7, "PLACA DO VEÍCULO", d["placa"], "C")
        self.campo(x + 142, y, 10, 7, "UF", d["placa_uf"], "C")
        self.campo(x + 152, y, 44, 7, "CNPJ / CPF", fmt_doc(d["transp_doc"]), "C")
        y += 7
        self.campo(x, y, 96, 7, "ENDEREÇO", d["transp_end"])
        self.campo(x + 96, y, 56, 7, "MUNICÍPIO", d["transp_mun"])
        self.campo(x + 152, y, 10, 7, "UF", d["transp_uf"], "C")
        self.campo(x + 162, y, 34, 7, "INSCRIÇÃO ESTADUAL", d["transp_ie"])
        y += 7
        for i, (rot, k, al) in enumerate([("QUANTIDADE", "qVol", "R"), ("ESPÉCIE", "esp", "L"),
                                          ("MARCA", "marca", "L"), ("NUMERAÇÃO", "nVol", "L"),
                                          ("PESO BRUTO", "pesoB", "R"), ("PESO LÍQUIDO", "pesoL", "R")]):
            self.campo(x + i * LARGURA / 6, y, LARGURA / 6, 7, rot, d[k], al)
        return y + 7

    def adicionais(self):
        d, x = self.nf.d, MARGEM
        y = self.titulo(LIMITE_INF - ALT_ADIC, "DADOS ADICIONAIS")
        self.caixa_texto(x, y, 130, ALT_ADIC - 3, "INFORMAÇÕES COMPLEMENTARES", d["infCpl"])
        self.caixa_texto(x + 130, y, 66, ALT_ADIC - 3, "RESERVADO AO FISCO", d["infAdFisco"])
    def _linhas_item(self, item: Item) -> list[str]:
        larg = (COLUNAS[1][1] - 1.2) * mm
        linhas = []
        for par in item.descricao.splitlines():
            linhas += simpleSplit(par, "Helvetica", FONTE_ITEM, larg) or [""]
        return linhas

    def _alt_item(self, item: Item) -> float:
        return max(1, len(self._linhas_item(item))) * LINHA_ITEM + 1.0

    def _cab_itens(self, y) -> float:
        y = self.titulo(y, "DADOS DO PRODUTO / SERVIÇO")
        cx = MARGEM
        for rot, w, _, _ in COLUNAS:
            self.c.rect(cx * mm, self._y(y + 6), w * mm, 6 * mm)
            linhas = simpleSplit(rot, "Helvetica", 5, (w - 0.8) * mm)[:2]
            base = y + (3.6 if len(linhas) == 1 else 2.5)
            for i, ln in enumerate(linhas):
                self._texto(cx, base + i * 2.1, ln, tam=5, alinh="C", larg=w)
            cx += w
        return y + 6

    def _desenhar_itens(self, y_ini, y_fim, itens: list[Item]):
        c = self.c
        c.rect(MARGEM * mm, self._y(y_fim), LARGURA * mm, (y_fim - y_ini) * mm)
        cx = MARGEM
        for _, w, _, _ in COLUNAS[:-1]:
            cx += w
            c.line(cx * mm, self._y(y_ini), cx * mm, self._y(y_fim))
        y = y_ini
        for n, item in enumerate(itens):
            h = self._alt_item(item)
            cx = MARGEM
            for _, w, al, attr in COLUNAS:
                if attr == "descricao":
                    for i, ln in enumerate(self._linhas_item(item)):
                        self._texto(cx + 0.6, y + 2.6 + i * LINHA_ITEM, ln, tam=FONTE_ITEM)
                else:
                    txt, t = self._ajustar(getattr(item, attr), w - 1.0, "Helvetica", FONTE_ITEM, 4.5)
                    self._texto(cx + 0.5, y + 2.6, txt, tam=t, alinh=al, larg=w - 1.0)
                cx += w
            y += h
            if n < len(itens) - 1:
                c.saveState()
                c.setStrokeColorRGB(0.75, 0.75, 0.75)
                c.setLineWidth(0.3)
                c.line(MARGEM * mm, self._y(y - 0.3), (MARGEM + LARGURA) * mm, self._y(y - 0.3))
                c.restoreState()
    def _paginar(self) -> list[list[Item]]:
        # topo da área de itens na página 1 e nas seguintes
        topo1 = MARGEM + 21 + 46 + 24 + self._alt_fatura() + 17 + 24 + 9
        cap1 = (LIMITE_INF - ALT_ADIC - 1) - topo1
        capn = LIMITE_INF - (MARGEM + 46 + 9)
        paginas, atual, usado, cap = [], [], 0.0, cap1
        for item in self.nf.itens:
            h = self._alt_item(item)
            if atual and usado + h > cap:
                paginas.append(atual)
                atual, usado, cap = [], 0.0, capn
            atual.append(item)
            usado += h
        paginas.append(atual)
        return paginas

    def desenhar(self, c: canvas.Canvas) -> int:
        """Desenha todas as folhas desta NF-e no canvas informado (permite juntar várias notas)."""
        self.c = c
        c.setLineWidth(0.5)
        paginas = self._paginar()
        total = len(paginas)
        for i, itens in enumerate(paginas, start=1):
            self.marca_dagua()
            c.setLineWidth(0.5)
            if i == 1:
                y = self.canhoto(MARGEM)
                y = self.cabecalho(y, i, total)
                y = self.destinatario(y)
                y = self.fatura(y)
                y = self.impostos(y)
                y = self.transporte(y)
                y = self._cab_itens(y)
                self._desenhar_itens(y, LIMITE_INF - ALT_ADIC - 1, itens)
                self.adicionais()
            else:
                y = self.cabecalho(MARGEM, i, total)
                y = self._cab_itens(y)
                self._desenhar_itens(y, LIMITE_INF, itens)
            c.showPage()
        return total

    def gerar(self, destino: str | Path) -> int:
        d = self.nf.d
        c = canvas.Canvas(str(destino), pagesize=A4)
        c.setTitle(f"DANFE NF-e {d['nNF']} - {self.nf.chave}")
        c.setAuthor(d["emit_nome"])
        total = self.desenhar(c)
        c.save()
        return total


def _validar_modelo(nf: NotaFiscal):
    if nf.d.get("mod") and nf.d["mod"] != "55":
        raise ErroXml(f"o XML é de modelo {nf.d['mod']}; este gerador cobre apenas NF-e modelo 55")


def gerar_danfe(xml: bytes, destino: str | Path, logo=None, marca_dagua=None) -> NotaFiscal:
    nf = ler_nfe(xml)
    _validar_modelo(nf)
    DanfeRenderer(nf, logo, marca_dagua).gerar(destino)
    return nf


def gerar_danfe_unico(itens: list[tuple[bytes, str | None]], destino: str | Path, titulo: str, logo=None) -> int:
    """Junta vários DANFEs (xml, marca d'água) num único PDF, na ordem recebida. Retorna nº de folhas."""
    c = canvas.Canvas(str(destino), pagesize=A4)
    c.setTitle(titulo)
    folhas = 0
    for xml, marca in itens:
        nf = ler_nfe(xml)
        _validar_modelo(nf)
        folhas += DanfeRenderer(nf, logo, marca).desenhar(c)
    c.save()
    return folhas
