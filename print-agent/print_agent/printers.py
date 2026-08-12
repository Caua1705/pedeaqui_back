"""Envio dos bytes para a fila de impressao do Windows.

Por que `win32print` cru e nao uma biblioteca de impressao: a comanda ja e
ESC/POS, e qualquer camada que "formate" o documento (Word, PDF, driver
grafico) transformaria os bytes de controle em texto impresso — a via sairia
com "GS ! 0x11" escrito nela. `StartDocPrinter` com datatype RAW e o unico
caminho que entrega os bytes intactos.

A impressora precisa estar instalada no Windows com um driver que aceite
RAW (o "Generic / Text Only" serve, e o driver do fabricante tambem). Nao
precisa estar compartilhada nem ter porta especial: basta o nome que aparece
em "Dispositivos e Impressoras", que e o que vai no config.ini.
"""

import logging

from print_agent.config import normalize_sector


logger = logging.getLogger(__name__)


class PrinterError(Exception):
    """A via nao chegou na impressora.

    Erro esperado, nao excepcional: papel acabou, impressora desligada,
    cabo USB solto. Quem chama tenta de novo e, se insistir, grita no log —
    ver `agent.print_jobs`.
    """


class Printer:
    """Interface do destino de uma via.

    Existe para que o laco do agente seja testavel sem Windows e sem
    impressora: o teste injeta uma implementacao que guarda os bytes numa
    lista.
    """

    def send(self, printer_name: str, payload: bytes, job_name: str) -> None:
        raise NotImplementedError


class WindowsRawPrinter(Printer):
    """Fila de impressao do Windows, em modo RAW."""

    def send(self, printer_name: str, payload: bytes, job_name: str) -> None:
        # Importado aqui dentro, e nao no topo do modulo, porque `pywin32` so
        # existe no Windows. Com o import no topo, os testes (e qualquer
        # inspecao do codigo em outra maquina) nao conseguiriam nem importar
        # o arquivo.
        try:
            import win32print
        except ImportError as exc:  # pragma: no cover - depende do SO
            raise PrinterError(
                "pywin32 nao esta instalado: rode pip install -r requirements.txt"
            ) from exc

        try:
            handle = win32print.OpenPrinter(printer_name)
        except Exception as exc:  # pragma: no cover - depende da maquina
            # O pywin32 fala Unicode direto com a API W do Windows: nome com
            # acento chega intacto e nao precisa de tratamento. O que falha e
            # o nome que so PARECE igual — "Cozinha Ação" digitado com acento
            # combinante casa byte a byte com nada, e na tela e identico ao
            # nome certo. O Windows responde "o nome da impressora e
            # invalido" e o lojista jura que copiou certo.
            resolved = _resolve_printer_name(win32print, printer_name)
            if resolved is None:
                raise PrinterError(
                    f"nao foi possivel abrir a impressora '{printer_name}': {exc}. "
                    f"Instaladas nesta maquina: {_installed_names(win32print)}"
                ) from exc

            logger.warning(
                "o nome '%s' do config.ini nao existe exatamente assim no "
                "Windows; usando '%s', que e o unico parecido. Corrija o "
                "config.ini para o nome exato.",
                printer_name,
                resolved,
            )
            try:
                handle = win32print.OpenPrinter(resolved)
            except Exception as retry_exc:
                raise PrinterError(
                    f"nao foi possivel abrir a impressora '{resolved}': {retry_exc}"
                ) from retry_exc
            printer_name = resolved

        try:
            # O terceiro elemento e o datatype. "RAW" e o que impede o
            # spooler de interpretar os bytes de controle como texto.
            job = win32print.StartDocPrinter(handle, 1, (job_name, None, "RAW"))
            try:
                win32print.StartPagePrinter(handle)
                win32print.WritePrinter(handle, payload)
                win32print.EndPagePrinter(handle)
            finally:
                win32print.EndDocPrinter(handle)
            logger.debug(
                "job %s enviado para '%s' (%d bytes)", job, printer_name, len(payload)
            )
        except PrinterError:
            raise
        except Exception as exc:  # pragma: no cover - depende da maquina
            raise PrinterError(
                f"falha ao escrever em '{printer_name}': {exc}"
            ) from exc
        finally:
            win32print.ClosePrinter(handle)


def _enum_printers(win32print) -> list[str]:
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        return [entry[2] for entry in win32print.EnumPrinters(flags)]
    except Exception:  # pragma: no cover - depende da maquina
        return []


def _resolve_printer_name(win32print, wanted: str) -> str | None:
    """O nome real da impressora que so difere do configurado no acento.

    Devolve `None` quando nao ha candidato OU quando ha mais de um: adivinhar
    entre duas impressoras e imprimir a comanda na praca errada, que e pior
    que nao imprimir — nao imprimir grita no log, sair na impressora errada
    nao grita em lugar nenhum.

    Usa a mesma normalizacao dos setores (`normalize_sector`), entao vale
    tambem para acento perdido de vez ("Impressora Acao") e caixa diferente.
    """
    alvo = normalize_sector(wanted)
    candidates = [name for name in _enum_printers(win32print) if normalize_sector(name) == alvo]
    return candidates[0] if len(candidates) == 1 else None


def _installed_names(win32print) -> str:
    names = _enum_printers(win32print)
    return ", ".join(f"'{name}'" for name in names) if names else "nenhuma encontrada"


class LoggingPrinter(Printer):
    """Destino de ensaio: escreve no log em vez de na impressora.

    E o `dry_run` do config.ini. Serve para conferir a instalacao numa
    maquina sem termica ligada — mapeamento de setor, credencial, stream —
    sem gastar bobina e sem esperar o dia da inauguracao para descobrir que
    o token estava errado.
    """

    def __init__(self):
        self.sent: list[tuple[str, bytes, str]] = []

    def send(self, printer_name: str, payload: bytes, job_name: str) -> None:
        self.sent.append((printer_name, payload, job_name))
        logger.info(
            "[dry-run] '%s' receberia %d bytes (%s)", printer_name, len(payload), job_name
        )
