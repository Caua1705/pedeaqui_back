"""O som de pedido novo, tocado por quem imprime a comanda.

Hoje o alerta sonoro depende do painel estar aberto no navegador da maquina
do balcao. Isso falha de tres jeitos que ja acontecem: a aba fechou, o
navegador suspendeu a aba em segundo plano, ou o computador esta com o painel
aberto numa tela que ninguem olha. **O agente nao tem nenhum desses
problemas**: ele esta sempre rodando, e ja recebe o aviso antes do painel —
e ele quem imprime.

Toca DEPOIS da comanda sair, e nao quando o pedido chega. A ordem importa: o
som e o sinal de "tem papel na impressora, va pegar". Tocar antes de imprimir
mandaria alguem ate a impressora para achar bobina vazia quando a via falha.

Nao bloqueia: o som sai numa thread propria. `winsound.Beep` e `PlaySound`
sincrono seguram quem os chama, e segurar o laco do agente por um segundo a
cada pedido atrasaria a via do proximo.
"""

import logging
import threading
from pathlib import Path


logger = logging.getLogger(__name__)

# Tres apitos curtos e agudos. Escolhidos para atravessar barulho de cozinha:
# frequencia alta corta melhor que grave, e a repeticao e o que distingue de
# um bipe qualquer do Windows.
#
# `winsound.Beep` e nao um som de sistema ("SystemExclamation") de proposito:
# esquema de som em "Nenhum sons" — comum em maquina de balcao que alguem
# achou barulhenta — deixa os sons de sistema MUDOS, sem erro nenhum, e o
# alerta simplesmente nunca mais tocaria.
BEEP_FREQUENCY_HZ = 1200
BEEP_DURATION_MS = 180
BEEP_COUNT = 3


def play_new_order(sound_file: Path | None = None) -> None:
    """Alerta de comanda impressa, sem segurar quem chamou."""
    thread = threading.Thread(
        target=_play, args=(sound_file,), name="som-pedido", daemon=True
    )
    thread.start()


def _play(sound_file: Path | None) -> None:
    try:
        import winsound
    except ImportError:  # pragma: no cover - depende do SO
        logger.debug("winsound indisponivel: sem som de pedido novo")
        return

    try:
        if sound_file is not None:
            _play_file(winsound, sound_file)
            return
        _beep(winsound)
    except Exception as exc:  # pragma: no cover - depende da placa de som
        # Maquina sem placa de som, sem alto-falante, ou .wav corrompido.
        # Nada disso pode atrapalhar a impressao: o som e conforto, a comanda
        # e a operacao.
        logger.debug("nao foi possivel tocar o alerta: %s", exc)


def _play_file(winsound, sound_file: Path) -> None:
    """Toca o .wav escolhido no config.ini.

    `SND_FILENAME` sem `SND_ASYNC`: ja estamos numa thread propria, e o
    sincrono e o unico que devolve erro quando o arquivo nao e um WAV valido.
    Assincrono, um caminho errado no config.ini seria silencio sem uma linha
    no log.
    """
    winsound.PlaySound(str(sound_file), winsound.SND_FILENAME)


def _beep(winsound) -> None:
    for _ in range(BEEP_COUNT):
        winsound.Beep(BEEP_FREQUENCY_HZ, BEEP_DURATION_MS)
