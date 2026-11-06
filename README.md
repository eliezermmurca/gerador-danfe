# Gerador de DANFE

Gera o DANFE (PDF, NF-e modelo 55, retrato A4) a partir do XML das notas, em lote.

Foi criado como solução paliativa para um período em que o ERP da empresa não conseguia
emitir os DANFEs. O XML das notas já existia no banco do ERP (SQL Server); o programa busca
esses XMLs pelo número da carga, completa o protocolo de autorização na SEFAZ e monta um
único PDF por carga, pronto para impressão.

## Funcionalidades

- Geração por número da carga, com um PDF único por carga na ordem da consulta.
- Geração a partir de chaves de acesso (arquivos `.txt`/`.csv`) ou de XMLs locais.
- Consulta do protocolo de autorização na SEFAZ da UF da nota (NFeConsultaProtocolo4).
- Download de XML pela Distribuição DF-e do Ambiente Nacional, para notas de terceiros.
- Cache local dos XMLs, para não consultar a mesma chave duas vezes.
- Tarja para notas canceladas, de homologação ou sem protocolo.
- Relatório por lote com motivo do erro e lista de chaves para reprocessar.
- Senhas do certificado e do banco gravadas com DPAPI (Windows).
- Interface gráfica (Tkinter) e linha de comando.

## Requisitos

- Windows 10 ou superior (a gravação protegida de senhas usa DPAPI).
- Python 3.10 ou superior.
- Certificado digital A1 (e-CNPJ ICP-Brasil) da empresa.
- Para buscar por carga: SQL Server acessível e um driver ODBC
  ("ODBC Driver 17/18 for SQL Server" ou o driver "SQL Server" do Windows).

## Instalação

### Com o instalador

Execute `instalador\INSTALAR.bat`. Ele instala o Python se necessário, cria o ambiente
virtual, instala as dependências, copia o programa para `C:\GeradorDANFE` e cria os atalhos.
Detalhes em [`instalador/LEIA-ME_INSTALACAO.txt`](instalador/LEIA-ME_INSTALACAO.txt).

### Manual

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Na primeira execução o arquivo `config/config.json` é criado a partir de
`config/config.exemplo.json`.

### Teste sem certificado

```
copy exemplos\nota_exemplo_homologacao.xml xml_manual\
copy exemplos\chaves_exemplo.txt entrada\
python executar.py
```

O PDF sai em `saida/lote_.../pdf/` com a tarja "SEM VALOR FISCAL" (nota fictícia de homologação).

## Configuração

Tudo fica em `config/config.json`. Os campos principais:

| Campo | Descrição |
|---|---|
| `ambiente` | `producao` ou `homologacao` |
| `uf_autor` | UF da empresa, usada quando o certificado não tem `uf` própria |
| `certificados` | lista de certificados: `arquivo`, `senha` ou `senha_variavel_ambiente`, `cnpj` e `uf` (opcionais) |
| `sql_server.servidor` / `banco` | servidor (ex.: `SERVIDOR\SQLEXPRESS` ou `10.0.0.10,1433`) e banco do ERP |
| `sql_server.autenticacao_windows` | `true` usa o login do Windows; `false` usa `usuario` e senha |
| `sql_server.consulta_carga` | SELECT que devolve o XML das notas de uma carga, com `?` no lugar do número |
| `sql_server.consulta` / `ativo` | SELECT que devolve o XML de uma chave; com `ativo: true`, usado no modo por chaves |
| `consultar_protocolo` | busca o protocolo na SEFAZ quando o XML não o tem (padrão `true`) |
| `gerar_sem_protocolo` | gera com tarja em vez de registrar erro quando não há protocolo (padrão `false`) |
| `incluir_canceladas_no_pdf_da_carga` | inclui notas canceladas no PDF da carga (padrão `false`) |
| `pastas_xml` | pastas extras onde procurar XMLs |
| `manifestar_ciencia_automaticamente` | registra a Ciência da Operação quando a SEFAZ só entrega o resumo |
| `intervalo_entre_consultas_segundos`, `intervalo_consulta_protocolo_segundos` | pausa entre consultas à SEFAZ |
| `tentativas_conexao`, `timeout_segundos` | repetição em falhas de rede |
| `verificar_ssl` | `true`, `false` ou caminho de um `.pem` com a cadeia ICP-Brasil |
| `logo_emitente` | imagem PNG/JPG para o quadro do emitente |
| `nome_arquivo_pdf` | padrão do nome; aceita `{chave}`, `{numero}`, `{serie}`, `{cnpj}` |
| `remover_do_texto_produto` | expressões regulares removidas das informações adicionais do produto |
| `urls`, `urls_consulta_protocolo` | substituem os endereços padrão da SEFAZ (ver `src/danfe_app/sefaz.py`) |

