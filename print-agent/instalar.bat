@echo off
setlocal enableextensions
chcp 1252 >nul

rem ---------------------------------------------------------------------
rem  Instalador do Rapidex Impressao.
rem
rem  Roda a partir do pendrive, sem perguntar nada. Nao precisa de
rem  administrador: instala no perfil do usuario (%LOCALAPPDATA%) e cria o
rem  atalho na Inicializacao DELE. Pedir administrador so traria o UAC para
rem  o meio da instalacao e nao daria nada em troca -- o agente nao precisa
rem  de privilegio nenhum para falar com a impressora do proprio usuario.
rem
rem  IMPORTANTE: um config.ini que ja exista NAO e sobrescrito. Reinstalar
rem  por cima (atualizar a versao) nao pode apagar a senha e o nome da
rem  impressora que alguem ja configurou.
rem ---------------------------------------------------------------------

set "ORIGEM=%~dp0"
set "PROGRAMA=%ORIGEM%programa"
set "DESTINO=%LOCALAPPDATA%\Rapidex Impressao"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "ATALHO=%STARTUP%\Rapidex Impressao.lnk"
set "EXE=RapidexImpressao.exe"

echo.
echo ======================================================================
echo   INSTALADOR DO RAPIDEX IMPRESSAO
echo ======================================================================
echo.

if not exist "%PROGRAMA%\%EXE%" (
    echo ERRO: nao encontrei o arquivo %EXE%.
    echo.
    echo Ele deveria estar em: %PROGRAMA%
    echo.
    echo As duas causas, nesta ordem:
    echo.
    rem Nada de parenteses no texto de dentro de um bloco if: o cmd le o ")"
    rem como fim do bloco e morre com "e foi inesperado neste momento",
    rem antes mesmo de o if ser avaliado.
    echo   1. O antivirus apagou o arquivo. Faca o item 1 do INSTALACAO.md,
    echo      que libera a pasta no antivirus, e rode este instalador de novo.
    echo   2. A pasta foi copiada pela metade. Copie do pendrive a pasta
    echo      INTEIRA e rode o instalar.bat de dentro dela.
    echo.
    pause
    exit /b 1
)

echo [1/4] Criando a pasta do programa...
if not exist "%DESTINO%" mkdir "%DESTINO%"
if errorlevel 1 (
    echo ERRO: nao foi possivel criar "%DESTINO%".
    pause
    exit /b 1
)

echo [2/4] Copiando o programa...
rem Se o programa ja estiver rodando, a copia falha com o arquivo em uso.
taskkill /f /im "%EXE%" >nul 2>&1

rem A _internal antiga sai antes: o xcopy sobrescreve o que tem nome igual,
rem mas nao apaga o que sumiu entre uma versao e outra, e DLL orfa de versao
rem velha ao lado da nova e defeito que so aparece na maquina do lojista.
rem So a _internal -- o config.ini e o log estao na raiz do DESTINO e
rem precisam sobreviver a uma reinstalacao.
if exist "%DESTINO%\_internal" rmdir /s /q "%DESTINO%\_internal"

xcopy /e /i /y /q "%PROGRAMA%" "%DESTINO%" >nul
if errorlevel 1 (
    echo ERRO: nao foi possivel copiar o programa para "%DESTINO%".
    pause
    exit /b 1
)

if exist "%DESTINO%\config.ini" (
    echo       Configuracao ja existente foi MANTIDA.
) else (
    if exist "%ORIGEM%config.ini" (
        copy /y "%ORIGEM%config.ini" "%DESTINO%\config.ini" >nul
        echo       Configuracao copiada do pendrive.
    ) else (
        echo       Sem config.ini: o programa vai criar um modelo ao abrir.
    )
)

echo [3/4] Configurando para iniciar junto com o Windows...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w = New-Object -ComObject WScript.Shell;" ^
  "$s = $w.CreateShortcut('%ATALHO%');" ^
  "$s.TargetPath = '%DESTINO%\%EXE%';" ^
  "$s.WorkingDirectory = '%DESTINO%';" ^
  "$s.Description = 'Agente de impressao de comandas do Rapidex';" ^
  "$s.Save()"
if errorlevel 1 (
    echo AVISO: nao consegui criar o atalho de inicializacao automatica.
    echo        O programa funciona, mas nao vai abrir sozinho depois de
    echo        reiniciar o computador.
) else (
    echo       Atalho criado na Inicializacao.
)

echo [4/4] Abrindo o programa...
start "" "%DESTINO%\%EXE%"

echo.
echo ======================================================================
echo   INSTALADO
echo ======================================================================
echo.
echo   Programa:      %DESTINO%\%EXE%
echo   Configuracao:  %DESTINO%\config.ini
echo   Registro:      %DESTINO%\rapidex-impressao.log
echo.
echo   O programa abriu numa janela propria. DEIXE ESSA JANELA ABERTA --
echo   e ela que imprime as comandas. Ela volta sozinha toda vez que o
echo   computador ligar.
echo.
echo   Se a janela pedir para preencher a configuracao, abra o arquivo
echo   config.ini acima no Bloco de Notas, preencha e abra o programa de
echo   novo pelo atalho na Inicializacao.
echo.
echo   Quando alguem disser que nao esta imprimindo, o arquivo que ajuda
echo   e o rapidex-impressao.log da pasta acima.
echo.
echo   Na mesma pasta ha uma subpasta _internal. Ela e parte do programa:
echo   se for apagada, ele para de abrir.
echo.
pause
endlocal
