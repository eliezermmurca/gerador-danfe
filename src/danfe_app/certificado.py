"""Carregamento de certificado digital A1 (.pfx / .p12)."""
from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, pkcs12,
)

OID_CNPJ_ICP = "2.16.76.1.3.3"  # CNPJ no certificado e-CNPJ ICP-Brasil


class ErroCertificado(Exception):
    pass


class Certificado:
    def __init__(self, arquivo: str | Path, senha: str):
        self.arquivo = Path(arquivo)
        if not self.arquivo.exists():
            raise ErroCertificado(f"arquivo de certificado não encontrado: {self.arquivo}")
        try:
            self.chave_privada, self.cert, self.cadeia = pkcs12.load_key_and_certificates(
                self.arquivo.read_bytes(), senha.encode() if senha else None
            )
        except ValueError as e:
            raise ErroCertificado(
                f"não foi possível abrir {self.arquivo.name}: senha incorreta ou arquivo inválido ({e})"
            ) from e
        if self.cert is None or self.chave_privada is None:
            raise ErroCertificado(f"{self.arquivo.name} não contém certificado e chave privada")
        self.titular = self._cn()
        self.cnpj = self._extrair_cnpj()
        self.validade = getattr(self.cert, "not_valid_after_utc", None) or \
            self.cert.not_valid_after.replace(tzinfo=timezone.utc)
        self._tmp: str | None = None
        self.par_pem: tuple[str, str] | None = None
    def _cn(self) -> str:
        try:
            return self.cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
        except IndexError:
            return self.cert.subject.rfc4514_string()

    def _extrair_cnpj(self) -> str | None:
        try:
            san = self.cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            for nome in san:
                if isinstance(nome, x509.OtherName) and nome.type_id.dotted_string == OID_CNPJ_ICP:
                    m = re.search(rb"\d{14}", nome.value)
                    if m:
                        return m.group().decode()
        except x509.ExtensionNotFound:
            pass
        m = re.search(r"(\d{14})", self.titular)
        return m.group(1) if m else None

    @property
    def vencido(self) -> bool:
        return datetime.now(timezone.utc) > self.validade

    def descricao(self) -> str:
        return f"{self.titular} | CNPJ {self.cnpj or '?'} | válido até {self.validade:%d/%m/%Y}"
    def __enter__(self) -> "Certificado":
        self._tmp = tempfile.mkdtemp(prefix="danfe_cert_")
        cert_pem = os.path.join(self._tmp, "cert.pem")
        key_pem = os.path.join(self._tmp, "key.pem")
        with open(cert_pem, "wb") as f:
            f.write(self.cert.public_bytes(Encoding.PEM))
            for extra in self.cadeia or []:
                f.write(extra.public_bytes(Encoding.PEM))
        with open(key_pem, "wb") as f:
            f.write(self.chave_privada.private_bytes(
                Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()))
        os.chmod(key_pem, stat.S_IRUSR | stat.S_IWUSR)
        self.par_pem = (cert_pem, key_pem)
        return self

    def __exit__(self, *exc) -> None:
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp = None
        self.par_pem = None
