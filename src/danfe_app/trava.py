"""Trava contra duas gerações simultâneas (evita consultas duplicadas à SEFAZ e arquivos em conflito)."""
from __future__ import annotations

import getpass
import os
import time
from pathlib import Path

TRAVA = Path(__file__).resolve().parents[2] / "config" / ".em_execucao.lock"
EXPIRA_SEG = 3 * 3600


class EmExecucao(Exception):
    pass


def adquirir() -> None:
    TRAVA.parent.mkdir(exist_ok=True)
    if TRAVA.exists() and time.time() - TRAVA.stat().st_mtime > EXPIRA_SEG:
        TRAVA.unlink(missing_ok=True)  # trava esquecida por uma execução interrompida
    try:
        fd = os.open(TRAVA, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        info = TRAVA.read_text(encoding="utf-8", errors="ignore").strip()
        raise EmExecucao(f"Já existe uma geração em andamento ({info}). Aguarde terminar. "
                         f"Se nenhuma estiver rodando, apague o arquivo {TRAVA}") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"iniciada em {time.strftime('%d/%m/%Y %H:%M:%S')} por {getpass.getuser()} (PID {os.getpid()})")


def liberar() -> None:
    TRAVA.unlink(missing_ok=True)
