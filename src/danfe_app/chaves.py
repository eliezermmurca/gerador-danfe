"""Leitura e validação das chaves de acesso de NF-e."""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import cycle
from pathlib import Path

UF_CODIGOS = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
    "28": "SE", "29": "BA", "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR",
    "42": "SC", "43": "RS", "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}
UF_SIGLA_PARA_CODIGO = {v: k for k, v in UF_CODIGOS.items()}

_CHAVE = re.compile(r"(?<!\d)\d{44}(?!\d)")
_NUMERO_LONGO = re.compile(r"(?<!\d)\d{30,}(?!\d)")
_SEPARADOR_ENTRE_DIGITOS = re.compile(r"(?<=\d)[ .\-/](?=\d)")


def digito_verificador(chave43: str) -> int:
    soma = sum(int(d) * p for d, p in zip(reversed(chave43), cycle(range(2, 10))))
    resto = soma % 11
    return 0 if resto in (0, 1) else 11 - resto


def validar_chave(chave: str) -> str | None:
    """Retorna None se a chave for válida, ou a descrição do problema."""
    if not re.fullmatch(r"\d{44}", chave):
        digitos = len(re.sub(r"\D", "", chave))
        return f"a chave deve ter 44 dígitos (tem {digitos})"
    if chave[:2] not in UF_CODIGOS:
        return f"código de UF inválido ({chave[:2]})"
    mes = int(chave[4:6])
    if not 1 <= mes <= 12:
        return f"mês de emissão inválido ({chave[4:6]})"
    if digito_verificador(chave[:43]) != int(chave[43]):
        return "dígito verificador não confere (provável erro de digitação)"
    modelo = chave[20:22]
    if modelo == "65":
        return "chave de NFC-e (modelo 65): este gerador emite DANFE de NF-e modelo 55"
    if modelo != "55":
        return f"modelo {modelo} não é NF-e (esperado 55)"
    return None


def info_chave(chave: str) -> dict:
    return {
        "uf": UF_CODIGOS.get(chave[:2], "?"),
        "aamm": chave[2:6],
        "cnpj_emitente": chave[6:20],
        "modelo": chave[20:22],
        "serie": chave[22:25],
        "numero": chave[25:34],
    }


@dataclass
class EntradaChave:
    chave: str
    origem: str
    erro: str | None = None


def _ler_texto(arquivo: Path) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return arquivo.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return arquivo.read_text(encoding="latin-1", errors="replace")


def extrair_chaves_de_linha(linha: str) -> tuple[list[str], list[str]]:
    """Devolve (chaves de 44 dígitos encontradas, sequências longas suspeitas)."""
    achadas = _CHAVE.findall(linha)
    if not achadas:
        # aceita chaves digitadas com espaços/pontos: "3524 1012 3456 ..."
        unida = _SEPARADOR_ENTRE_DIGITOS.sub("", linha)
        achadas = _CHAVE.findall(unida)
        if not achadas:
            return [], _NUMERO_LONGO.findall(unida)
    return achadas, []


def ler_entradas(arquivos: list[Path], textos: dict[Path, str] | None = None,
                 iniciais: list[EntradaChave] | None = None) -> list[EntradaChave]:
    entradas: list[EntradaChave] = []
    vistas: set[str] = set()
    for ent in iniciais or []:
        if ent.chave not in vistas:
            vistas.add(ent.chave)
            entradas.append(ent)
    for arquivo in arquivos:
        texto = (textos or {}).get(arquivo)
        if texto is None:
            texto = _ler_texto(arquivo)
        for n, linha in enumerate(texto.splitlines(), start=1):
            origem = f"{arquivo.name}:{n}"
            chaves, suspeitas = extrair_chaves_de_linha(linha)
            for s in suspeitas:
                entradas.append(EntradaChave(s, origem, f"a chave deve ter 44 dígitos (tem {len(s)})"))
            for ch in chaves:
                if ch in vistas:
                    continue  # duplicada: processa uma vez só
                vistas.add(ch)
                entradas.append(EntradaChave(ch, origem, validar_chave(ch)))
    return entradas