A consulta da carga depende do banco de cada ERP. Ela precisa ter exatamente um `?` e devolver
o XML da NF-e na primeira coluna. Exemplo:

```sql
SELECT XML_DA_NOTA FROM TABELA_NOTAS WHERE NUMERO_CARGA = ? ORDER BY NUMERO_NOTA
```

### Senhas

Evite gravar senhas no `config.json`. As opções, em ordem de prioridade:

1. Campo `senha` no config (não recomendado).
2. Variável de ambiente indicada em `senha_variavel_ambiente`.
3. Arquivo protegido pelo Windows em `config/*.dat`, gravado pela tela de configurações ou por:

```
python executar.py --salvar-senha
python executar.py --salvar-senha-sql
```

O arquivo `.dat` só pode ser lido pelo mesmo usuário, no mesmo computador.

## Uso

### Interface gráfica

Abra `app_gui.pyw` (ou o atalho do instalador).

- **Gerar DANFEs**: informe o número da carga (várias separadas por espaço ou vírgula) e tecle Enter.
- **Cargas geradas**: histórico dos PDFs para abrir ou reimprimir.
- **Configurações**: certificado, senha e conexão com o banco.

### Linha de comando

```
python executar.py --carga 1234            gera saida/cargas/CARGA_1234.pdf
python executar.py --carga 1234 1235       várias cargas
python executar.py                         processa os .txt/.csv de entrada/
python executar.py --reprocessar           reprocessa as chaves com erro do último lote
python executar.py --testar-certificado    confere senha, CNPJ e validade
python executar.py --testar-banco          testa a conexão com o SQL Server
```

`GERAR_CARGA.bat` e `GERAR_DANFE.bat` são atalhos para os dois primeiros modos.

### Saída

```
saida/
├── cargas/CARGA_1234.pdf            PDF para impressão (ou CARGA_1234_INCOMPLETA.pdf)
└── carga_1234_AAAAMMDD_HHMMSS/      ou lote_AAAAMMDD_HHMMSS/
    ├── pdf/                         DANFEs individuais
    ├── xml/                         XML de cada nota
    ├── relatorio.csv                status de todas as chaves
    ├── ERROS.csv                    motivo e ação sugerida para cada erro
    ├── REPROCESSAR.txt              chaves que podem ser tentadas de novo
    └── log.txt
```

Nenhum erro interrompe o lote. Se alguma nota de uma carga falhar, o PDF recebe o sufixo
`_INCOMPLETA`; depois de corrigir, gere a carga de novo (as notas já prontas vêm do cache).

## Como o XML é obtido

1. `acervo_xml/`: cache dos XMLs já completos com protocolo.
2. `xml_manual/` e `pastas_xml`: XMLs locais, com qualquer nome.
3. SQL Server (por carga, ou por chave com `sql_server.ativo`).
4. SEFAZ, pela Distribuição DF-e, usando o certificado de um CNPJ que participa da nota.

Quando o XML não tem `protNFe`, o protocolo é consultado na SEFAZ autorizadora e anexado sem
alterar a NF-e assinada.

