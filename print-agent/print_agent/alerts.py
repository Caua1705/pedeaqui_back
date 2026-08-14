"""Caixas de aviso do Windows, para o que EXIGE acao do lojista.

Este modulo existe por causa do `--noconsole`. Enquanto havia janela preta,
"configuracao incompleta", "credencial invalida" e "impressora nao
encontrada" apareciam na tela e alguem lia. Sem janela, as mesmas mensagens
sobrariam so no `rapidex-impressao.log` — um arquivo que ninguem abre
sozinho — e o programa ficaria vivo, sem imprimir, em silencio absoluto. E
exatamente o modo de falhar que o tratamento de config existe para evitar.

**O criterio para uma mensagem virar caixa e estreito: ela exige que uma
pessoa faca alguma coisa AGORA.** Reconexao, heartbeat perdido, replay
truncado e via reenviada continuam so no log — sao coisas que o proprio
programa resolve, e uma caixa por reconexao treinaria o lojista a clicar OK
sem ler, que e como o aviso que importa deixa de ser lido.

Nao usa tkinter: o `MessageBoxW` ja esta no Windows, nao acrescenta nada ao
pacote do PyInstaller e nao precisa de uma janela raiz viva para funcionar.
"""

import logging
import threading


logger = logging.getLogger(__name__)

# Flags do MessageBoxW (winuser.h).
_MB_OK = 0x00000000
_MB_ICONERROR = 0x00000010
_MB_ICONWARNING = 0x00000030
_MB_ICONINFORMATION = 0x00000040
# SETFOREGROUND + TOPMOST porque o programa nao tem janela: sem os dois, a
# caixa nasce atras do que estiver aberto no balcao e ninguem ve.
_MB_SETFOREGROUND = 0x00010000
_MB_TOPMOST = 0x00040000

TITLE = "Rapidex Impressao"

# Chaves ja avisadas nesta execucao. Ver `show_once`.
_already_shown: set[str] = set()


def show_error(message: str, title: str = TITLE) -> None:
    """Caixa de erro que SEGURA quem chamou ate o OK.

    Para o caminho em que o programa esta encerrando de qualquer jeito
    (configuracao invalida, credencial recusada): a mensagem e a ultima coisa
    que acontece, e sumir da tela antes de alguem ler nao serviria de nada.
    """
    _message_box(message, title, _MB_ICONERROR)


def show_info(message: str, title: str = TITLE) -> None:
    """Caixa de instrucao que SEGURA quem chamou ate o OK.

    Icone diferente do `show_error` porque a diferenca importa para quem esta
    olhando a tela no dia da instalacao: "falta preencher o config.ini" e uma
    tarefa, "o programa quebrou" e um chamado.
    """
    _message_box(message, title, _MB_ICONINFORMATION)


def show_warning_async(message: str, title: str = TITLE) -> None:
    """Caixa de aviso que NAO segura quem chamou.

    Para o que acontece com o agente rodando. Uma caixa modal na thread do
    laco pararia a impressao de todos os outros setores ate alguem clicar OK
    — e o balcao pode estar vazio as 3h da manha.
    """
    thread = threading.Thread(
        target=_message_box,
        args=(message, title, _MB_ICONWARNING),
        name="alerta",
        daemon=True,
    )
    thread.start()


def show_warning_once(key: str, message: str, title: str = TITLE) -> None:
    """Avisa uma vez por `key` nesta execucao.

    A impressora sem papel falha a cada pedido, e o agente reconecta a cada
    15 minutos. Sem esta memoria, um restaurante movimentado acumularia
    dezenas de caixas empilhadas na tela do balcao — e a ultima cobriria a
    primeira, que era a que dizia qual impressora.

    Por execucao, e nao por hora: quem resolveu o problema fecha e abre o
    programa pela bandeja, e ai o aviso volta a valer.
    """
    if key in _already_shown:
        logger.debug("aviso '%s' ja mostrado nesta execucao", key)
        return
    _already_shown.add(key)
    show_warning_async(message, title)


def _message_box(message: str, title: str, icon: int) -> None:
    """Chama o MessageBoxW do Windows.

    Falhar aqui nunca pode derrubar o agente: fora do Windows (teste, maquina
    de quem desenvolve) o `ctypes.windll` nem existe, e uma sessao sem area
    de trabalho — tarefa agendada rodando como servico — recusa a janela. Nos
    dois casos o log continua tendo a mensagem inteira.
    """
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, message, title, _MB_OK | icon | _MB_SETFOREGROUND | _MB_TOPMOST
        )
    except Exception:
        logger.debug("nao foi possivel mostrar a caixa de aviso: %s", message)
