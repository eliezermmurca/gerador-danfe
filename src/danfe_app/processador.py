"""Orquestra um lote: lê chaves, obtém XMLs, gera DANFEs e registra erros."""
from __future__ import annotations

import csv
import getpass
import json
import logging
import os
import shutil
import sys
import time
import traceback
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lxml import etree

from .certificado import Certificado, ErroCertificado
from .chaves import UF_CODIGOS, UF_SIGLA_PARA_CODIGO, EntradaChave, info_chave, ler_entradas, validar_chave
from . import danfe_pdf, fonte_sql, segredo
from .danfe_pdf import ErroXml, gerar_danfe, gerar_danfe_unico
from .sefaz import (URLS_CONSULTA_PROTOCOLO, ClienteSefaz, ErroSefaz, consultar_protocolo, montar_proc_nfe,
                    preparar_ssl)

RAIZ = Path(__file__).resolve().parents[2]
PASTAS = {
    "config": RAIZ / "config",
    "certificados": RAIZ / "certificados",
    "entrada": RAIZ / "entrada",
    "xml_manual": RAIZ / "xml_manual",
    "acervo": RAIZ / "acervo_xml",
    "saida": RAIZ / "saida",
}

# código -> (pode reprocessar sem mexer na chave?, o que fazer)
ACOES = {
    "CHAVE_INVALIDA": (False, "Corrija a chave no arquivo de entrada e rode novamente."),
    "MODELO_NAO_SUPORTADO": (False, "Este gerador cobre apenas NF-e modelo 55."),
    "SEM_CERTIFICADO": (True, "Configure um certificado em config/config.json ou coloque o XML da nota em xml_manual/."),
    "CERTIFICADO": (True, "Verifique arquivo, senha e validade do certificado em config/config.json."),
    "SEM_PROTOCOLO": (True, "O XML não tem protocolo e não foi possível obtê-lo na SEFAZ. Verifique certificado/"
                            "conexão e reprocesse."),
    "NAO_AUTORIZADA": (True, "A SEFAZ não encontrou essa NF-e autorizada (cStat 217). Confira se a nota foi "
                             "realmente transmitida/autorizada e reprocesse."),
    "DENEGADA": (False, "NF-e com uso denegado: não deve ser impressa como documento válido."),
    "SQL": (True, "Falha ao buscar o XML no SQL Server: confira 'sql_server' no config.json, rede e permissões."),
    "NOTA_PROPRIA": (True, "Nota emitida pela sua própria empresa: a SEFAZ não devolve ao emitente. Copie o XML "
                           "autorizado do seu emissor/ERP para xml_manual/ (ou configure 'pastas_xml') e reprocesse."),
    "NAO_LOCALIZADA": (True, "A SEFAZ não entregou a nota ao CNPJ do(s) certificado(s). Confirme que a empresa é "
                             "emitente, destinatária ou transportadora da NF-e (notas recém-emitidas podem demorar "
                             "alguns minutos). Alternativa: coloque o XML em xml_manual/."),
    "APENAS_RESUMO": (True, "A empresa é destinatária e a SEFAZ só liberou o resumo. Registre a Ciência da Operação "
                            "(ative 'manifestar_ciencia_automaticamente' ou manifeste pelo seu ERP/portal), "
                            "aguarde alguns minutos e reprocesse."),
    "CONSUMO_INDEVIDO": (True, "A SEFAZ bloqueou temporariamente as consultas deste CNPJ (rejeição 656). "
                               "Aguarde cerca de 1 hora e reprocesse."),
    "NAO_CONSULTADA": (True, "Não foi consultada porque a SEFAZ bloqueou o CNPJ nesta execução. Reprocesse mais tarde."),
    "FALHA_CONEXAO": (True, "Verifique internet, proxy ou firewall e reprocesse."),
    "FALHA_SSL": (True, "Falha de SSL. Confira o certificado ou ajuste 'verificar_ssl' (veja o README)."),
    "CERTIFICADO_RECUSADO": (True, "A SEFAZ recusou o certificado. Confira se é A1 ICP-Brasil, válido e do CNPJ correto."),
    "FALHA_SEFAZ": (True, "Erro no serviço da SEFAZ. Reprocesse mais tarde."),
    "RESPOSTA_INVALIDA": (True, "Resposta inesperada da SEFAZ. Reprocesse mais tarde."),
    "REJEICAO_SEFAZ": (True, "Veja o motivo da rejeição; após corrigir, reprocesse."),
    "XML_INVALIDO": (False, "O XML obtido não pôde ser lido; confira o arquivo salvo em xml/."),
    "ERRO_PDF": (True, "Falha ao montar o PDF; veja o log do lote e reporte se persistir."),
}

COR = {"ok": "\033[32m", "aviso": "\033[33m", "erro": "\033[31m", "neg": "\033[1m", "fim": "\033[0m"}
if os.name == "nt" and sys.stdout is not None and sys.stdout.isatty():
    os.system("")  # habilita cores ANSI no terminal do Windows


