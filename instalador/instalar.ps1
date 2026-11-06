# Instalador do Gerador de DANFE.
# Instala o Python (se necessário) e as bibliotecas, copia o programa e cria os atalhos.
# Pode ser executado de novo para atualizar: config, certificados, senhas gravadas e
# DANFEs já gerados são mantidos.
param([string]$Destino = "C:\GeradorDANFE")

$ErrorActionPreference = "Continue"   # erros tratados por $LASTEXITCODE / -ErrorAction Stop
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Origem = Split-Path -Parent $MyInvocation.MyCommand.Path
# Pacote ZIP: programa na pasta "app" ao lado do instalador. Repositório: pasta acima.
$App = Join-Path $Origem "app"
if (-not (Test-Path $App)) { $App = Split-Path -Parent $Origem }
$PyVersao = "3.12.10"
$PyUrl = "https://www.python.org/ftp/python/$PyVersao/python-$PyVersao-amd64.exe"

function Etapa($t)  { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok($t)     { Write-Host "    [OK] $t" -ForegroundColor Green }
function Aviso($t)  { Write-Host "    [!]  $t" -ForegroundColor Yellow }
function Falha($t)  { Write-Host ""; Write-Host "    [ERRO] $t" -ForegroundColor Red; Write-Host ""; Read-Host "Pressione Enter para sair"; exit 1 }

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "      Instalador - Gerador de DANFE" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $App "app_gui.pyw"))) { Falha "Arquivos do programa não encontrados. Extraia o pacote inteiro antes de executar." }

# 1. Pasta
Etapa "Pasta de instalação"
try {
    New-Item -ItemType Directory -Force -Path $Destino -ErrorAction Stop | Out-Null
    $teste = Join-Path $Destino ".teste_escrita"
    Set-Content $teste "ok" -ErrorAction Stop; Remove-Item $teste -ErrorAction SilentlyContinue
} catch {
    $Destino = Join-Path $env:LOCALAPPDATA "GeradorDANFE"
    New-Item -ItemType Directory -Force -Path $Destino | Out-Null
    Aviso "Sem permissão em C:\. Usando $Destino"
}
$Atualizacao = Test-Path (Join-Path $Destino "config\config.json")
Ok $Destino
if ($Atualizacao) { Ok "Instalação existente encontrada: será ATUALIZADA (configurações mantidas)" }

# 2. Arquivos
Etapa "Copiando arquivos do programa"
& robocopy "$App" "$Destino" /E /NFL /NDL /NJH /NJS /NP `
    /XD .git .venv .vs __pycache__ instalador certificados entrada xml_manual acervo_xml saida `
    /XF config.json *.dat *.lock *.pfx *.p12 | Out-Null
if ($LASTEXITCODE -ge 8) { Falha "Falha ao copiar arquivos (robocopy código $LASTEXITCODE)." }
$cfgDestino = Join-Path $Destino "config\config.json"
if (-not (Test-Path $cfgDestino)) {
    $cfgOrigem = Join-Path $App "config\config.json"
    if (-not (Test-Path $cfgOrigem)) { $cfgOrigem = Join-Path $App "config\config.exemplo.json" }
    Copy-Item $cfgOrigem $cfgDestino
}
New-Item -ItemType Directory -Force -Path (Join-Path $Destino "certificados") | Out-Null
$pfx = Get-ChildItem -Path $Origem -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in ".pfx", ".p12" }
foreach ($c in $pfx) {
    Copy-Item $c.FullName (Join-Path $Destino "certificados") -Force
    Ok "Certificado copiado: $($c.Name)"
}
Ok "Arquivos copiados"

