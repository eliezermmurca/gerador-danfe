"""Comunicação com o Ambiente Nacional da SEFAZ.

- NFeDistribuicaoDFe (consChNFe): baixa o XML da NF-e pela chave, para o CNPJ do
  certificado (emitente, destinatário ou transportador da nota).
- NFeRecepcaoEvento4: registra "Ciência da Operação" (210210), necessária para o
  destinatário receber o XML completo em vez do resumo.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import ssl
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import certifi
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import Encoding
from lxml import etree

from .certificado import Certificado

NS_NFE = "http://www.portalfiscal.inf.br/nfe"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"

URLS_PADRAO = {
    "producao": {
        "distribuicao": "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
        "evento": "https://www.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
    },
    "homologacao": {
        "distribuicao": "https://hom1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
        "evento": "https://hom1.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
    },
}


class ErroSefaz(Exception):
    def __init__(self, codigo: str, mensagem: str):
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


@dataclass
class RetornoDistribuicao:
    cstat: str
    motivo: str
    documentos: list[tuple[str, bytes]] = field(default_factory=list)  # (schema, xml)


def _envelope(corpo: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">'
        f"<soap12:Body>{corpo}</soap12:Body></soap12:Envelope>"
    ).encode("utf-8")


def _local(el, nome):
    r = el.xpath(f".//*[local-name()='{nome}']")
    return r[0] if r else None


def _texto(el, nome) -> str:
    r = _local(el, nome)
    return (r.text or "").strip() if r is not None else ""


def assinar_evento(evento: etree._Element, cert: Certificado) -> None:
    """Assinatura XMLDSig enveloped (RSA-SHA1 / C14N), padrão SEFAZ, sobre infEvento."""
    inf = evento.find(f"{{{NS_NFE}}}infEvento")
    ref_id = inf.get("Id")
    digest = base64.b64encode(hashlib.sha1(etree.tostring(inf, method="c14n")).digest()).decode()

    E = lambda tag, **at: etree.Element(f"{{{NS_DS}}}{tag}", **at)
    sig = etree.SubElement(evento, f"{{{NS_DS}}}Signature", nsmap={None: NS_DS})
    si = etree.SubElement(sig, f"{{{NS_DS}}}SignedInfo")
    si.append(E("CanonicalizationMethod", Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"))
    si.append(E("SignatureMethod", Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"))
    ref = etree.SubElement(si, f"{{{NS_DS}}}Reference", URI=f"#{ref_id}")
    tr = etree.SubElement(ref, f"{{{NS_DS}}}Transforms")
    tr.append(E("Transform", Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"))
    tr.append(E("Transform", Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"))
    ref.append(E("DigestMethod", Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"))
    etree.SubElement(ref, f"{{{NS_DS}}}DigestValue").text = digest

    assinatura = cert.chave_privada.sign(
        etree.tostring(si, method="c14n"), padding.PKCS1v15(), hashes.SHA1())
    etree.SubElement(sig, f"{{{NS_DS}}}SignatureValue").text = base64.b64encode(assinatura).decode()
    ki = etree.SubElement(sig, f"{{{NS_DS}}}KeyInfo")
    x5 = etree.SubElement(ki, f"{{{NS_DS}}}X509Data")
    etree.SubElement(x5, f"{{{NS_DS}}}X509Certificate").text = \
        base64.b64encode(cert.cert.public_bytes(Encoding.DER)).decode()


class ClienteSefaz:
    def __init__(self, cert: Certificado, *, ambiente: str, uf_autor_codigo: str, cnpj: str,
                 timeout: int = 30, tentativas: int = 3, verificar_ssl=True, urls: dict | None = None):
        if cert.par_pem is None:
            raise RuntimeError("use o certificado dentro de um bloco 'with'")
        self.cert = cert
        self.ambiente = ambiente
        self.tp_amb = "1" if ambiente == "producao" else "2"
        self.uf_autor = uf_autor_codigo
        self.cnpj = cnpj
        self.timeout = timeout
        self.tentativas = max(1, tentativas)
        self.urls = (urls or {}).get(ambiente) or URLS_PADRAO[ambiente]
        self.sessao = requests.Session()
        self.sessao.cert = cert.par_pem
        self.sessao.verify = verificar_ssl
        self.bloqueado = False  # ligado após rejeição 656 (consumo indevido)
    def _post(self, url: str, action: str, corpo: str) -> etree._Element:
        headers = {"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'}
        ultimo_erro = None
        for tentativa in range(1, self.tentativas + 1):
            try:
                r = self.sessao.post(url, data=_envelope(corpo), headers=headers, timeout=self.timeout)
            except requests.exceptions.SSLError as e:
                raise ErroSefaz("FALHA_SSL", f"erro de SSL/certificado ao conectar na SEFAZ: {e}") from e
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                ultimo_erro = e
                if tentativa < self.tentativas:
                    time.sleep(2 ** tentativa)
                    continue
                raise ErroSefaz("FALHA_CONEXAO",
                                f"sem resposta da SEFAZ após {self.tentativas} tentativas: {e}") from e
            if r.status_code == 403:
                raise ErroSefaz("CERTIFICADO_RECUSADO",
                                "a SEFAZ recusou o certificado (HTTP 403). Verifique se é um A1 ICP-Brasil válido.")
            if r.status_code >= 500 and b"Fault" not in r.content and tentativa < self.tentativas:
                time.sleep(2 ** tentativa)
                continue
            try:
                xml = etree.fromstring(r.content)
            except etree.XMLSyntaxError as e:
                raise ErroSefaz("RESPOSTA_INVALIDA",
                                f"resposta inesperada da SEFAZ (HTTP {r.status_code})") from e
            falha = _local(xml, "Fault")
            if falha is not None:
                raise ErroSefaz("FALHA_SEFAZ", f"SOAP Fault: {' '.join(falha.itertext()).strip()[:300]}")
            return xml
        raise ErroSefaz("FALHA_CONEXAO", f"SEFAZ indisponível: {ultimo_erro}")
    def consultar_chave(self, chave: str) -> RetornoDistribuicao:
        corpo = (
            '<nfeDistDFeInteresse xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe">'
            "<nfeDadosMsg>"
            f'<distDFeInt xmlns="{NS_NFE}" versao="1.01">'
            f"<tpAmb>{self.tp_amb}</tpAmb><cUFAutor>{self.uf_autor}</cUFAutor>"
            f"<CNPJ>{self.cnpj}</CNPJ><consChNFe><chNFe>{chave}</chNFe></consChNFe>"
            "</distDFeInt></nfeDadosMsg></nfeDistDFeInteresse>"
        )
        xml = self._post(self.urls["distribuicao"],
                         "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe/nfeDistDFeInteresse",
                         corpo)
        ret = _local(xml, "retDistDFeInt")
        if ret is None:
            raise ErroSefaz("RESPOSTA_INVALIDA", "resposta sem retDistDFeInt")
        retorno = RetornoDistribuicao(_texto(ret, "cStat"), _texto(ret, "xMotivo"))
        for doc in ret.xpath(".//*[local-name()='docZip']"):
            conteudo = gzip.decompress(base64.b64decode(doc.text))
            retorno.documentos.append((doc.get("schema", ""), conteudo))
        if retorno.cstat == "656":
            self.bloqueado = True
        return retorno
    def manifestar_ciencia(self, chave: str) -> tuple[str, str]:
        dh = datetime.now().astimezone().isoformat(timespec="seconds")
        env = etree.Element(f"{{{NS_NFE}}}envEvento", nsmap={None: NS_NFE}, versao="1.00")
        etree.SubElement(env, f"{{{NS_NFE}}}idLote").text = str(int(time.time() * 1000))[-15:]
        evento = etree.SubElement(env, f"{{{NS_NFE}}}evento", versao="1.00")
        inf = etree.SubElement(evento, f"{{{NS_NFE}}}infEvento", Id=f"ID210210{chave}01")
        for tag, valor in [("cOrgao", "91"), ("tpAmb", self.tp_amb), ("CNPJ", self.cnpj),
                           ("chNFe", chave), ("dhEvento", dh), ("tpEvento", "210210"),
                           ("nSeqEvento", "1"), ("verEvento", "1.00")]:
            etree.SubElement(inf, f"{{{NS_NFE}}}{tag}").text = valor
        det = etree.SubElement(inf, f"{{{NS_NFE}}}detEvento", versao="1.00")
        etree.SubElement(det, f"{{{NS_NFE}}}descEvento").text = "Ciencia da Operacao"
        assinar_evento(evento, self.cert)

        corpo = ('<nfeDadosMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4">'
                 + etree.tostring(env, encoding="unicode") + "</nfeDadosMsg>")
        xml = self._post(self.urls["evento"],
                         "http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4/nfeRecepcaoEvento",
                         corpo)
        ret_evento = _local(xml, "retEvento")
        if ret_evento is not None:
            return _texto(ret_evento, "cStat"), _texto(ret_evento, "xMotivo")
        return _texto(xml, "cStat"), _texto(xml, "xMotivo")
# NFeConsultaProtocolo4: consulta a situação/protocolo da NF-e na SEFAZ
# autorizadora da UF de emissão. Disponível ao emitente (sem captcha).
_SVRS = ("https://nfe.svrs.rs.gov.br/ws/NfeConsulta/NfeConsulta4.asmx",
         "https://nfe-homologacao.svrs.rs.gov.br/ws/NfeConsulta/NfeConsulta4.asmx")
_SVAN = ("https://www.sefazvirtual.fazenda.gov.br/NFeConsultaProtocolo4/NFeConsultaProtocolo4.asmx",
         "https://hom.sefazvirtual.fazenda.gov.br/NFeConsultaProtocolo4/NFeConsultaProtocolo4.asmx")
URLS_CONSULTA_PROTOCOLO = {  # sigla UF -> (produção, homologação); sobrescrevível no config
    "SP": ("https://nfe.fazenda.sp.gov.br/ws/nfeconsultaprotocolo4.asmx",
           "https://homologacao.nfe.fazenda.sp.gov.br/ws/nfeconsultaprotocolo4.asmx"),
    "MG": ("https://nfe.fazenda.mg.gov.br/nfe2/services/NFeConsultaProtocolo4",
           "https://hnfe.fazenda.mg.gov.br/nfe2/services/NFeConsultaProtocolo4"),
    "PR": ("https://nfe.sefa.pr.gov.br/nfe/NFeConsultaProtocolo4",
           "https://homologacao.nfe.sefa.pr.gov.br/nfe/NFeConsultaProtocolo4"),
    "RS": ("https://nfe.sefazrs.rs.gov.br/ws/NfeConsulta/NfeConsulta4.asmx",
           "https://nfe-homologacao.sefazrs.rs.gov.br/ws/NfeConsulta/NfeConsulta4.asmx"),
    "BA": ("https://nfe.sefaz.ba.gov.br/webservices/NFeConsultaProtocolo4/NFeConsultaProtocolo4.asmx",
           "https://hnfe.sefaz.ba.gov.br/webservices/NFeConsultaProtocolo4/NFeConsultaProtocolo4.asmx"),
    "GO": ("https://nfe.sefaz.go.gov.br/nfe/services/NFeConsultaProtocolo4",
           "https://homolog.sefaz.go.gov.br/nfe/services/NFeConsultaProtocolo4"),
    "MT": ("https://nfe.sefaz.mt.gov.br/nfews/v2/services/NfeConsulta4",
           "https://homologacao.sefaz.mt.gov.br/nfews/v2/services/NfeConsulta4"),
    "MS": ("https://nfe.sefaz.ms.gov.br/ws/NFeConsultaProtocolo4",
           "https://hom.nfe.sefaz.ms.gov.br/ws/NFeConsultaProtocolo4"),
    "PE": ("https://nfe.sefaz.pe.gov.br/nfe-service/services/NFeConsultaProtocolo4",
           "https://nfehomolog.sefaz.pe.gov.br/nfe-service/services/NFeConsultaProtocolo4"),
    "AM": ("https://nfe.sefaz.am.gov.br/services2/services/NfeConsulta4",
           "https://homnfe.sefaz.am.gov.br/services2/services/NfeConsulta4"),
    "MA": _SVAN,
    **{uf: _SVRS for uf in ("AC", "AL", "AP", "CE", "DF", "ES", "PA", "PB", "PI", "RJ",
                            "RN", "RO", "RR", "SC", "SE", "TO")},
}


@dataclass
class RetornoProtocolo:
    cstat: str
    motivo: str
    prot_nfe: bytes | None = None          # <protNFe> pronto para anexar
    cancelada: bool = False


def consultar_protocolo(sessao: requests.Session, url: str, chave: str, tp_amb: str,
                        timeout: int = 30, tentativas: int = 3) -> RetornoProtocolo:
    corpo = (
        '<nfeDadosMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeConsultaProtocolo4">'
        f'<consSitNFe xmlns="{NS_NFE}" versao="4.00"><tpAmb>{tp_amb}</tpAmb>'
        f"<xServ>CONSULTAR</xServ><chNFe>{chave}</chNFe></consSitNFe></nfeDadosMsg>"
    )
    headers = {"Content-Type": 'application/soap+xml; charset=utf-8; '
                               'action="http://www.portalfiscal.inf.br/nfe/wsdl/NFeConsultaProtocolo4/nfeConsultaNF"'}
    for tentativa in range(1, tentativas + 1):
        try:
            r = sessao.post(url, data=_envelope(corpo), headers=headers, timeout=timeout)
            break
        except requests.exceptions.SSLError as e:
            raise ErroSefaz("FALHA_SSL", f"erro de SSL/certificado na SEFAZ autorizadora: {e}") from e
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if tentativa == tentativas:
                raise ErroSefaz("FALHA_CONEXAO", f"sem resposta da SEFAZ autorizadora: {e}") from e
            time.sleep(2 ** tentativa)
    if r.status_code == 403:
        raise ErroSefaz("CERTIFICADO_RECUSADO", "a SEFAZ autorizadora recusou o certificado (HTTP 403)")
    try:
        xml = etree.fromstring(r.content)
    except etree.XMLSyntaxError as e:
        raise ErroSefaz("RESPOSTA_INVALIDA", f"resposta inesperada da SEFAZ (HTTP {r.status_code})") from e
    falha = _local(xml, "Fault")
    if falha is not None:
        raise ErroSefaz("FALHA_SEFAZ", f"SOAP Fault: {' '.join(falha.itertext()).strip()[:300]}")
    ret = _local(xml, "retConsSitNFe")
    if ret is None:
        raise ErroSefaz("RESPOSTA_INVALIDA", "resposta sem retConsSitNFe")
    out = RetornoProtocolo(_texto(ret, "cStat"), _texto(ret, "xMotivo"))
    prot = _local(ret, "protNFe")
    if prot is not None:
        out.prot_nfe = etree.tostring(_copiar_no_ns_nfe(prot), encoding="utf-8")
    out.cancelada = out.cstat in ("101", "151") or any(
        (t.text or "").strip() == "110111" for t in ret.xpath(".//*[local-name()='tpEvento']"))
    return out


def _copiar_no_ns_nfe(el, pai=None):
    """Recria o elemento (e filhos) no namespace da NF-e, sem prefixos/namespaces do SOAP."""
    tag = f"{{{NS_NFE}}}{etree.QName(el).localname}"
    novo = etree.Element(tag, nsmap={None: NS_NFE}) if pai is None else etree.SubElement(pai, tag)
    for k, v in el.attrib.items():
        novo.set(etree.QName(k).localname, v)
    novo.text = el.text
    for filho in el:
        if isinstance(filho.tag, str):
            _copiar_no_ns_nfe(filho, novo).tail = None
    return novo


def montar_proc_nfe(xml_nfe: bytes, prot_nfe: bytes) -> bytes:
    """Une <NFe> assinada + <protNFe> em <nfeProc>, sem alterar a NF-e (assinatura preservada)."""
    raiz = etree.fromstring(xml_nfe)
    nfe = raiz if etree.QName(raiz).localname == "NFe" else raiz.xpath("//*[local-name()='NFe']")[0]
    corpo_nfe = etree.tostring(nfe, method="c14n")
    return (b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">'
            + corpo_nfe + prot_nfe.replace(b"<?xml version='1.0' encoding='utf-8'?>\n", b"") + b"</nfeProc>")
# Confiança SSL: os servidores das SEFAZ estaduais (ex.: SP) usam cadeia ICP-Brasil,
# ausente do pacote de certificados do Python. Monta um bundle com:
#   certifi + repositório do Windows + certificados oficiais da ICP-Brasil (ITI).
URL_ICP = "acraiz.icpbrasil.gov.br/credenciadas/CertificadosAC-ICP-Brasil/"
_SSL_PREPARADO: str | None = None


def _baixar_cadeia_icp(pasta) -> int:
    ultimo = None
    for esquema in ("https://", "http://"):
        try:
            zip_ = requests.get(esquema + URL_ICP + "ACcompactado.zip", timeout=60)
            zip_.raise_for_status()
            if esquema == "http://":  # sem TLS: confere o hash SHA-512 publicado pelo ITI
                hashes_txt = requests.get(esquema + URL_ICP + "hashsha512.txt", timeout=30).text.lower()
                if hashlib.sha512(zip_.content).hexdigest() not in hashes_txt:
                    raise RuntimeError("hash SHA-512 do pacote do ITI não confere")
            n = 0
            with zipfile.ZipFile(io.BytesIO(zip_.content)) as z:
                for nome in z.namelist():
                    if nome.lower().endswith((".crt", ".cer", ".pem")):
                        (pasta / Path(nome).name).write_bytes(z.read(nome))
                        n += 1
            return n
        except Exception as e:
            ultimo = e
    raise RuntimeError(f"não foi possível baixar a cadeia ICP-Brasil do ITI: {ultimo}")


def _para_pem(dado: bytes) -> list[str]:
    if b"-----BEGIN CERTIFICATE-----" in dado:
        txt = dado.decode("ascii", errors="ignore")
        return [b + "-----END CERTIFICATE-----\n" for b in txt.split("-----END CERTIFICATE-----") if "BEGIN" in b]
    try:
        return [ssl.DER_cert_to_PEM_cert(dado)]
    except Exception:
        return []


def preparar_ssl(pasta_config) -> str:
    """Devolve o caminho do bundle PEM a usar em 'verify' (gerado uma vez e reaproveitado por 30 dias)."""
    global _SSL_PREPARADO
    if _SSL_PREPARADO:
        return _SSL_PREPARADO
    pasta_config = Path(pasta_config)
    destino = pasta_config / "cadeia_ssl.pem"
    if destino.exists() and time.time() - destino.stat().st_mtime < 30 * 86400:
        _SSL_PREPARADO = str(destino)
        return _SSL_PREPARADO
    pasta_icp = pasta_config / "icp_brasil"
    pasta_icp.mkdir(exist_ok=True)
    if not any(pasta_icp.iterdir()):
        print("  Baixando cadeia de certificados ICP-Brasil (ITI) para validar os servidores da SEFAZ…", flush=True)
        try:
            print(f"  [OK] {_baixar_cadeia_icp(pasta_icp)} certificados ICP-Brasil salvos em {pasta_icp}", flush=True)
        except RuntimeError as e:
            print(f"  [AVISO] {e}\n    Baixe manualmente http://{URL_ICP}ACcompactado.zip e extraia em {pasta_icp}",
                  flush=True)
    pems = [Path(certifi.where()).read_text(encoding="utf-8")]
    if hasattr(ssl, "enum_certificates"):
        for loja in ("ROOT", "CA"):
            try:
                for cert, enc, _uso in ssl.enum_certificates(loja):
                    if enc == "x509_asn":
                        pems.append(ssl.DER_cert_to_PEM_cert(cert))
            except OSError:
                continue
    for arq in pasta_icp.rglob("*"):
        if arq.is_file():
            pems += _para_pem(arq.read_bytes())
    destino.write_text("\n".join(pems), encoding="utf-8")
    _SSL_PREPARADO = str(destino)
    return _SSL_PREPARADO
