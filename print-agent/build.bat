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
rem  --console (padrao, nao passado explicitamente): a janela preta CONTINUA
rem  visivel de proposito. No dia da instalacao alguem precisa ver na tela
rem  que conectou e que a comanda saiu. Janela escondida vira depuracao as
rem  cegas. Se um dia virar icone de bandeja, e --noconsole + uma bandeja de
rem  verdade, nao so o flag.
rem ---------------------------------------------------------------------

cd /d "%~dp0"

echo Limpando build anterior...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Gerando o executavel...
py -m PyInstaller --onedir --name RapidexImpressao print_agent/__main__.py
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
