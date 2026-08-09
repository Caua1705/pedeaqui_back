"""Memoria do que ja foi impresso.

Existe por uma razao so, e ela e cara: **reconectar nao pode reimprimir**.

O stream entrega ao menos uma vez — cada poll do servidor olha alguns
segundos para tras do cursor, entao o mesmo evento chega repetido na
reconexao de proposito. Alem disso o servidor fecha a conexao a cada 15
minutos e o agente reabre mandando `Last-Event-ID`, o que faz o replay
comecar antes do ultimo evento visto. Sem esta memoria, um agente que
reconecta as 22h cospe a fila do dia inteiro.

E um arquivo e nao memoria RAM porque o caso que mais dói e justamente o
reinicio: queda de energia, atualizacao do Windows, alguem que fechou a
janela. O processo volta e precisa lembrar.

A gravacao e atomica (arquivo temporario + `os.replace`) porque a queda de
energia acontece no meio da escrita tambem, e um JSON truncado apagaria a
memoria inteira — de novo, a fila do dia toda saindo pela impressora.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


logger = logging.getLogger(__name__)

# Teto absoluto de entradas guardadas. A poda por data ja segura o tamanho;
# este limite existe para o caso patologico (relogio da maquina errado
# fazendo tudo parecer recente).
MAX_ENTRIES = 5000


class PrintedOrders:
    """Ids de pedido ja impressos, com a data em que foram."""

    def __init__(self, path: Path, retention_days: int = 7):
        self.path = path
        self.retention_days = retention_days
        self._entries: dict[str, str] = self._load()

    def __contains__(self, order_id: str) -> bool:
        return str(order_id) in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def mark(self, order_id: str, moment: datetime | None = None) -> None:
        """Registra o pedido e grava o arquivo na hora.

        Gravar a cada pedido, e nao periodicamente, e deliberado: o que este
        arquivo protege e exatamente o desligamento inesperado, e um buffer
        na memoria perderia justamente o ultimo pedido impresso.
        """
        stamp = (moment or _utcnow()).isoformat()
        self._entries[str(order_id)] = stamp
        self._prune()
        self._save()

    def _load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Arquivo corrompido nao derruba o agente: ele volta a imprimir
            # tudo, o que e ruim, mas nao para de imprimir, que e pior.
            logger.warning(
                "estado ilegivel em %s (%s): comecando do zero", self.path, exc
            )
            return {}
        if not isinstance(raw, dict):
            logger.warning("estado em %s nao e um objeto: comecando do zero", self.path)
            return {}
        return {str(key): str(value) for key, value in raw.items()}

    def _prune(self) -> None:
        floor = _utcnow() - timedelta(days=self.retention_days)
        kept = {}
        for order_id, stamp in self._entries.items():
            moment = _parse(stamp)
            if moment is None or moment >= floor:
                kept[order_id] = stamp

        if len(kept) > MAX_ENTRIES:
            newest = sorted(kept.items(), key=lambda pair: pair[1], reverse=True)
            kept = dict(newest[:MAX_ENTRIES])
        self._entries = kept

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Temporario na MESMA pasta: os.replace so e atomico dentro do mesmo
        # volume, e a pasta de temporarios do Windows costuma ser outro.
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=self.path.name,
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle as stream:
                json.dump(self._entries, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, self.path)
        except OSError as exc:
            logger.error("nao foi possivel gravar o estado em %s: %s", self.path, exc)
            try:
                os.unlink(handle.name)
            except OSError:
                pass


def _parse(stamp: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