@dataclass
class Resultado:
    chave: str
    origem: str
    status: str  # OK | AVISO | ERRO
    codigo: str = ""
    mensagem: str = ""
    fonte_xml: str = ""
    arquivo_pdf: str = ""


class ErroProcesso(Exception):
    def __init__(self, codigo, mensagem):
        super().__init__(mensagem)
        self.codigo, self.mensagem = codigo, mensagem
def arquivo_config() -> Path:
    """Caminho do config.json; na primeira execução é criado a partir do config.exemplo.json."""
    arq = PASTAS["config"] / "config.json"
    exemplo = PASTAS["config"] / "config.exemplo.json"
    if not arq.exists() and exemplo.exists():
        shutil.copyfile(exemplo, arq)
    return arq


def carregar_config() -> dict:
    arq = arquivo_config()
    if not arq.exists():
        raise SystemExit(f"Arquivo de configuração não encontrado: {arq}")
    with open(arq, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("ambiente", "producao")
    cfg.setdefault("uf_autor", "SP")
    cfg.setdefault("certificados", [])
    cfg.setdefault("manifestar_ciencia_automaticamente", False)
    cfg.setdefault("intervalo_entre_consultas_segundos", 2)
    cfg.setdefault("tentativas_conexao", 3)
    cfg.setdefault("timeout_segundos", 30)
    cfg.setdefault("verificar_ssl", True)
    cfg.setdefault("pastas_xml", [])
    cfg.setdefault("consultar_protocolo", True)
    cfg.setdefault("gerar_sem_protocolo", False)
    cfg.setdefault("urls_consulta_protocolo", {})
    cfg.setdefault("sql_server", {"ativo": False})
    cfg.setdefault("remover_do_texto_produto", None)
    cfg.setdefault("intervalo_consulta_protocolo_segundos", 1.0)
    cfg.setdefault("incluir_canceladas_no_pdf_da_carga", False)
    cfg.setdefault("logo_emitente", "")
    cfg.setdefault("mover_entradas_processadas", True)
    cfg.setdefault("nome_arquivo_pdf", "DANFE_{numero}_{chave}.pdf")
    if cfg["ambiente"] not in ("producao", "homologacao"):
        raise SystemExit("config.json: 'ambiente' deve ser 'producao' ou 'homologacao'")
    return cfg


def _resolver(caminho: str) -> Path:
    p = Path(caminho)
    return p if p.is_absolute() else RAIZ / p
def _logger(pasta_lote: Path) -> logging.Logger:
    log = logging.getLogger("danfe")
    log.handlers.clear()
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(pasta_lote / "log.txt", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%d/%m/%Y %H:%M:%S"))
    log.addHandler(fh)
    return log


def _print(msg, cor=None):
    print(f"{COR[cor]}{msg}{COR['fim']}" if cor else msg, flush=True)
def _chave_do_xml(xml: bytes) -> str | None:
    try:
        raiz = etree.fromstring(xml)
    except etree.XMLSyntaxError:
        return None
    inf = raiz.xpath("//*[local-name()='infNFe']")
    if inf and (inf[0].get("Id") or "").startswith("NFe"):
        return inf[0].get("Id")[3:]
    ch = raiz.xpath("//*[local-name()='chNFe']")
    return ch[0].text.strip() if ch else None


def indexar_xml_manual(pastas_extras: list[str] | None = None) -> dict[str, Path]:
    """Indexa por chave os XMLs de xml_manual/ e das pastas extras (ex.: pasta de XMLs do ERP)."""
    indice = {}
    pastas = [PASTAS["xml_manual"]] + [_resolver(p) for p in (pastas_extras or [])]
    arquivos = []
    for pasta in pastas:
        if pasta.is_dir():
            arquivos += list(pasta.rglob("*.xml")) + list(pasta.rglob("*.XML"))
        else:
            _print(f"  [AVISO] Pasta de XML não encontrada: {pasta}", "aviso")
    for arq in arquivos:
        try:
            ch = _chave_do_xml(arq.read_bytes())
        except OSError:
            continue
        if ch and (ch not in indice or b"protNFe" in arq.read_bytes()):
            indice[ch] = arq  # prefere o XML com protocolo de autorização
    return indice


def _eh_nfe_completa(xml: bytes) -> bool:
    return b"infNFe" in xml


class BuscadorXml:
    """Procura o XML: acervo local -> xml_manual -> SEFAZ (com cada certificado)."""

    def __init__(self, cfg: dict, log: logging.Logger, pilha: ExitStack):
        self.cfg, self.log, self.pilha = cfg, log, pilha
        self.manual = indexar_xml_manual(cfg["pastas_xml"])
        self._clientes: list[ClienteSefaz] | None = None
        self._erro_cert: ErroProcesso | None = None
        self._ultima_consulta = 0.0
        self._sql: fonte_sql.ConexaoSql | None = None
        self._ultimo_protocolo = 0.0
        self._protocolo_bloqueado = False
        self._erro_sql: str | None = None

    # certificados são carregados só se alguma chave precisar da SEFAZ
    def clientes(self) -> list[ClienteSefaz]:
        if self._clientes is not None:
            return self._clientes
        self._clientes = []
        lista = self.cfg["certificados"]
        if not lista:
            self._erro_cert = ErroProcesso("SEM_CERTIFICADO", "nenhum certificado configurado")
            return self._clientes
        problemas = []
        for item in lista:
            arq = _resolver(item.get("arquivo", ""))
            if not arq.is_file():
                msg = f"arquivo não encontrado: {arq}"
                problemas.append(msg)
                _print(f"  [ERRO] Certificado: {msg}", "erro")
                continue
            try:
                senha = segredo.obter_senha(item, PASTAS["config"] / "senha_certificado.dat")
            except RuntimeError as e:
                _print(f"  [AVISO] Senha gravada: {e}", "aviso")
                senha = ""
            if not senha and sys.stdin is not None and sys.stdin.isatty():
                senha = getpass.getpass(f"Senha do certificado {arq.name}: ")
            try:
                cert = Certificado(arq, senha)
            except ErroCertificado as e:
                problemas.append(str(e))
                self.log.error("Certificado: %s", e)
                _print(f"  [ERRO] Certificado: {e}", "erro")
                continue
            if cert.vencido:
                msg = f"{arq.name} venceu em {cert.validade:%d/%m/%Y}"
                problemas.append(msg)
                _print(f"  [ERRO] Certificado: {msg}", "erro")
                continue
            cnpj = "".join(c for c in (item.get("cnpj") or cert.cnpj or "") if c.isdigit())
            if len(cnpj) != 14:
                msg = f"não foi possível obter o CNPJ de {arq.name}; informe 'cnpj' no config.json"
                problemas.append(msg)
                _print(f"  [ERRO] Certificado: {msg}", "erro")
                continue
            uf = (item.get("uf") or self.cfg["uf_autor"]).upper()
            if uf not in UF_SIGLA_PARA_CODIGO:
                problemas.append(f"UF inválida '{uf}' para {arq.name}")
                continue
            self.pilha.enter_context(cert)
            self._clientes.append(ClienteSefaz(
                cert, ambiente=self.cfg["ambiente"], uf_autor_codigo=UF_SIGLA_PARA_CODIGO[uf], cnpj=cnpj,
                timeout=self.cfg["timeout_segundos"], tentativas=self.cfg["tentativas_conexao"],
                verificar_ssl=self._ssl(), urls=self.cfg.get("urls")))
            self.log.info("Certificado carregado: %s", cert.descricao())
            _print(f"  [OK] Certificado: {cert.descricao()}", "ok")
        if not self._clientes:
            self._erro_cert = ErroProcesso("CERTIFICADO", "; ".join(problemas))
        return self._clientes

    def _ssl(self):
        v = self.cfg["verificar_ssl"]
        if isinstance(v, str):
            return str(_resolver(v))
        if v:
            return preparar_ssl(PASTAS["config"])
        return False

    def _esperar_intervalo(self):
        falta = self.cfg["intervalo_entre_consultas_segundos"] - (time.time() - self._ultima_consulta)
        if falta > 0:
            time.sleep(falta)
        self._ultima_consulta = time.time()

    def buscar(self, chave: str) -> tuple[bytes, str, list[str]]:
        """Retorna (xml, fonte, avisos)."""
        acervo = PASTAS["acervo"] / f"{chave}-procNFe.xml"
        if acervo.exists():
            cancelada = (PASTAS["acervo"] / f"{chave}.CANCELADA").exists()
            return acervo.read_bytes(), "acervo local", ["NF-e CANCELADA"] if cancelada else []
        xml, fonte = None, ""
        if chave in self.manual:
            xml, fonte = self.manual[chave].read_bytes(), f"XML local ({self.manual[chave].name})"
        elif self.cfg["sql_server"].get("ativo"):
            xml = self._buscar_sql(chave)
            fonte = "SQL Server"
        if xml is None:
            return self._buscar_sefaz(chave)
        if b"protNFe" in xml:
            return xml, fonte, []
        return self._completar_protocolo(chave, xml, fonte)
    def _buscar_sql(self, chave: str) -> bytes | None:
        if self._erro_sql:
            raise ErroProcesso("SQL", self._erro_sql)
        if self._sql is None:
            try:
                self._sql = fonte_sql.ConexaoSql(self.cfg["sql_server"])
                self.pilha.callback(self._sql.fechar)
                _print("  [OK] Conectado ao SQL Server", "ok")
            except Exception as e:
                self._erro_sql = f"não foi possível conectar: {e}"
                _print(f"  [ERRO] SQL Server: {self._erro_sql}", "erro")
                raise ErroProcesso("SQL", self._erro_sql) from e
        try:
            return self._sql.buscar(chave)
        except Exception as e:
            raise ErroProcesso("SQL", f"erro na consulta: {e}") from e
    def _completar_protocolo(self, chave: str, xml: bytes, fonte: str) -> tuple[bytes, str, list[str]]:
        def sem_protocolo(motivo: str):
            if self.cfg["gerar_sem_protocolo"]:
                return xml, fonte, [f"XML sem protocolo ({motivo})"]
            raise ErroProcesso("SEM_PROTOCOLO", motivo)

        if not self.cfg["consultar_protocolo"]:
            return sem_protocolo("consulta de protocolo desligada no config")
        if self._protocolo_bloqueado:
            raise ErroProcesso("NAO_CONSULTADA", "consultas de protocolo suspensas: SEFAZ indicou consumo "
                                                 "indevido (656) nesta execução")
        clientes = self.clientes()
        if not clientes:
            return sem_protocolo(f"sem certificado para consultar: {self._erro_cert.mensagem}")
        uf = UF_CODIGOS.get(chave[:2], "")
        url = self.cfg["urls_consulta_protocolo"].get(uf)
        if not url:
            par = URLS_CONSULTA_PROTOCOLO.get(uf)
            if not par:
                return sem_protocolo(f"sem endereço de consulta para a UF {uf}")
            url = par[0] if self.cfg["ambiente"] == "producao" else par[1]
        cli = next((c for c in clientes if c.cnpj == info_chave(chave)["cnpj_emitente"]), clientes[0])
        falta = self.cfg["intervalo_consulta_protocolo_segundos"] - (time.time() - self._ultimo_protocolo)
        if falta > 0:
            time.sleep(falta)
        try:
            ret = consultar_protocolo(cli.sessao, url, chave, cli.tp_amb,
                                      self.cfg["timeout_segundos"], self.cfg["tentativas_conexao"])
        except ErroSefaz as e:
            return sem_protocolo(f"{e.codigo}: {e.mensagem}")
        finally:
            self._ultimo_protocolo = time.time()
        self.log.info("ConsultaProtocolo %s -> %s %s", chave, ret.cstat, ret.motivo)
        if ret.cstat == "656":
            self._protocolo_bloqueado = True
            _print(f"  [AVISO] SEFAZ-{uf} indicou consumo indevido (656). Consultas de protocolo suspensas nesta execução.",
                   "aviso")
            raise ErroProcesso("CONSUMO_INDEVIDO", f"cStat 656 - {ret.motivo}")
        if ret.cstat in ("110", "301", "302", "303"):
            raise ErroProcesso("DENEGADA", f"cStat {ret.cstat} - {ret.motivo}")
        if ret.cstat == "217":
            raise ErroProcesso("NAO_AUTORIZADA", f"cStat 217 - {ret.motivo}")
        if ret.prot_nfe is None or ret.cstat not in ("100", "150", "101", "151"):
            raise ErroProcesso("REJEICAO_SEFAZ", f"consulta de protocolo: cStat {ret.cstat} - {ret.motivo}")
        proc = montar_proc_nfe(xml, ret.prot_nfe)
        self._arquivar(chave, proc)
        if ret.cancelada:
            (PASTAS["acervo"] / f"{chave}.CANCELADA").write_text(f"cStat {ret.cstat} - {ret.motivo}", encoding="utf-8")
        avisos = ["NF-e CANCELADA"] if ret.cancelada else []
        return proc, f"{fonte} + protocolo SEFAZ-{uf}", avisos

    def _buscar_sefaz(self, chave: str) -> tuple[bytes, str, list[str]]:
        clientes = self.clientes()
        if not clientes:
            raise self._erro_cert
        disponiveis = [c for c in clientes if not c.bloqueado]
        if not disponiveis:
            raise ErroProcesso("NAO_CONSULTADA", "consultas suspensas: SEFAZ bloqueou o(s) CNPJ(s) nesta execução")
        emit = info_chave(chave)["cnpj_emitente"]
        # a Distribuição DF-e não entrega ao emitente as próprias notas (rejeição 641):
        # não gasta consulta com o certificado do emitente
        disponiveis = [c for c in disponiveis if c.cnpj != emit]
        if not disponiveis:
            raise ErroProcesso("NOTA_PROPRIA", f"a nota foi emitida pelo CNPJ {emit}, dono do certificado; "
                                               "a SEFAZ não devolve ao emitente as notas que ele emitiu (641)")

        resumo_de: ClienteSefaz | None = None
        ultimo: ErroProcesso | None = None
        for cli in disponiveis:
            self._esperar_intervalo()
            try:
                ret = cli.consultar_chave(chave)
            except ErroSefaz as e:
                ultimo = ErroProcesso(e.codigo, e.mensagem)
                continue
            self.log.info("DistDFe %s CNPJ %s -> %s %s", chave, cli.cnpj, ret.cstat, ret.motivo)
            if ret.cstat == "138":
                xml_completo = next((x for s, x in ret.documentos if s.startswith("procNFe")), None)
                if xml_completo:
                    cancelada = any(b"<tpEvento>110111</tpEvento>" in x for _, x in ret.documentos)
                    self._arquivar(chave, xml_completo)
                    if cancelada:
                        (PASTAS["acervo"] / f"{chave}.CANCELADA").write_text("cancelada", encoding="utf-8")
                    return xml_completo, f"SEFAZ (CNPJ {cli.cnpj})", ["NF-e CANCELADA"] if cancelada else []
                if any(s.startswith("resNFe") for s, _ in ret.documentos):
                    resumo_de = cli
                    continue
                ultimo = ErroProcesso("NAO_LOCALIZADA", f"SEFAZ retornou apenas eventos: {ret.motivo}")
            elif ret.cstat == "641":
                ultimo = ErroProcesso("NOTA_PROPRIA", f"cStat 641 - {ret.motivo}")
            elif ret.cstat == "137":
                ultimo = ErroProcesso("NAO_LOCALIZADA", f"cStat 137 - {ret.motivo}")
            elif ret.cstat == "656":
                ultimo = ErroProcesso("CONSUMO_INDEVIDO", f"cStat 656 - {ret.motivo}")
                _print(f"  [AVISO] SEFAZ bloqueou consultas do CNPJ {cli.cnpj} (656 - consumo indevido)", "aviso")
            else:
                ultimo = ErroProcesso("REJEICAO_SEFAZ", f"cStat {ret.cstat} - {ret.motivo}")

        if resumo_de is not None:
            return self._via_ciencia(chave, resumo_de)
        raise ultimo or ErroProcesso("NAO_LOCALIZADA", "nota não localizada")

    def _via_ciencia(self, chave: str, cli: ClienteSefaz):
        if not self.cfg["manifestar_ciencia_automaticamente"]:
            raise ErroProcesso("APENAS_RESUMO", "SEFAZ liberou só o resumo (falta Ciência da Operação)")
        try:
            cstat, motivo = cli.manifestar_ciencia(chave)
        except ErroSefaz as e:
            raise ErroProcesso("APENAS_RESUMO", f"falha ao registrar ciência: {e.mensagem}") from e
        self.log.info("Ciência %s CNPJ %s -> %s %s", chave, cli.cnpj, cstat, motivo)
        if cstat not in ("135", "136", "573"):  # registrado / vinculado / já existente
            raise ErroProcesso("APENAS_RESUMO", f"ciência não registrada: cStat {cstat} - {motivo}")
        _print(f"    ciência da operação registrada ({cstat}); aguardando liberação do XML…")
        for espera in (5, 15):
            time.sleep(espera)
            self._esperar_intervalo()
            try:
                ret = cli.consultar_chave(chave)
            except ErroSefaz:
                continue
            xml = next((x for s, x in ret.documentos if s.startswith("procNFe")), None)
            if xml:
                self._arquivar(chave, xml)
                return xml, f"SEFAZ (CNPJ {cli.cnpj}, após ciência)", []
            if ret.cstat == "656":
                break
        raise ErroProcesso("APENAS_RESUMO", "ciência registrada; o XML completo ainda não foi liberado. "
                                            "Reprocesse em alguns minutos.")

    @staticmethod
    def _arquivar(chave, xml):
        PASTAS["acervo"].mkdir(exist_ok=True)
        (PASTAS["acervo"] / f"{chave}-procNFe.xml").write_bytes(xml)
def arquivos_de_entrada() -> list[Path]:
    return sorted(p for p in PASTAS["entrada"].iterdir()
                  if p.is_file() and p.suffix.lower() in (".txt", ".csv"))


def ultimo_lote() -> Path | None:
    lotes = sorted(p for p in PASTAS["saida"].glob("lote_*") if p.is_dir())
    return lotes[-1] if lotes else None


def _processar(cfg: dict, entradas: list[EntradaChave], lote_id: str, origem_txt: str,
               pilha: ExitStack, buscador: "BuscadorXml | None" = None
               ) -> tuple[Path, list[Resultado], dict[str, tuple[bytes, list[str]]]]:
    """Gera os DANFEs de um lote. Retorna (pasta, resultados, {chave: (xml, avisos)} dos gerados)."""
    pasta = PASTAS["saida"] / lote_id
    (pasta / "pdf").mkdir(parents=True)
    (pasta / "xml").mkdir()
    log = _logger(pasta)
    log.info("Lote %s | ambiente %s | entradas: %s", lote_id, cfg["ambiente"], origem_txt)
    gerados: dict[str, tuple[bytes, list[str]]] = {}

    _print(f"\n{COR['neg']}=== {lote_id} ({cfg['ambiente']}) ==={COR['fim']}")
    _print(f"Arquivos: {origem_txt}")
    _print(f"Chaves encontradas: {len(entradas)}\n")

    resultados: list[Resultado] = []
    logo = _resolver(cfg["logo_emitente"]) if cfg["logo_emitente"] else None

    buscador = buscador or BuscadorXml(cfg, log, pilha)
    buscador.log = log
    for i, ent in enumerate(entradas, start=1):
        prefixo = f"[{i}/{len(entradas)}] {ent.chave}"
        res = Resultado(ent.chave, ent.origem, "ERRO")
        try:
            if ent.erro:
                codigo = "MODELO_NAO_SUPORTADO" if "modelo" in ent.erro or "NFC-e" in ent.erro \
                    else "CHAVE_INVALIDA"
                raise ErroProcesso(codigo, ent.erro)
            xml, fonte, avisos = buscador.buscar(ent.chave)
            res.fonte_xml = fonte
            (pasta / "xml" / f"{ent.chave}-procNFe.xml").write_bytes(xml)
            if not _eh_nfe_completa(xml):
                raise ErroProcesso("XML_INVALIDO", "o XML não contém a NF-e completa")
            info = info_chave(ent.chave)
            nome = cfg["nome_arquivo_pdf"].format(chave=ent.chave, numero=int(info["numero"]),
                                                  serie=int(info["serie"]), cnpj=info["cnpj_emitente"])
            destino = pasta / "pdf" / nome
            marca = "NF-e CANCELADA" if "NF-e CANCELADA" in avisos else None
            try:
                nf = gerar_danfe(xml, destino, logo, marca)
            except ErroXml as e:
                raise ErroProcesso("XML_INVALIDO", str(e)) from e
            except Exception as e:
                log.error("Falha no PDF %s\n%s", ent.chave, traceback.format_exc())
                raise ErroProcesso("ERRO_PDF", f"{type(e).__name__}: {e}") from e
            if nf.chave and nf.chave != ent.chave:
                avisos.append(f"chave do XML ({nf.chave}) difere da solicitada")
            if nf.homologacao:
                avisos.append("NF-e de homologação (sem valor fiscal)")
            if not nf.protocolo:
                avisos.append("XML sem protocolo de autorização")
            res.arquivo_pdf = str(destino.relative_to(pasta))
            gerados[ent.chave] = (xml, list(avisos))
            if avisos:
                res.status, res.codigo, res.mensagem = "AVISO", "AVISO", "; ".join(avisos)
                _print(f"{prefixo}  [AVISO] PDF gerado com aviso: {res.mensagem}", "aviso")
            else:
                res.status, res.mensagem = "OK", f"NF {nf.d['nNF']} - {nf.d['emit_nome']}"
                _print(f"{prefixo}  [OK] {res.mensagem}  [{fonte}]", "ok")
        except ErroProcesso as e:
            res.codigo, res.mensagem = e.codigo, e.mensagem
            _print(f"{prefixo}  [ERRO] {e.codigo}: {e.mensagem}", "erro")
        except Exception as e:  # nenhum erro interrompe o lote
            res.codigo, res.mensagem = "ERRO_PDF", f"{type(e).__name__}: {e}"
            log.error("Erro inesperado %s\n%s", ent.chave, traceback.format_exc())
            _print(f"{prefixo}  [ERRO] erro inesperado: {e}", "erro")
        log.info("%s | %s | %s | %s | %s", ent.chave, res.status, res.codigo, res.mensagem, res.fonte_xml)
        resultados.append(res)

    _gravar_relatorios(pasta, resultados)
    if not any((pasta / "pdf").iterdir()):
        (pasta / "pdf").rmdir()
    return pasta, resultados, gerados


def executar(reprocessar: Path | None = None) -> int:
    cfg = carregar_config()
    for p in PASTAS.values():
        p.mkdir(exist_ok=True)

    if reprocessar:
        arq_rep = reprocessar / "REPROCESSAR.txt" if reprocessar.is_dir() else reprocessar
        if not arq_rep.exists():
            _print(f"Nada para reprocessar: {arq_rep} não existe.", "aviso")
            return 0
        arquivos = [arq_rep]
    else:
        arquivos = arquivos_de_entrada()
    if not arquivos:
        _print(f"Nenhum .txt/.csv encontrado em {PASTAS['entrada']}", "aviso")
        return 0

    # XMLs colados/exportados do SQL (uma NF-e por linha ou célula) dentro dos arquivos
    textos: dict[Path, str] = {}
    iniciais: list[EntradaChave] = []
    for arq in arquivos:
        bruto = arq.read_bytes()
        if b"<infNFe" not in bruto:
            continue
        achados, restante, invalidos = fonte_sql.extrair_xmls(bruto)
        fonte_sql.salvar_importados(achados, PASTAS["xml_manual"] / "importados")
        for chave, _ in achados:
            iniciais.append(EntradaChave(chave, f"{arq.name}:XML", validar_chave(chave)))
        textos[arq] = restante.decode("utf-8", errors="replace")
        _print(f"{arq.name}: {len(achados)} XML(s) de NF-e importado(s)")
        if invalidos:
            _print(f"  [AVISO] {arq.name}: {invalidos} bloco(s) de XML incompleto(s)/inválido(s) ignorado(s) "
                   "(veja se a exportação do SELECT cortou o texto)", "aviso")
    entradas: list[EntradaChave] = ler_entradas(arquivos, textos, iniciais)
    if cfg["remover_do_texto_produto"] is not None:
        danfe_pdf.FILTROS_INF_AD_PROD[:] = cfg["remover_do_texto_produto"]
    if not entradas:
        _print("Nenhuma chave de acesso encontrada nos arquivos de entrada.", "aviso")
        return 0

    lote_id = datetime.now().strftime("lote_%Y%m%d_%H%M%S")
    origem_txt = "reprocessamento de " + str(reprocessar) if reprocessar else ", ".join(a.name for a in arquivos)
    with ExitStack() as pilha:
        pasta, resultados, _ = _processar(cfg, entradas, lote_id, origem_txt, pilha)
    if cfg["mover_entradas_processadas"] and not reprocessar:
        destino = PASTAS["entrada"] / "processados" / lote_id
        destino.mkdir(parents=True, exist_ok=True)
        for a in arquivos:
            shutil.move(str(a), destino / a.name)
    return _resumo(pasta, resultados)


def executar_cargas(cargas: list[str], relatorio: list | None = None) -> int:
    """Busca no SQL Server as NF-e de cada carga, gera os DANFEs e junta tudo em CARGA_<nº>.pdf."""
    cfg = carregar_config()
    for p in PASTAS.values():
        p.mkdir(exist_ok=True)
    if cfg["remover_do_texto_produto"] is not None:
        danfe_pdf.FILTROS_INF_AD_PROD[:] = cfg["remover_do_texto_produto"]
    if not cfg["sql_server"].get("servidor") or "NOME_DO" in cfg["sql_server"].get("servidor", ""):
        _print("Configure o bloco 'sql_server' no config/config.json (servidor, banco, consulta_carga).", "erro")
        return 1
    pasta_cargas = PASTAS["saida"] / "cargas"
    pasta_cargas.mkdir(exist_ok=True)
    logo = _resolver(cfg["logo_emitente"]) if cfg["logo_emitente"] else None
    codigo_saida = 0

    with ExitStack() as pilha:
        try:
            sql = fonte_sql.ConexaoSql(cfg["sql_server"])
        except Exception as e:
            _print(f"[ERRO] Não foi possível conectar ao SQL Server: {e}", "erro")
            if relatorio is not None:
                relatorio.append({"carga": " ".join(cargas), "pdf": None, "notas": 0,
                                  "falha": f"sem conexão com o banco: {e}"})
            return 1
        pilha.callback(sql.fechar)
        _print("[OK] Conectado ao SQL Server", "ok")
        buscador = None

        for carga in cargas:
            carga = str(carga).strip()
            if not carga:
                continue
            _print(f"\n{COR['neg']}##### CARGA {carga} #####{COR['fim']}")
            try:
                notas, linhas, invalidas = sql.buscar_carga(carga)
            except Exception as e:
                _print(f"[ERRO] Erro ao consultar a carga {carga}: {e}", "erro")
                if relatorio is not None:
                    relatorio.append({"carga": carga, "pdf": None, "notas": 0, "falha": f"erro na consulta: {e}"})
                codigo_saida = 1
                continue
            if invalidas:
                _print(f"  [AVISO] {invalidas} linha(s) da consulta sem XML válido de NF-e", "aviso")
            if not notas:
                _print(f"  Nenhuma NF-e encontrada para a carga {carga} ({linhas} linha(s) retornada(s)).", "aviso")
                if relatorio is not None:
                    relatorio.append({"carga": carga, "pdf": None, "notas": 0, "falha": "nenhuma NF-e encontrada"})
                codigo_saida = 1
                continue
            fonte_sql.salvar_importados(notas, PASTAS["xml_manual"] / "importados")
            entradas = [EntradaChave(ch, f"carga {carga}", validar_chave(ch)) for ch, _ in notas]
            lote_id = datetime.now().strftime(f"carga_{carga}_%Y%m%d_%H%M%S")
            if buscador is None:
                buscador = BuscadorXml(cfg, logging.getLogger("danfe"), pilha)
            for ch, xml in notas:  # garante que a cópia recém-lida do banco seja usada
                buscador.manual[ch] = PASTAS["xml_manual"] / "importados" / f"{ch}-nfe.xml"
            pasta, resultados, gerados = _processar(cfg, entradas, lote_id, f"SQL Server, carga {carga}",
                                                    pilha, buscador)
            codigo_saida |= _juntar_carga(cfg, carga, notas, resultados, gerados, pasta, pasta_cargas, logo,
                                          relatorio)
    return codigo_saida


def _juntar_carga(cfg, carga, notas, resultados, gerados, pasta, pasta_cargas, logo, relatorio=None) -> int:
    canceladas = [ch for ch, (_, av) in gerados.items() if "NF-e CANCELADA" in av]
    itens = []
    for ch, _ in notas:  # mantém a ordem do SELECT
        if ch not in gerados:
            continue
        xml, avisos = gerados[ch]
        cancelada = "NF-e CANCELADA" in avisos
        if cancelada and not cfg["incluir_canceladas_no_pdf_da_carga"]:
            continue
        itens.append((xml, "NF-e CANCELADA" if cancelada else None))
    erros = [r for r in resultados if r.status == "ERRO"]
    for antigo in pasta_cargas.glob(f"CARGA_{carga}*.pdf"):
        try:
            antigo.unlink()
        except OSError:
            pass
    info = {"carga": carga, "pdf": None, "notas": len(notas), "incluidas": len(itens), "folhas": 0,
            "canceladas": len(canceladas), "pasta": str(pasta),
            "erros": [(r.chave, r.codigo, r.mensagem) for r in erros]}
    if relatorio is not None:
        relatorio.append(info)
    if not itens:
        _print(f"  [ERRO] Nenhum DANFE gerado para a carga {carga}; PDF da carga não criado.", "erro")
        return _resumo(pasta, resultados) or 1
    sufixo = "_INCOMPLETA" if erros else ""
    destino = pasta_cargas / f"CARGA_{carga}{sufixo}.pdf"
    folhas = gerar_danfe_unico(itens, destino, f"DANFEs - Carga {carga}", logo)
    shutil.copy2(destino, pasta / destino.name)
    info["pdf"], info["folhas"] = str(destino), folhas
    rc = _resumo(pasta, resultados)
    if erros:
        _print(f"  [AVISO] PDF DA CARGA INCOMPLETO: {len(itens)} de {len(notas)} nota(s). Corrija os erros e "
               f"rode a carga {carga} de novo (as notas já prontas não são consultadas novamente).", "aviso")
    if canceladas and not cfg["incluir_canceladas_no_pdf_da_carga"]:
        _print(f"  [AVISO] {len(canceladas)} nota(s) cancelada(s) ficaram fora do PDF da carga.", "aviso")
    _print(f"{COR['neg']}  PDF PARA IMPRESSÃO: {destino}  ({len(itens)} nota(s), {folhas} folha(s)){COR['fim']}",
           "ok" if not erros else "aviso")
    return rc


def _gravar_relatorios(pasta: Path, resultados: list[Resultado]):
    with open(pasta / "relatorio.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["chave", "status", "codigo", "mensagem", "origem", "fonte_xml", "arquivo_pdf"])
        for r in resultados:
            w.writerow([r.chave, r.status, r.codigo, r.mensagem, r.origem, r.fonte_xml, r.arquivo_pdf])

    erros = [r for r in resultados if r.status == "ERRO"]
    if not erros:
        return
    with open(pasta / "ERROS.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["chave", "codigo", "motivo", "o_que_fazer", "reprocessavel", "origem"])
        for r in erros:
            rep, acao = ACOES.get(r.codigo, (True, ""))
            w.writerow([r.chave, r.codigo, r.mensagem, acao, "SIM" if rep else "NÃO (corrigir)", r.origem])
    reprocessaveis = [r.chave for r in erros if ACOES.get(r.codigo, (True,))[0]]
    if reprocessaveis:
        (pasta / "REPROCESSAR.txt").write_text("\n".join(reprocessaveis) + "\n", encoding="utf-8")


def _resumo(pasta: Path, resultados: list[Resultado]) -> int:
    ok = sum(r.status == "OK" for r in resultados)
    av = sum(r.status == "AVISO" for r in resultados)
    er = [r for r in resultados if r.status == "ERRO"]
    _print(f"\n{COR['neg']}=== RESUMO ==={COR['fim']}")
    _print(f"  Gerados:           {ok}", "ok")
    if av:
        _print(f"  Gerados com aviso: {av}", "aviso")
    if er:
        _print(f"  Com erro:          {len(er)}", "erro")
        por_codigo: dict[str, int] = {}
        for r in er:
            por_codigo[r.codigo] = por_codigo.get(r.codigo, 0) + 1
        for cod, n in por_codigo.items():
            _print(f"      {cod} ({n}): {ACOES.get(cod, (True, ''))[1]}", "erro")
        if (pasta / "REPROCESSAR.txt").exists():
            _print("\n  Depois de tratar, rode:  python executar.py --reprocessar", "aviso")
        _print(f"  Detalhes: {pasta / 'ERROS.csv'}", "aviso")
    _print(f"\nSaída do lote: {pasta}\n")
    return 1 if er else 0
