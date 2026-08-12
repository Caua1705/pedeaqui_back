@echo off
setlocal enableextensions
chcp 1252 >nul

rem ---------------------------------------------------------------------
rem  Desinstalador do Rapidex Impressao.
rem
rem  Remove o atalho de inicializacao, fecha o programa e apaga a pasta.
rem
rem  O log e o config.ini vao junto: e uma DESinstalacao, e deixar a senha
rem  do lojista num arquivo de texto na maquina depois de sair e pior que
rem  perder o historico. Quem quiser guardar o log copia antes -- o caminho
rem  aparece na tela abaixo antes de qualquer coisa ser apagada.
rem ---------------------------------------------------------------------

set "DESTINO=%LOCALAPPDATA%\Rapidex Impressao"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "ATALHO=%STARTUP%\Rapidex Impressao.lnk"
set "EXE=RapidexImpressao.exe"

echo.
echo ======================================================================
echo   DESINSTALADOR DO RAPIDEX IMPRESSAO
echo ======================================================================
echo.
echo   Vai ser removido:
echo     %DESTINO%
echo     %ATALHO%
echo.
echo   Isso inclui o registro (rapidex-impressao.log) e a configuracao.
echo   Se quiser guardar o log, copie a pasta antes de continuar.
echo.
pause

echo.
echo [1/3] Fechando o programa...
taskkill /f /im "%EXE%" >nul 2>&1
if errorlevel 1 (
    echo       Nao estava rodando.
) else (
    echo       Fechado.
)

echo [2/3] Removendo o atalho de inicializacao...
if exist "%ATALHO%" (
    del /f /q "%ATALHO%"
    if exist "%ATALHO%" (
        echo AVISO: nao consegui remover "%ATALHO%".
        echo        Apague a mao: tecle Win+R, digite shell:startup e Enter.
    ) else (
        echo       Removido.
    )
) else (
    echo       Nao havia atalho.
)

echo [3/3] Removendo a pasta do programa...
if exist "%DESTINO%" (
    rmdir /s /q "%DESTINO%"
    if exist "%DESTINO%" (
        echo AVISO: a pasta nao pode ser removida por inteiro.
        echo        Provavelmente o programa ainda estava aberto. Feche-o e
        echo        apague a mao: %DESTINO%
    ) else (
        echo       Removida.
    )
) else (
    echo       Nao havia pasta.
)

echo.
echo ======================================================================
echo   DESINSTALADO
echo ======================================================================
echo.
pause
endlocal
