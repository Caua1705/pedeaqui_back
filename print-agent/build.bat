@echo off
setlocal enableextensions
chcp 1252 >nul

rem ---------------------------------------------------------------------
rem  Gera o RapidexImpressao.exe. Roda na SUA maquina, nao na do lojista.
rem
rem  --onefile: um arquivo so, para caber num pendrive e nao ter DLL solta
rem             para alguem perder no caminho.
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
py -m PyInstaller --onefile --name RapidexImpressao print_agent/__main__.py
if errorlevel 1 (
    echo.
    echo ERRO: a geracao falhou. PyInstaller esta instalado?
    echo   py -m pip install pyinstaller
    pause
    exit /b 1
)

echo.
echo Montando a pasta para o pendrive...
set "PACOTE=dist\pendrive"
mkdir "%PACOTE%"
copy /y "dist\RapidexImpressao.exe" "%PACOTE%\" >nul
copy /y "instalar.bat"              "%PACOTE%\" >nul
copy /y "desinstalar.bat"           "%PACOTE%\" >nul
copy /y "INSTALACAO.md"             "%PACOTE%\" >nul

echo.
echo ======================================================================
echo   PRONTO
echo ======================================================================
echo.
echo   Copie para o pendrive a pasta:
echo   %CD%\%PACOTE%
echo.
echo   Nela estao: o programa, o instalar.bat, o desinstalar.bat e as
echo   instrucoes. Nao ha config.ini: o programa cria o modelo na primeira
echo   execucao, ja na maquina certa.
echo.
pause
endlocal
