"""Operações usadas pela tela (app_gui.pyw), sem dependência de interface gráfica."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import processador, segredo, trava
from .certificado import Certificado, ErroCertificado
from .fonte_sql import ConexaoSql

RAIZ = processador.RAIZ
ARQ_CONFIG = processador.PASTAS["config"] / "config.json"
SENHA_CERT = processador.PASTAS["config"] / "senha_certificado.dat"
SENHA_SQL = processador.PASTAS["config"] / "senha_sql.dat"
PASTA_CARGAS = processador.PASTAS["saida"] / "cargas"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
VERSAO = "1.0"
def ler_config() -> dict:
    with open(processador.arquivo_config(), encoding="utf-8-sig") as f:
        return json.load(f)


def gravar_config(cfg: dict) -> None:
    tmp = ARQ_CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(ARQ_CONFIG)
@dataclass
class Status:
    ok: bool
    texto: str


def status_certificado() -> Status:
    try:
        cfg = ler_config()
    except Exception as e:
        return Status(False, f"config.json inválido: {e}")
    lista = cfg.get("certificados") or []
    if not lista:
        return Status(False, "Nenhum certificado configurado")
    item = lista[0]
    arq = processador._resolver(item.get("arquivo", ""))
    if not arq.is_file():
        return Status(False, f"Arquivo do certificado não encontrado ({arq.name})")
    try:
        senha = segredo.obter_senha(item, SENHA_CERT)
    except RuntimeError as e:
        return Status(False, str(e))
    if not senha:
        return Status(False, "Senha do certificado não gravada")
    try:
        cert = Certificado(arq, senha)
    except ErroCertificado as e:
        return Status(False, str(e))
    if cert.vencido:
        return Status(False, f"Certificado VENCIDO em {cert.validade:%d/%m/%Y}")
    dias = (cert.validade - datetime.now(cert.validade.tzinfo)).days
    nome = cert.titular.split(":")[0]
    alerta = f"  (vence em {dias} dias!)" if dias <= 30 else ""
    return Status(True, f"{nome} · válido até {cert.validade:%d/%m/%Y}{alerta}")


def status_banco(testar: bool = True) -> Status:
    try:
        sql = ler_config().get("sql_server", {})
    except Exception as e:
        return Status(False, f"config.json inválido: {e}")
    if not sql.get("servidor") or "NOME_DO" in sql.get("servidor", ""):
        return Status(False, "Banco de dados não configurado")
    rotulo = f"{sql.get('banco')} em {sql.get('servidor')}"
    if not testar:
        return Status(True, rotulo)
    try:
        cn = ConexaoSql(sql)
        cn.fechar()
        return Status(True, rotulo)
    except Exception as e:
        msg = str(e)
        if "18456" in msg or "Login failed" in msg or "Falha de logon" in msg:
            msg = "usuário ou senha do banco incorretos"
        elif "08001" in msg or "tempo limite" in msg.lower() or "timeout" in msg.lower():
            msg = "servidor não encontrado ou inacessível pela rede"
        return Status(False, f"{rotulo}: {msg[:160]}")
def configurar_certificado(arquivo_pfx: str, senha: str) -> str:
    origem = Path(arquivo_pfx)
    cert = Certificado(origem, senha)  # valida senha antes de qualquer alteração
    if cert.vencido:
        raise ErroCertificado(f"o certificado venceu em {cert.validade:%d/%m/%Y}")
    destino_dir = processador.PASTAS["certificados"]
    destino_dir.mkdir(exist_ok=True)
    destino = destino_dir / origem.name
    if origem.resolve() != destino.resolve():
        shutil.copy2(origem, destino)
    segredo.salvar(SENHA_CERT, senha)
    cfg = ler_config()
    item = (cfg.get("certificados") or [{}])[0]
    item.update({"arquivo": f"certificados/{origem.name}", "senha": "",
                 "cnpj": cert.cnpj or item.get("cnpj", ""), "uf": item.get("uf") or cfg.get("uf_autor", "SP")})
    item.setdefault("senha_variavel_ambiente", "DANFE_SENHA_CERT")
    cfg["certificados"] = [item]
    gravar_config(cfg)
    return cert.descricao()
def configurar_banco(servidor: str, banco: str, usuario: str, senha: str | None, login_windows: bool) -> None:
    cfg = ler_config()
    sql = cfg.setdefault("sql_server", {})
    sql.update({"servidor": servidor.strip(), "banco": banco.strip(),
                "autenticacao_windows": bool(login_windows), "usuario": usuario.strip(), "senha": ""})
    if senha:  # vazio = mantém a senha já gravada
        segredo.salvar(SENHA_SQL, senha)
    gravar_config(cfg)
class _Saida(io.TextIOBase):
    """Recebe os print() do processamento e repassa linha a linha (sem cores ANSI)."""

    def __init__(self, ao_receber_linha):
        self._f, self._buf = ao_receber_linha, ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            linha, self._buf = self._buf.split("\n", 1)
            self._f(_ANSI.sub("", linha))
        return len(s)

    def flush(self):
        if self._buf:
            self._f(_ANSI.sub("", self._buf))
            self._buf = ""


def separar_cargas(texto: str) -> list[str]:
    return [c for c in re.split(r"[\s,;]+", texto.strip()) if c]


def gerar_cargas(cargas: list[str], ao_receber_linha, ao_terminar) -> threading.Thread:
    """Executa em segundo plano. ao_terminar(relatorio: list[dict], erro: str | None)."""

    def trabalho():
        relatorio, erro = [], None
        saida = _Saida(ao_receber_linha)
        try:
            trava.adquirir()
        except trava.EmExecucao as e:
            ao_terminar([], str(e))
            return
        try:
            with redirect_stdout(saida), redirect_stderr(saida):
                processador.executar_cargas(cargas, relatorio)
        except BaseException as e:
            erro = f"{type(e).__name__}: {e}"
        finally:
            saida.flush()
            trava.liberar()
        ao_terminar(relatorio, erro)

    t = threading.Thread(target=trabalho, daemon=True)
    t.start()
    return t


def cargas_recentes(limite: int = 200) -> list[tuple[str, Path, datetime]]:
    if not PASTA_CARGAS.is_dir():
        return []
    itens = []
    for pdf in PASTA_CARGAS.glob("CARGA_*.pdf"):
        nome = pdf.stem.replace("CARGA_", "").replace("_INCOMPLETA", " (INCOMPLETA)")
        itens.append((nome, pdf, datetime.fromtimestamp(pdf.stat().st_mtime)))
    itens.sort(key=lambda x: x[2], reverse=True)
    return itens[:limite]


def abrir(caminho: str | Path, imprimir: bool = False) -> None:
    caminho = str(caminho)
    if sys.platform == "win32":
        if imprimir:
            try:
                os.startfile(caminho, "print")  # usa o leitor de PDF padrão
                return
            except OSError:
                pass  # sem programa associado à impressão: apenas abre
        os.startfile(caminho)
    else:
        subprocess.Popen(["xdg-open", caminho])
