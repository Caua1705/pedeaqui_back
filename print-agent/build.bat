@echo off
setlocal enableextensions
chcp 1252 >nul

rem ---------------------------------------------------------------------
rem  Gera o RapidexImpressao.exe. Roda na SUA maquina, nao na do lojista.
rem
rem  --onedir e NAO --onefile: o executavel de arquivo unico se auto-extrai
rem  numa pasta temporaria a cada abertura, que e o comportamento que o
rem  Windows Defender marca como suspeito. Ele apagava o .exe na frente do
rem  lojista e o instalar.bat morria com "nao encontrei o
rem  RapidexImpressao.exe". O --onedir nao se auto-extrai e reduz muito esse
rem  falso positivo. O custo e a pasta _internal ao lado do .exe, que o
rem  instalar.bat copia junto -- ninguem digita esse caminho a mao.
rem
rem  --noconsole: sem janela preta. O programa vive como icone na bandeja,
rem  ao lado do relogio (print_agent/tray.py). A janela foi embora porque
rem  ficou aberta no balcao de um restaurante de verdade: parece terminal de
rem  desenvolvedor, ocupa a barra de tarefas e qualquer um a fecha sem
rem  querer -- e fechar a janela era parar de imprimir.
rem
rem  O que a janela fazia e que precisou de substituto:
rem    - "conectou?"        -> a cor do icone e o balao da primeira conexao
rem    - "a comanda saiu?"  -> o som de pedido novo
rem    - "por que parou?"   -> caixa de aviso do Windows (print_agent/alerts.py)
rem  Sem esses tres, --noconsole seria so esconder o diagnostico.
rem
rem  ATENCAO ANTIVIRUS: executavel sem janela lembra malware, e o Defender ja
rem  apagou o --onefile deste projeto uma vez. Depois de gerar, COPIE a pasta
rem  dist\pendrive para outro lugar e confira se o .exe sobreviveu -- e o
rem  teste que reproduz o que acontece no pendrive do lojista.
rem
rem  --hidden-import pystray._win32: o pystray escolhe o backend em tempo de
rem  execucao, entao o PyInstaller nao ve esse import vasculhando o codigo. Sem
rem  a linha, o .exe gera sem erro nenhum e morre ao abrir na maquina do
rem  lojista com "no backend available".
rem ---------------------------------------------------------------------

cd /d "%~dp0"

echo Limpando build anterior...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Gerando o executavel...
py -m PyInstaller --onedir --noconsole --name RapidexImpressao ^
   --hidden-import pystray._win32 ^
   print_agent/__main__.py
if errorlevel 1 (
    echo.
    echo ERRO: a geracao falhou. PyInstaller esta instalado?
    echo   py -m pip install pyinstaller
    pause
    exit /b 1
)

echo.
echo Montando a pasta para o pendrive...
rem  O .exe e a _internal ficam juntos numa subpasta 'programa': quem espeta
rem  o pendrive precisa achar o instalar.bat de primeira, e no --onedir a
rem  raiz teria dezenas de DLLs em volta dele.
set "PACOTE=dist\pendrive"
mkdir "%PACOTE%"
xcopy /e /i /y /q "dist\RapidexImpressao" "%PACOTE%\programa" >nul
if errorlevel 1 (
    echo.
    echo ERRO: nao consegui montar a pasta do pendrive.
    pause
    exit /b 1
)
copy /y "instalar.bat"    "%PACOTE%\" >nul
copy /y "desinstalar.bat" "%PACOTE%\" >nul
copy /y "INSTALACAO.md"   "%PACOTE%\" >nul

echo.
echo ======================================================================
echo   PRONTO
echo ======================================================================
echo.
echo   Copie para o pendrive a pasta:
echo   %CD%\%PACOTE%
echo.
echo   Nela estao: a pasta 'programa', o instalar.bat, o desinstalar.bat e
echo   as instrucoes. Nao ha config.ini: o programa cria o modelo na
echo   primeira execucao, ja na maquina certa.
echo.
echo   Copie a pasta INTEIRA para o pendrive. So o .exe nao roda: ele
echo   depende da _internal que esta ao lado dele.
echo.
pause
endlocal
