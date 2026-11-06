"""Guarda senhas protegidas pelo Windows (DPAPI), atreladas ao usuário do Windows.

O arquivo gerado só pode ser aberto pelo mesmo usuário, no mesmo computador.
Não é possível copiá-lo para outra máquina e usar a senha.
"""
from __future__ import annotations

import base64
import ctypes
import os
import sys
from pathlib import Path


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(dados: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(dados, len(dados))
    return _Blob(len(dados), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi(dados: bytes, proteger: bool) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("a gravação protegida de senha só está disponível no Windows")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    entrada, saida = _blob(dados), _Blob()
    entropia = _blob(b"gerador_danfe")
    fn = crypt32.CryptProtectData if proteger else crypt32.CryptUnprotectData
    args = (ctypes.byref(entrada), "gerador_danfe" if proteger else None, ctypes.byref(entropia), None, None,
            0x01, ctypes.byref(saida))  # CRYPTPROTECT_UI_FORBIDDEN
    if not fn(*args):
        raise RuntimeError("o Windows não conseguiu " + ("proteger" if proteger else "abrir") +
                           " a senha (arquivo de outro usuário/computador?). Grave-a novamente.")
    try:
        return ctypes.string_at(saida.pbData, saida.cbData)
    finally:
        kernel32.LocalFree(saida.pbData)


def salvar(arquivo: Path, senha: str) -> None:
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(base64.b64encode(_dpapi(senha.encode("utf-8"), True)).decode(), encoding="ascii")


def ler(arquivo: Path) -> str | None:
    if not arquivo.is_file():
        return None
    return _dpapi(base64.b64decode(arquivo.read_text(encoding="ascii").strip()), False).decode("utf-8")


def obter_senha(item: dict, arquivo_padrao: Path) -> str:
    """Ordem: 'senha' no config -> variável de ambiente -> arquivo protegido."""
    if item.get("senha"):
        return item["senha"]
    var = item.get("senha_variavel_ambiente")
    if var and os.environ.get(var):
        return os.environ[var]
    arq = Path(item.get("senha_protegida") or arquivo_padrao)
    return ler(arq) or ""
