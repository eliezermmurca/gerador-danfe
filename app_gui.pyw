"""Gerador de DANFE: interface gráfica.

Abra com duplo clique (ou pelo atalho criado pelo instalador).
"""
import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "src"))

from danfe_app import servico

COR_FUNDO = "#f4f6f8"
COR_PRIMARIA = "#1f5fa8"
COR_OK = "#1e7a3c"
COR_AVISO = "#a86400"
COR_ERRO = "#b3261e"
FONTE = ("Segoe UI", 10)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Gerador de DANFE  ·  v{servico.VERSAO}")
        self.geometry("880x640")
        self.minsize(760, 560)
        self.configure(bg=COR_FUNDO)
        icone = RAIZ / "recursos" / "danfe.ico"
        if icone.exists():
            try:
                self.iconbitmap(str(icone))
            except tk.TclError:
                pass
        self.fila: queue.Queue = queue.Queue()
        self.executando = False
        self.ultimo_pdf: str | None = None
        self.total_notas = 0

        self._estilos()
        self._montar()
        self.protocol("WM_DELETE_WINDOW", self._fechar)
        self.after(100, self._processar_fila)
        self.after(300, lambda: self.atualizar_status(testar_banco=True))
    def _estilos(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            st.theme_use("clam")
        st.configure(".", font=FONTE)
        st.configure("TFrame", background=COR_FUNDO)
        st.configure("TLabelframe", background=COR_FUNDO)
        st.configure("TLabelframe.Label", background=COR_FUNDO, font=("Segoe UI", 10, "bold"))
        st.configure("TLabel", background=COR_FUNDO)
        st.configure("TCheckbutton", background=COR_FUNDO)
        st.configure("Titulo.TLabel", font=("Segoe UI", 16, "bold"), foreground=COR_PRIMARIA)
        st.configure("Sub.TLabel", foreground="#555")
        st.configure("Grande.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 8))
        st.configure("Resultado.TLabel", font=("Segoe UI", 11, "bold"))

    def _montar(self):
        topo = ttk.Frame(self, padding=(16, 12, 16, 4))
        topo.pack(fill="x")
        ttk.Label(topo, text="Gerador de DANFE", style="Titulo.TLabel").pack(anchor="w")
        self.lbl_cert = ttk.Label(topo, text="Certificado: verificando…", style="Sub.TLabel")
        self.lbl_cert.pack(anchor="w")
        self.lbl_banco = ttk.Label(topo, text="Banco: verificando…", style="Sub.TLabel")
        self.lbl_banco.pack(anchor="w")

        self.abas = ttk.Notebook(self)
        self.abas.pack(fill="both", expand=True, padx=12, pady=8)
        self.aba_gerar = ttk.Frame(self.abas, padding=12)
        self.aba_hist = ttk.Frame(self.abas, padding=12)
        self.aba_cfg = ttk.Frame(self.abas, padding=12)
        self.abas.add(self.aba_gerar, text="  Gerar DANFEs  ")
        self.abas.add(self.aba_hist, text="  Cargas geradas  ")
        self.abas.add(self.aba_cfg, text="  Configurações  ")
        self._montar_gerar()
        self._montar_historico()
        self._montar_config()
    def _montar_gerar(self):
        f = self.aba_gerar
        linha = ttk.Frame(f)
        linha.pack(fill="x")
        ttk.Label(linha, text="Número da carga:", font=("Segoe UI", 11)).pack(side="left")
        self.ent_carga = ttk.Entry(linha, font=("Segoe UI", 14), width=22)
        self.ent_carga.pack(side="left", padx=10)
        self.ent_carga.bind("<Return>", lambda _e: self.gerar())
        self.btn_gerar = ttk.Button(linha, text="Gerar DANFEs", style="Grande.TButton", command=self.gerar)
        self.btn_gerar.pack(side="left")
        ttk.Label(f, text="Para várias cargas, separe por espaço ou vírgula (ex.: 1234 1235).",
                  style="Sub.TLabel").pack(anchor="w", pady=(4, 8))

        self.progresso = ttk.Progressbar(f, mode="determinate")
        self.progresso.pack(fill="x")
        self.lbl_etapa = ttk.Label(f, text="", style="Sub.TLabel")
        self.lbl_etapa.pack(anchor="w", pady=(2, 6))

        self.quadro_res = ttk.Frame(f)
        self.lbl_res = ttk.Label(self.quadro_res, text="", style="Resultado.TLabel")
        self.lbl_res.pack(side="left")
        self.btn_pasta = ttk.Button(self.quadro_res, text="Abrir pasta",
                                    command=lambda: servico.abrir(servico.PASTA_CARGAS))
        self.btn_imprimir = ttk.Button(self.quadro_res, text="Imprimir",
                                       command=lambda: self._abrir_pdf(imprimir=True))
        self.btn_abrir = ttk.Button(self.quadro_res, text="Abrir PDF", command=self._abrir_pdf)
        for b in (self.btn_pasta, self.btn_imprimir, self.btn_abrir):
            b.pack(side="right", padx=3)

        quadro_log = ttk.Frame(f)
        quadro_log.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(quadro_log, height=14, wrap="word", font=("Consolas", 9), bg="white",
                           relief="solid", borderwidth=1, state="disabled")
        barra = ttk.Scrollbar(quadro_log, command=self.log.yview)
        self.log.configure(yscrollcommand=barra.set)
        barra.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("ok", foreground=COR_OK)
        self.log.tag_configure("aviso", foreground=COR_AVISO)
        self.log.tag_configure("erro", foreground=COR_ERRO)
        self.log.tag_configure("titulo", font=("Consolas", 9, "bold"))
        self.ent_carga.focus_set()

    def _escrever(self, linha: str):
        tag = None
        if "[ERRO]" in linha:
            tag = "erro"
        elif "[AVISO]" in linha or "INCOMPLET" in linha:
            tag = "aviso"
        elif "[OK]" in linha or "PDF PARA IMPRESSÃO" in linha:
            tag = "ok"
        elif linha.startswith("#####") or linha.startswith("==="):
            tag = "titulo"
        self.log.configure(state="normal")
        self.log.insert("end", linha + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def gerar(self):
        if self.executando:
            return
        cargas = servico.separar_cargas(self.ent_carga.get())
        if not cargas:
            messagebox.showinfo("Gerar DANFEs", "Digite o número da carga.")
            return
        invalidas = [c for c in cargas if not c.isdigit()]
        if invalidas:
            messagebox.showwarning("Gerar DANFEs", f"Número de carga inválido: {', '.join(invalidas)}")
            return
        self.executando = True
        self.btn_gerar.state(["disabled"])
        self.quadro_res.pack_forget()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.progresso.configure(value=0, maximum=1)
        self.lbl_etapa.configure(text="Consultando o banco de dados…")
        self.configure(cursor="watch")
        servico.gerar_cargas(cargas, lambda l: self.fila.put(("linha", l)),
                             lambda rel, erro: self.fila.put(("fim", (rel, erro))))

    def _processar_fila(self):
        try:
            while True:
                tipo, dado = self.fila.get_nowait()
                if tipo == "linha":
                    self._tratar_linha(dado)
                elif tipo == "fim":
                    self._concluir(*dado)
                elif tipo == "status":
                    self._aplicar_status(*dado)
        except queue.Empty:
            pass
        self.after(100, self._processar_fila)

    def _tratar_linha(self, linha: str):
        if not linha.strip():
            return
        self._escrever(linha)
        if linha.startswith("##### CARGA"):
            self.lbl_etapa.configure(text=linha.strip("# ").title())
        elif linha.startswith("Chaves encontradas:"):
            try:
                self.total_notas = int(linha.split(":")[1])
                self.progresso.configure(maximum=max(self.total_notas, 1), value=0)
            except ValueError:
                pass
        elif linha.startswith("[") and "/" in linha[:12]:
            try:
                atual, total = linha[1:linha.index("]")].split("/")
                self.progresso.configure(maximum=int(total), value=int(atual))
                self.lbl_etapa.configure(text=f"Gerando DANFE {atual} de {total}…")
            except ValueError:
                pass

    def _concluir(self, relatorio: list, erro: str | None):
        self.executando = False
        self.btn_gerar.state(["!disabled"])
        self.configure(cursor="")
        self.lbl_etapa.configure(text="")
        self._carregar_historico()
        if erro:
            self._escrever(f"[ERRO] {erro}")
            messagebox.showerror("Gerar DANFEs", erro)
            return
        pdfs = [r for r in relatorio if r.get("pdf")]
        falhas = [r for r in relatorio if not r.get("pdf") or r.get("erros")]
        if pdfs:
            ult = pdfs[-1]
            self.ultimo_pdf = ult["pdf"]
            incompleta = bool(ult.get("erros"))
            texto = (f"Carga {ult['carga']}: {ult['incluidas']} nota(s), {ult['folhas']} folha(s)"
                     + (" (INCOMPLETA)" if incompleta else ""))
            if len(pdfs) > 1:
                texto = f"{len(pdfs)} cargas geradas. Última: " + texto
            self.lbl_res.configure(text=texto, foreground=COR_AVISO if incompleta else COR_OK)
            self.quadro_res.pack(fill="x", pady=(0, 4), before=self.log.master)
        if falhas:
            linhas = []
            for r in falhas:
                if r.get("falha"):
                    linhas.append(f"Carga {r['carga']}: {r['falha']}")
                else:
                    linhas.append(f"Carga {r['carga']}: {len(r['erros'])} nota(s) com erro, PDF INCOMPLETO")
                    for ch, cod, msg in r["erros"][:5]:
                        linhas.append(f"   • NF {int(ch[25:34])}: {cod} – {msg[:90]}")
            messagebox.showwarning("Atenção", "\n".join(linhas) +
                                   "\n\nDepois de resolver, gere a carga novamente: as notas já prontas "
                                   "não são consultadas outra vez.")
        elif pdfs:
            self.ent_carga.delete(0, "end")
        self.ent_carga.focus_set()

    def _abrir_pdf(self, imprimir=False):
        if self.ultimo_pdf and Path(self.ultimo_pdf).exists():
            servico.abrir(self.ultimo_pdf, imprimir)
    def _montar_historico(self):
        f = self.aba_hist
        cols = ("carga", "data")
        self.tabela = ttk.Treeview(f, columns=cols, show="headings", height=14)
        self.tabela.heading("carga", text="Carga")
        self.tabela.heading("data", text="Gerada em")
        self.tabela.column("carga", width=260)
        self.tabela.column("data", width=180)
        self.tabela.pack(fill="both", expand=True)
        self.tabela.bind("<Double-1>", lambda _e: self._hist_abrir())
        botoes = ttk.Frame(f)
        botoes.pack(fill="x", pady=(8, 0))
        ttk.Button(botoes, text="Abrir PDF", command=self._hist_abrir).pack(side="left")
        ttk.Button(botoes, text="Imprimir", command=lambda: self._hist_abrir(True)).pack(side="left", padx=6)
        ttk.Button(botoes, text="Abrir pasta",
                   command=lambda: servico.abrir(servico.PASTA_CARGAS)).pack(side="left")
        ttk.Button(botoes, text="Atualizar", command=self._carregar_historico).pack(side="right")
        self._pdfs_hist: dict[str, Path] = {}
        self._carregar_historico()

    def _carregar_historico(self):
        self.tabela.delete(*self.tabela.get_children())
        self._pdfs_hist.clear()
        for nome, pdf, quando in servico.cargas_recentes():
            iid = self.tabela.insert("", "end", values=(nome, quando.strftime("%d/%m/%Y %H:%M")))
            self._pdfs_hist[iid] = pdf

    def _hist_abrir(self, imprimir=False):
        sel = self.tabela.selection()
        if sel:
            servico.abrir(self._pdfs_hist[sel[0]], imprimir)
    def _montar_config(self):
        f = self.aba_cfg
        q1 = ttk.LabelFrame(f, text="Certificado digital (A1)", padding=10)
        q1.pack(fill="x")
        self.lbl_cert_cfg = ttk.Label(q1, text="", wraplength=760)
        self.lbl_cert_cfg.pack(anchor="w")
        b1 = ttk.Frame(q1)
        b1.pack(anchor="w", pady=(8, 0))
        ttk.Button(b1, text="Selecionar certificado (.pfx)…", command=self._cfg_certificado).pack(side="left")
        ttk.Button(b1, text="Gravar/alterar senha", command=self._cfg_senha_cert).pack(side="left", padx=6)

        q2 = ttk.LabelFrame(f, text="Banco de dados (SQL Server)", padding=10)
        q2.pack(fill="x", pady=10)
        self.var_srv, self.var_banco = tk.StringVar(), tk.StringVar()
        self.var_usuario, self.var_senha = tk.StringVar(), tk.StringVar()
        self.var_win = tk.BooleanVar()
        campos = [("Servidor", self.var_srv, ""), ("Banco", self.var_banco, ""),
                  ("Usuário", self.var_usuario, ""), ("Senha", self.var_senha, "*")]
        for i, (rot, var, mostrar) in enumerate(campos):
            ttk.Label(q2, text=rot + ":").grid(row=i, column=0, sticky="w", pady=2)
            e = ttk.Entry(q2, textvariable=var, width=40, show=mostrar)
            e.grid(row=i, column=1, sticky="w", padx=8, pady=2)
            if rot == "Usuário":
                self.ent_usuario = e
            if rot == "Senha":
                self.ent_senha = e
        ttk.Label(q2, text="(deixe a senha em branco para manter a já gravada)", style="Sub.TLabel").grid(
            row=3, column=2, sticky="w")
        ttk.Checkbutton(q2, text="Usar o login do Windows", variable=self.var_win,
                        command=self._alternar_login).grid(row=4, column=1, sticky="w", pady=4)
        b2 = ttk.Frame(q2)
        b2.grid(row=5, column=1, sticky="w", pady=(6, 0))
        ttk.Button(b2, text="Salvar e testar conexão", command=self._cfg_banco).pack(side="left")

        q3 = ttk.LabelFrame(f, text="Pastas", padding=10)
        q3.pack(fill="x")
        ttk.Button(q3, text="PDFs das cargas", command=lambda: servico.abrir(servico.PASTA_CARGAS)).pack(
            side="left")
        ttk.Button(q3, text="Detalhes dos lotes (logs/erros)",
                   command=lambda: servico.abrir(servico.processador.PASTAS["saida"])).pack(side="left", padx=6)
        ttk.Button(q3, text="Pasta do programa", command=lambda: servico.abrir(servico.RAIZ)).pack(side="left")
        self._carregar_cfg_banco()

    def _carregar_cfg_banco(self):
        try:
            sql = servico.ler_config().get("sql_server", {})
        except Exception:
            sql = {}
        srv = sql.get("servidor", "")
        self.var_srv.set("" if "NOME_DO" in srv else srv)
        banco = sql.get("banco", "")
        self.var_banco.set("" if "NOME_DO" in banco else banco)
        self.var_usuario.set(sql.get("usuario", ""))
        self.var_win.set(bool(sql.get("autenticacao_windows", False)))
        self._alternar_login()

    def _alternar_login(self):
        estado = ["disabled"] if self.var_win.get() else ["!disabled"]
        self.ent_usuario.state(estado)
        self.ent_senha.state(estado)

    def _cfg_certificado(self):
        arq = filedialog.askopenfilename(title="Selecione o certificado A1",
                                         filetypes=[("Certificado A1", "*.pfx *.p12"), ("Todos", "*.*")])
        if arq:
            self._gravar_cert(arq)

    def _cfg_senha_cert(self):
        try:
            item = (servico.ler_config().get("certificados") or [{}])[0]
            arq = servico.processador._resolver(item.get("arquivo", ""))
        except Exception:
            arq = None
        if not arq or not arq.is_file():
            messagebox.showinfo("Certificado", "Selecione primeiro o arquivo do certificado (.pfx).")
            return self._cfg_certificado()
        self._gravar_cert(str(arq))

    def _gravar_cert(self, arq: str):
        senha = simpledialog.askstring("Senha do certificado", f"Senha de {Path(arq).name}:", show="*",
                                       parent=self)
        if senha is None:
            return
        try:
            desc = servico.configurar_certificado(arq, senha)
        except Exception as e:
            messagebox.showerror("Certificado", f"Não foi possível usar o certificado:\n{e}")
            return
        messagebox.showinfo("Certificado", f"Certificado configurado com sucesso.\n\n{desc}")
        self.atualizar_status(testar_banco=False)

    def _cfg_banco(self):
        if not self.var_srv.get().strip() or not self.var_banco.get().strip():
            messagebox.showwarning("Banco de dados", "Informe o servidor e o banco.")
            return
        if not self.var_win.get() and not self.var_usuario.get().strip():
            messagebox.showwarning("Banco de dados", "Informe o usuário (ou marque 'Usar o login do Windows').")
            return
        try:
            servico.configurar_banco(self.var_srv.get(), self.var_banco.get(), self.var_usuario.get(),
                                     self.var_senha.get(), self.var_win.get())
        except Exception as e:
            messagebox.showerror("Banco de dados", f"Não foi possível salvar:\n{e}")
            return
        self.var_senha.set("")

        def resultado(st):
            if st.ok:
                messagebox.showinfo("Banco de dados", "Conexão com o banco realizada com sucesso.")
            else:
                messagebox.showerror("Banco de dados", f"Configuração salva, mas a conexão falhou:\n{st.texto}")

        self.atualizar_status(testar_banco=True, depois=resultado)
    def atualizar_status(self, testar_banco=True, depois=None):
        """Verifica certificado e banco em segundo plano (a conexão pode demorar) e atualiza a tela."""
        if testar_banco:
            self.lbl_banco.configure(text="Banco: verificando…", foreground="#555")

        def trabalho():
            try:
                sc = servico.status_certificado()
                sb = servico.status_banco(testar=testar_banco)
            except Exception as e:
                sc = sb = servico.Status(False, str(e))
            self.fila.put(("status", (sc, sb, testar_banco, depois)))

        threading.Thread(target=trabalho, daemon=True).start()

    def _aplicar_status(self, sc, sb, testar_banco, depois):
        self.lbl_cert.configure(text="Certificado: " + sc.texto,
                                foreground=COR_OK if sc.ok else COR_ERRO)
        self.lbl_cert_cfg.configure(text=sc.texto, foreground=COR_OK if sc.ok else COR_ERRO)
        if testar_banco or not sb.ok:
            self.lbl_banco.configure(text="Banco: " + sb.texto,
                                     foreground=COR_OK if sb.ok else COR_ERRO)
        if (not sc.ok or not sb.ok) and not self.executando:
            self.abas.select(self.aba_cfg)
        if depois:
            depois(sb)

    def _fechar(self):
        if self.executando and not messagebox.askyesno(
                "Sair", "Há uma geração em andamento. Deseja realmente sair?"):
            return
        self.destroy()


def main():
    try:
        App().mainloop()
    except Exception:
        log = RAIZ / "erro_tela.txt"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            messagebox.showerror("Gerador de DANFE", f"Erro inesperado. Detalhes em:\n{log}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
