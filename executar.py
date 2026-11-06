"""Gerador de DANFE em lote.

Uso:
    python executar.py --carga 1234             gera os DANFEs da carga e junta em saida/cargas/CARGA_1234.pdf
    python executar.py --carga 1234 1235        várias cargas, uma após a outra
    python executar.py                          processa os .txt/.csv da pasta entrada/
    python executar.py --reprocessar            reprocessa as chaves com erro do último lote
    python executar.py --salvar-senha           grava a senha do certificado (protegida pelo Windows)
    python executar.py --salvar-senha-sql       grava a senha do usuário do SQL Server
    python executar.py --testar-certificado     confere senha, CNPJ e validade dos certificados
    python executar.py --testar-banco           testa a conexão com o SQL Server
"""
import argparse
import getpass
import logging
import sys
from contextlib import ExitStack
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "src"))

from danfe_app import processador, segredo, trava


def adquirir_trava() -> bool:
    try:
        trava.adquirir()
        return True
    except trava.EmExecucao as e:
        print(e)
        return False


def salvar_senha_certificado() -> int:
    cfg = processador.carregar_config()
    if not cfg["certificados"]:
        print("Nenhum certificado configurado em config/config.json")
        return 1
    from danfe_app.certificado import Certificado, ErroCertificado
    for item in cfg["certificados"]:
        arq = processador._resolver(item["arquivo"])
        senha = getpass.getpass(f"Senha do certificado {arq.name} (não aparece ao digitar): ")
        try:
            cert = Certificado(arq, senha)
        except ErroCertificado as e:
            print(f"[ERRO] {e}")
            return 1
        destino = Path(item.get("senha_protegida") or RAIZ / "config" / "senha_certificado.dat")
        segredo.salvar(destino, senha)
        print(f"[OK] Senha conferida e gravada protegida pelo Windows: {destino}\n  {cert.descricao()}")
    return 0


def salvar_senha_sql() -> int:
    cfg = processador.carregar_config()["sql_server"]
    senha = getpass.getpass(f"Senha do usuário '{cfg.get('usuario', '')}' do SQL Server: ")
    destino = Path(cfg.get("senha_protegida") or RAIZ / "config" / "senha_sql.dat")
    segredo.salvar(destino, senha)
    print(f"[OK] Senha gravada protegida pelo Windows: {destino}")
    return 0


def testar_banco() -> int:
    from danfe_app import fonte_sql
    cfg = processador.carregar_config()["sql_server"]
    try:
        import pyodbc
        print("Drivers ODBC instalados:", ", ".join(pyodbc.drivers()) or "(nenhum)")
        cn = fonte_sql.ConexaoSql(cfg)
        cur = cn.cn.cursor()
        cur.execute("SELECT @@SERVERNAME, DB_NAME(), SUSER_SNAME()")
        srv, banco, usuario = cur.fetchone()
        print(f"[OK] Conectado: servidor {srv} | banco {banco} | usuário {usuario}")
        cn.fechar()
        return 0
    except Exception as e:
        print(f"[ERRO] Falha na conexão: {e}")
        return 1


def testar_certificados() -> int:
    cfg = processador.carregar_config()
    if not cfg["certificados"]:
        print("Nenhum certificado configurado em config/config.json")
        return 1
    with ExitStack() as pilha:
        b = processador.BuscadorXml(cfg, logging.getLogger("teste"), pilha)
        return 0 if b.clientes() else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Gera DANFEs (PDF) a partir de chaves de NF-e ou de cargas do ERP.")
    ap.add_argument("--carga", nargs="+", metavar="NUM", help="número(s) da carga")
    ap.add_argument("--reprocessar", nargs="?", const="ULTIMO", metavar="PASTA_DO_LOTE",
                    help="reprocessa as chaves com erro (padrão: último lote)")
    ap.add_argument("--salvar-senha", action="store_true", help="grava a senha do certificado")
    ap.add_argument("--salvar-senha-sql", action="store_true", help="grava a senha do SQL Server")
    ap.add_argument("--testar-certificado", action="store_true", help="valida os certificados configurados")
    ap.add_argument("--testar-banco", action="store_true", help="testa a conexão com o SQL Server")
    args = ap.parse_args()

    if args.salvar_senha:
        return salvar_senha_certificado()
    if args.salvar_senha_sql:
        return salvar_senha_sql()
    if args.testar_certificado:
        return testar_certificados()
    if args.testar_banco:
        return testar_banco()

    if not adquirir_trava():
        return 2
    try:
        if args.carga:
            return processador.executar_cargas(args.carga)
        if args.reprocessar:
            if args.reprocessar == "ULTIMO":
                lote = processador.ultimo_lote()
                if lote is None:
                    print("Nenhum lote anterior encontrado em saida/.")
                    return 0
            else:
                lote = Path(args.reprocessar)
            return processador.executar(reprocessar=lote)
        return processador.executar()
    finally:
        trava.liberar()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuário.")
        sys.exit(130)
