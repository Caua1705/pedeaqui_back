@echo off
setlocal enableextensions
chcp 1252 >nul

rem ---------------------------------------------------------------------
rem  Sobe a bancada do assistente: a API, o servidor da pagina e o
rem  navegador. Roda na SUA maquina, por duplo clique.
rem
rem  Sao duas janelas de proposito, e nao uma so em segundo plano: a da API
rem  e onde aparece o traceback, e a do http.server denuncia porta ocupada.
rem  Fechar qualquer uma das duas encerra a peca correspondente -- e assim
rem  que se para a bancada.
rem
rem  NADA DE POWERSHELL AQUI. O instalar.bat do print-agent chama o
rem  powershell com -ExecutionPolicy Bypass para criar um atalho; este nao
rem  precisa de nada disso, e nao ter a dependencia significa que o duplo
rem  clique funciona em maquina com politica restrita, sem pedir nada a
rem  ninguem. Se um dia faltar alguma coisa aqui, resolva em cmd puro.
rem
rem  A PAGINA NAO ABRE COM DUPLO CLIQUE. `file://` tem origem `null`, e a
rem  lista de origens do `main.py` so aceita origem conhecida: toda chamada
rem  morreria no CORS antes de sair. Por isso o http.server na 5500, que ja
rem  esta liberada la (junto da 5501 e da 5173).
rem
rem  E `localhost`, e nao o IP da maquina na rede, porque e a origem que o
rem  CORS do backend ja aceita.
rem ---------------------------------------------------------------------

cd /d "%~dp0"

set "BANCADA=%~dp0tools\bancada"
set "PAGINA=http://localhost:5500/bancada.html"

echo.
echo ======================================================================
echo   BANCADA DO ASSISTENTE
echo ======================================================================
echo.

rem --- O interpretador -------------------------------------------------
rem  O do venv primeiro, e nao o `py` do sistema: quem tem uvicorn e
rem  fastapi instalados e ele. Com o interpretador global, a janela da API
rem  abriria so para morrer em "No module named uvicorn".
set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
    echo AVISO: nao achei o venv em "%~dp0venv".
    echo        Vou tentar o Python do sistema. Se a janela da API fechar
    echo        sozinha reclamando de uvicorn, o venv e o que falta:
    echo.
    echo          py -m venv venv
    echo          venv\Scripts\python -m pip install -r requirements.lock.txt
    echo.
    set "PY=py"
    where py >nul 2>&1
    if errorlevel 1 set "PY=python"
)

rem --- O que precisa existir -------------------------------------------
if not exist "%BANCADA%\bancada.html" (
    echo ERRO: nao encontrei a pagina da bancada.
    echo.
    echo Ela deveria estar em: %BANCADA%
    echo.
    echo A bancada e versionada junto do backend desde 25/08/2026. Se a
    echo pasta sumiu, ela volta com: git checkout -- tools/bancada
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0.env" (
    echo AVISO: nao ha .env na raiz. A API precisa de DATABASE_URL e de
    echo        OPENAI_API_KEY para o /chat responder.
    echo.
)

rem --- Porta ocupada e' bancada que ja esta de pe ------------------------
rem  Sem esta checagem, a segunda execucao abriria uma janela que morre no
rem  ato com "address already in use" -- e some antes de dar para ler.
set "API_DE_PE="
for /f %%L in ('netstat -ano -p tcp ^| findstr /c:":8000 " ^| findstr /i "LISTENING" 2^>nul') do set "API_DE_PE=1"

set "SERVIDOR_DE_PE="
for /f %%L in ('netstat -ano -p tcp ^| findstr /c:":5500 " ^| findstr /i "LISTENING" 2^>nul') do set "SERVIDOR_DE_PE=1"

rem --- A API ------------------------------------------------------------
if defined API_DE_PE (
    echo [1/3] A porta 8000 ja esta ocupada. Reaproveitando o que esta de pe.
) else (
    echo [1/3] Subindo a API em http://localhost:8000 ...
    start "API - uvicorn :8000" /d "%~dp0." cmd /k "%PY%" -m uvicorn main:app --reload
)

rem --- O servidor da pagina --------------------------------------------
if defined SERVIDOR_DE_PE (
    echo [2/3] A porta 5500 ja esta ocupada. Reaproveitando o que esta de pe.
) else (
    echo [2/3] Servindo tools\bancada em http://localhost:5500 ...
    start "Bancada - http.server :5500" /d "%BANCADA%" cmd /k "%PY%" -m http.server 5500
)

rem --- O navegador ------------------------------------------------------
rem  A espera e' para o http.server ter atendido a porta antes de o
rem  navegador bater nela; sem ela, a primeira carga cai em "recusou a
rem  conexao" e alguem recarrega a mao achando que quebrou. A API demora
rem  mais que isto e nao faz falta: quem a chama e' o botao, nao a carga.
echo [3/3] Abrindo o navegador...
timeout /t 3 /nobreak >nul 2>&1
start "" "%PAGINA%"

echo.
echo ======================================================================
echo   NO AR
echo ======================================================================
echo.
echo   Pagina:  %PAGINA%
echo   API:     http://localhost:8000  ^(documentacao em /docs^)
echo.
echo   Duas janelas abriram junto com esta. Feche-as para parar a bancada;
echo   fechar ESTA aqui nao para nada.
echo.
echo   Se a janela da API disser "Configuracao invalida", foi o
echo   startup_checks: ele confere o .env no lifespan, lista o que falta e
echo   recusa subir de proposito. A janela fica aberta com a lista.
echo.
echo   O que a tela mede, e o que ela nao mede, esta em
echo   tools\bancada\LEIA-ME.md.
echo.
pause
endlocal
