"""XMLs vindos do SQL Server.

1) Exportação do SELECT (.txt/.csv) colocada em entrada/: cada linha/célula com o
   XML da NF-e é reconhecida automaticamente.
2) Conexão direta (opcional): o programa recebe só as chaves e busca o XML no banco
   com a consulta configurada em config.json -> "sql_server".
"""
from __future__ import annotations

import re
from pathlib import Path

from lxml import etree

_XML_NFE = re.compile(rb"<(nfeProc|NFe)\b[^>]*>.*?</\1>", re.S)
_ID_CHAVE = re.compile(rb'Id\s*=\s*"+NFe(\d{44})"')


def chave_do_xml(xml: bytes) -> str | None:
    m = _ID_CHAVE.search(xml)
    return m.group(1).decode() if m else None


def _normalizar(bloco: bytes) -> bytes | None:
    """Desfaz aspas duplicadas de CSV (xmlns=""..."") e valida o XML."""
    if b'=""' in bloco:
        bloco = bloco.replace(b'""', b'"')
    try:
        etree.fromstring(bloco)
    except etree.XMLSyntaxError:
        return None
    return bloco


def extrair_xmls(conteudo: bytes) -> tuple[list[tuple[str, bytes]], bytes, int]:
    """Devolve ([(chave, xml)], conteúdo restante sem os XMLs, qtde de blocos inválidos)."""
    achados, invalidos = [], 0
    for m in _XML_NFE.finditer(conteudo):
        xml = _normalizar(m.group(0))
        chave = chave_do_xml(xml) if xml else None
        if xml and chave:
            achados.append((chave, xml))
        else:
            invalidos += 1
    restante = _XML_NFE.sub(b"\n", conteudo)
    return achados, restante, invalidos


DRIVERS_PREFERIDOS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server")


def escolher_driver(preferido: str | None, instalados: list[str]) -> str:
    """Usa o driver do config se instalado; senão o melhor disponível (o 'SQL Server' vem com o Windows)."""
    if preferido and preferido in instalados:
        return preferido
    for d in DRIVERS_PREFERIDOS:
        if d in instalados:
            return d
    raise RuntimeError("nenhum driver ODBC do SQL Server instalado. Instale o 'ODBC Driver 18 for SQL Server'.")


class ConexaoSql:
    """Busca XML por chave direto no SQL Server (requer 'pip install pyodbc')."""

    def __init__(self, cfg: dict):
        try:
            import pyodbc
        except ImportError as e:
            raise RuntimeError("para conectar ao SQL Server instale o pyodbc: python -m pip install pyodbc") from e
        self.cfg = cfg
        self.driver = escolher_driver(cfg.get("driver"), pyodbc.drivers())
        partes = [f"DRIVER={{{self.driver}}}", f"SERVER={cfg['servidor']}", f"DATABASE={cfg['banco']}"]
        if cfg.get("autenticacao_windows", True):
            partes.append("Trusted_Connection=yes")
        else:
            from .segredo import obter_senha
            senha = obter_senha(cfg, Path(__file__).resolve().parents[2] / "config" / "senha_sql.dat")
            partes += [f"UID={cfg['usuario']}", f"PWD={{{senha}}}"]
        if self.driver != "SQL Server":  # driver antigo do Windows não conhece essa opção
            partes.append("TrustServerCertificate=yes")
        self.cn = pyodbc.connect(";".join(partes), timeout=int(cfg.get("timeout_segundos", 15)))

    def _consulta(self, nome: str) -> str:
        sql = (self.cfg.get(nome) or "").strip()
        if not sql:
            raise RuntimeError(f"config.json: preencha sql_server.{nome}")
        if sql.count("?") != 1:
            raise RuntimeError(f"config.json: sql_server.{nome} deve ter exatamente um '?'")
        return sql

    def buscar_carga(self, carga: str) -> tuple[list[tuple[str, bytes]], int, int]:
        """Executa a consulta da carga. Retorna ([(chave, xml)] na ordem do SELECT, linhas, inválidas)."""
        cur = self.cn.cursor()
        cur.execute(self._consulta("consulta_carga"), int(carga) if str(carga).isdigit() else carga)
        notas, vistas, linhas, invalidas = [], set(), 0, 0
        for linha in cur.fetchall():
            linhas += 1
            valor = linha[0]
            if valor is None:
                invalidas += 1
                continue
            dado = valor.encode("utf-8") if isinstance(valor, str) else bytes(valor)
            achados, _, inval = extrair_xmls(dado)
            invalidas += inval if achados else max(inval, 1)
            for ch, xml in achados:
                if ch not in vistas:
                    vistas.add(ch)
                    notas.append((ch, xml))
        return notas, linhas, invalidas

    def buscar(self, chave: str) -> bytes | None:
        cur = self.cn.cursor()
        cur.execute(self._consulta("consulta"), chave)
        for linha in cur.fetchall():
            valor = linha[0]
            if valor is None:
                continue
            dado = valor.encode("utf-8") if isinstance(valor, str) else bytes(valor)
            achados, _, _ = extrair_xmls(dado)
            for ch, xml in achados:
                if ch == chave:
                    return xml
        return None

    def fechar(self):
        try:
            self.cn.close()
        except Exception:
            pass


def salvar_importados(achados: list[tuple[str, bytes]], pasta: Path) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    for chave, xml in achados:
        (pasta / f"{chave}-nfe.xml").write_bytes(xml)
