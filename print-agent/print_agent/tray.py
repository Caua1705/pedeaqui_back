"""O icone na bandeja do Windows, ao lado do relogio.

Substitui a janela preta de console. O motivo nao e estetica: o programa foi
instalado num restaurante de verdade e a janela ficou aberta no balcao, no
meio do movimento. Ela parece terminal de desenvolvedor, ocupa a barra de
tarefas e **qualquer pessoa a fecha sem querer** — e fechar a janela e parar
de imprimir, sem ninguem perceber ate o cliente reclamar do pedido que a
cozinha nunca viu.

## Quem roda onde

O `pystray` precisa da thread PRINCIPAL: no Windows ele cria uma janela
escondida e fica bombeando a fila de mensagens dela, que e o que faz o icone
responder a clique. Entao a divisao e:

    thread principal  ->  o icone (`icon.run`)
    thread de trabalho ->  o laco do agente (`agent.run`)

E nao o contrario. Com o icone numa thread secundaria, o menu abre e nao
responde ao clique em algumas maquinas — depende de quem criou a janela.

## O estado pela cor

    verde    conectado, escutando pedidos
    ambar    sem conexao, tentando de novo sozinho
    vermelho parou; so volta com alguem editando o config.ini

A cor responde "esta funcionando?" de longe. O texto exato fica no tooltip
(passar o mouse) e no menu, porque cor sozinha nao diz O QUE fazer — e quem
esta no balcao as 20h precisa das duas coisas.
"""

import logging
import threading

import pystray
from PIL import Image, ImageDraw

from print_agent import alerts, sound
from print_agent.agent import AgentListener, PrintAgent
from print_agent.config import Config
from print_agent.paths import open_folder


logger = logging.getLogger(__name__)

# Quanto tempo o "Sair" espera o laco terminar a via em andamento antes de o
# processo morrer de qualquer jeito.
#
# Dez segundos cobre com folga uma comanda inteira saindo na bobina, que e o
# que `PrintAgent.stop()` existe para nao cortar no meio. Nao cobre — de
# proposito — a leitura do socket, que pode ficar ate 60s esperando um byte
# que nao vem: quem clicou em "Sair" nao pode ver o programa continuar na
# lista de processos por um minuto. A thread e daemon justamente para isso:
# passado o prazo, o interpretador termina e a leva junto.
EXIT_GRACE_SECONDS = 10.0

GREEN = "#1f9d55"
AMBER = "#d97706"
RED = "#c81e1e"

TOOLTIP_CONNECTED = "Rapidex Impressao - conectado"
TOOLTIP_OFFLINE = "Rapidex Impressao - sem conexao, tentando de novo"
TOOLTIP_STARTING = "Rapidex Impressao - iniciando"
TOOLTIP_FATAL = "Rapidex Impressao - PARADO, precisa de atencao"