## Códigos de erro

| Código | Significado |
|---|---|
| `CHAVE_INVALIDA` | tamanho, UF, mês ou dígito verificador incorreto |
| `MODELO_NAO_SUPORTADO` | chave de NFC-e (65) ou de outro modelo |
| `SEM_PROTOCOLO` | XML sem protocolo e consulta à SEFAZ sem sucesso |
| `NAO_AUTORIZADA` | a SEFAZ não encontrou a nota autorizada (cStat 217) |
| `DENEGADA` | uso denegado |
| `SQL` | falha na conexão ou na consulta ao SQL Server |
| `NOTA_PROPRIA` | nota emitida pela própria empresa sem XML local (a SEFAZ não devolve ao emitente, cStat 641) |
| `NAO_LOCALIZADA` | a SEFAZ não entregou a nota ao CNPJ do certificado |
| `APENAS_RESUMO` | a empresa é destinatária e falta a Ciência da Operação |
| `CONSUMO_INDEVIDO` | rejeição 656; aguarde cerca de 1 hora |
| `NAO_CONSULTADA` | não consultada porque houve 656 na mesma execução |
| `CERTIFICADO`, `SEM_CERTIFICADO`, `CERTIFICADO_RECUSADO` | arquivo, senha, validade ou CNPJ do certificado |
| `FALHA_CONEXAO`, `FALHA_SSL`, `FALHA_SEFAZ`, `RESPOSTA_INVALIDA` | rede, SSL ou instabilidade da SEFAZ |
| `XML_INVALIDO`, `ERRO_PDF` | XML ilegível ou falha ao montar o PDF (detalhes no `log.txt`) |

## Observações sobre a SEFAZ

- A Distribuição DF-e só entrega notas em que o CNPJ do certificado é emitente, destinatário,
  transportador ou autorizado no `autXML`. Para notas de terceiros, use `xml_manual/`.
- A Ciência da Operação (evento 210210) é um registro oficial; por isso vem desligada.
- Consultas em excesso geram a rejeição 656. O programa espera entre consultas, guarda os XMLs
  em cache e suspende as consultas da execução ao receber 656.
- Os servidores das SEFAZ usam a cadeia ICP-Brasil. Na primeira consulta o programa baixa a
  cadeia oficial do ITI e monta `config/cadeia_ssl.pem`.
- O XML guardado no banco do ERP pode não ser idêntico, byte a byte, ao XML assinado. Para
  enviar o XML oficial ao cliente, use o arquivo do emissor.

## Estrutura

```
├── app_gui.pyw              interface gráfica
├── executar.py              linha de comando
├── config/                  config.exemplo.json (config.json e senhas ficam fora do Git)
├── certificados/            certificados A1 (fora do Git)
├── entrada/                 .txt/.csv com chaves ou XMLs exportados do SELECT
├── xml_manual/              XMLs locais
├── acervo_xml/              cache de XMLs
├── saida/                   PDFs e relatórios
├── exemplos/                nota fictícia de homologação
├── instalador/              instalador para Windows
└── src/danfe_app/
    ├── processador.py       orquestração dos lotes e das cargas
    ├── danfe_pdf.py         leitura do XML e desenho do DANFE (ReportLab)
    ├── sefaz.py             web services da SEFAZ e cadeia SSL
    ├── fonte_sql.py         leitura de XMLs do SQL Server e de exportações
    ├── chaves.py            leitura e validação de chaves de acesso
    ├── certificado.py       certificado A1
    ├── segredo.py           senhas protegidas com DPAPI
    ├── servico.py           operações usadas pela interface
    └── trava.py             impede duas execuções simultâneas
```

## Limitações

- Apenas NF-e modelo 55, DANFE retrato. NFC-e e DANFE paisagem não são suportados.
- Gravação protegida de senhas apenas no Windows.
- O layout segue o Manual de Orientação do Contribuinte, mas não substitui a validação
  do DANFE pela área fiscal da empresa.