# 3. Python
Etapa "Python"
function Testar-Python([string]$exe, [string[]]$extra) {
    try {
        $v = & $exe @extra -c "import sys,tkinter,venv,ssl;print('%d.%d' % sys.version_info[:2])" 2>$null
        $v = $v | Select-Object -Last 1
        if ($LASTEXITCODE -eq 0 -and $v -and ([version]$v -ge [version]"3.10")) { return $v }
    } catch {}
    return $null
}
$Py = $null; $PyArgs = @()
$candidatos = @(@("py", @("-3.12")), @("py", @("-3")), @("python", @()))
$achados = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending
foreach ($f in $achados) { $candidatos += ,@($f.FullName, @()) }
foreach ($c in $candidatos) {
    $v = Testar-Python $c[0] $c[1]
    if ($v) { $Py = $c[0]; $PyArgs = $c[1]; Ok "Python $v encontrado ($($c[0]) $($c[1] -join ' '))"; break }
}
if (-not $Py) {
    Aviso "Python não encontrado. Baixando Python $PyVersao do site oficial (python.org)…"
    $inst = Join-Path $env:TEMP "python-$PyVersao-amd64.exe"
    try { Invoke-WebRequest -Uri $PyUrl -OutFile $inst -UseBasicParsing }
    catch { Falha "Não foi possível baixar o Python ($PyUrl). Verifique a internet ou instale manualmente e rode o instalador de novo." }
    Write-Host "    Instalando Python (pode levar alguns minutos)…"
    $p = Start-Process -FilePath $inst -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1 Include_pip=1 Include_test=0" -Wait -PassThru
    if ($p.ExitCode -ne 0) { Falha "A instalação do Python retornou o código $($p.ExitCode)." }
    $Py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (-not (Testar-Python $Py @())) { Falha "Python instalado, mas não foi encontrado em $Py." }
    Ok "Python $PyVersao instalado"
}

# 4. Ambiente e bibliotecas
Etapa "Bibliotecas do programa"
$Venv = Join-Path $Destino ".venv"
$VPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VPy)) {
    & $Py @PyArgs -m venv "$Venv"
    if ($LASTEXITCODE -ne 0) { Falha "Não foi possível criar o ambiente do programa." }
}
& $VPy -m pip install --disable-pip-version-check -q --upgrade pip *> $null
& $VPy -m pip install --disable-pip-version-check -q -r (Join-Path $Destino "requirements.txt")
if ($LASTEXITCODE -ne 0) { Falha "Falha ao instalar as bibliotecas (verifique internet/proxy e rode de novo)." }
& $VPy -c "import requests, lxml, cryptography, reportlab, pyodbc, tkinter" *> $null
if ($LASTEXITCODE -ne 0) { Falha "Bibliotecas instaladas, mas a verificação falhou." }
Ok "Bibliotecas instaladas"

# 5. Driver ODBC
Etapa "Driver do SQL Server (ODBC)"
$drv = (Get-ItemProperty "HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers" -ErrorAction SilentlyContinue).PSObject.Properties.Name
if ($drv -match "ODBC Driver 1[78] for SQL Server") { Ok "Encontrado: $(($drv -match 'ODBC Driver 1[78]') -join ', ')" }
elseif ($drv -contains "SQL Server") { Aviso "Usando o driver 'SQL Server' que vem com o Windows (funciona). Opcional: instalar o 'ODBC Driver 18 for SQL Server'." }
else { Aviso "Nenhum driver do SQL Server encontrado. Instale o 'ODBC Driver 18 for SQL Server' da Microsoft." }

# 6. Atalhos
Etapa "Atalhos"
$Shell = New-Object -ComObject WScript.Shell
$VPyw = Join-Path $Venv "Scripts\pythonw.exe"
$locais = @([Environment]::GetFolderPath("Desktop"), (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"))
foreach ($l in $locais) {
    $lnk = $Shell.CreateShortcut((Join-Path $l "Gerador de DANFE.lnk"))
    $lnk.TargetPath = $VPyw
    $lnk.Arguments = "`"$(Join-Path $Destino 'app_gui.pyw')`""
    $lnk.WorkingDirectory = $Destino
    $lnk.IconLocation = (Join-Path $Destino "recursos\danfe.ico")
    $lnk.Description = "Gera os DANFEs das cargas"
    $lnk.Save()
}
New-Item -ItemType Directory -Force -Path (Join-Path $Destino "saida\cargas") | Out-Null
$lnk = $Shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "DANFEs das Cargas.lnk"))
$lnk.TargetPath = (Join-Path $Destino "saida\cargas"); $lnk.Save()
Ok "Atalhos criados na Área de Trabalho e no Menu Iniciar"

# Fim
Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host "  Instalação concluída em $Destino" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
if (-not $Atualizacao -or -not (Test-Path (Join-Path $Destino "config\senha_certificado.dat"))) {
    Write-Host ""
    Write-Host "  Primeiro uso: na aba 'Configurações' do programa," -ForegroundColor Yellow
    Write-Host "  selecione o certificado (.pfx), informe a senha e" -ForegroundColor Yellow
    Write-Host "  confira/salve o banco de dados." -ForegroundColor Yellow
}
Write-Host ""
Start-Process -FilePath $VPyw -ArgumentList "`"$(Join-Path $Destino 'app_gui.pyw')`"" -WorkingDirectory $Destino
Read-Host "Pressione Enter para fechar"