class Tray(AgentListener):
    """O icone, o menu e a traducao de "o que o agente esta fazendo" em cor."""

    def __init__(self, config: Config, agent: PrintAgent):
        self.config = config
        self.agent = agent
        self.fatal = False
        self._worker: threading.Thread | None = None
        self._announced_connection = False
        self._icon = pystray.Icon(
            name="rapidex-impressao",
            icon=_dot(AMBER),
            title=TOOLTIP_STARTING,
            menu=pystray.Menu(
                pystray.MenuItem("Abrir a pasta do registro", self._open_log_folder),
                pystray.MenuItem("Abrir a pasta da configuracao", self._open_config_folder),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Sair", self._quit),
            ),
        )

    def run(self, work) -> None:
        """Sobe `work` numa thread e fica com o icone ate alguem sair.

        `setup=` e o gancho do pystray que roda DEPOIS de o icone existir na
        bandeja. Comecar o agente antes disso deixaria uma janela em que ele
        ja esta imprimindo e ainda nao ha icone nenhum para clicar — e o
        "Sair" nao existiria.
        """

        def worker():
            try:
                work()
            finally:
                # O agente saiu do laco sozinho (credencial fatal). Sem esta
                # linha, o icone continuaria na bandeja de um programa que
                # nao imprime mais nada — que e o pior estado possivel,
                # porque parece que esta funcionando.
                self._icon.stop()

        self._worker = threading.Thread(target=worker, name="print-agent", daemon=True)
        self._icon.run(setup=lambda icon: self._worker.start())
        self._wait_for_worker()

    def _wait_for_worker(self) -> None:
        if self._worker is None:
            return
        self._worker.join(timeout=EXIT_GRACE_SECONDS)
        if self._worker.is_alive():
            logger.warning(
                "o laco nao terminou em %.0fs (provavelmente esperando o "
                "servidor); encerrando assim mesmo",
                EXIT_GRACE_SECONDS,
            )

    # ---- o que o agente avisa (AgentListener) ------------------------------

    def on_connected(self) -> None:
        self._show(GREEN, TOOLTIP_CONNECTED)
        self._announce_once()

    def on_disconnected(self, reason: str) -> None:
        self._show(AMBER, TOOLTIP_OFFLINE)

    def on_fatal(self, reason: str) -> None:
        self.fatal = True
        self._show(RED, TOOLTIP_FATAL)
        # Bloqueante, e nao `show_warning_async`: o programa esta encerrando
        # atras desta caixa. Uma caixa assincrona sumiria junto com o
        # processo antes de alguem ler.
        alerts.show_error(
            "O programa PAROU de imprimir.\n\n"
            f"{reason}\n\n"
            "Confira o e-mail e a senha no config.ini (clique com o botao "
            "direito no icone > Abrir a pasta da configuracao) e abra o "
            "programa de novo."
        )

    def on_order_printed(self, label: str) -> None:
        if not self.config.sound:
            return
        sound.play_new_order(self.config.sound_file)

    def on_printer_problem(self, printer_name: str, detail: str) -> None:
        # A chave da deduplicacao e a impressora, nao o pedido: papel acabado
        # falha em TODO pedido, e uma caixa por pedido empilharia a tela do
        # balcao inteira em cima da primeira, que era a que dizia qual
        # impressora. Ver `alerts.show_warning_once`.
        alerts.show_warning_once(f"impressora:{printer_name}", detail)

    # ---- o menu -----------------------------------------------------------

    def _open_log_folder(self) -> None:
        open_folder(self.config.log_file.parent)

    def _open_config_folder(self) -> None:
        open_folder(self.config.config_dir)

    def _quit(self) -> None:
        """Encerra de verdade.

        A ordem importa: `agent.stop()` primeiro, para o laco terminar a via
        que estiver saindo; `icon.stop()` depois, que e o que devolve a
        thread principal e deixa o processo terminar. Invertido, o icone
        sumiria da bandeja com o agente ainda imprimindo, e o lojista ficaria
        com um processo sem nenhuma forma de ve-lo ou fecha-lo.
        """
        logger.info("encerramento pedido pelo icone da bandeja")
        self.agent.stop()
        self._icon.stop()

    # ---- desenho ----------------------------------------------------------

    def _show(self, color: str, tooltip: str) -> None:
        self._icon.icon = _dot(color)
        self._icon.title = tooltip

    def _announce_once(self) -> None:
        """O balao da primeira conexao.

        E a unica resposta para "como o lojista sabe que deu certo?". Sem
        console e sem janela, uma instalacao bem-sucedida e indistinguivel de
        uma que nao subiu — as duas terminam com a tela do jeito que estava.
        Uma vez por execucao: o balao de toda reconexao viraria ruido.
        """
        if self._announced_connection:
            return
        self._announced_connection = True
        try:
            self._icon.notify(
                "Conectado. As comandas vao sair sozinhas.", "Rapidex Impressao"
            )
        except Exception:  # pragma: no cover - depende da versao do Windows
            logger.debug("nao foi possivel mostrar o balao de conexao")


def _dot(color: str) -> Image.Image:
    """Um circulo cheio da cor do estado.

    Desenhado aqui em vez de vir de um .ico no pacote: arquivo embutido no
    PyInstaller precisa de `datas` no .spec, de `sys._MEIPASS` para ser
    encontrado, e some silenciosamente quando alguem esquece um dos dois. Um
    circulo cabe em cinco linhas.

    A borda branca nao e enfeite: sem ela o ambar desaparece numa barra de
    tarefas clara e o verde some numa escura.
    """
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((6, 6, 58, 58), fill=color, outline="#ffffff", width=5)
    return image
