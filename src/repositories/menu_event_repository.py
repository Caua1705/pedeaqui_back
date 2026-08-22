"""Escrita e contagem do funil do cardapio.

Duas naturezas bem diferentes convivem aqui, e vale saber qual e qual:

- **a escrita** e o caminho quente. Roda numa rota publica, sem
  autenticacao, chamada por toda pessoa que abre o cardapio. E um INSERT em
  lote e mais nada — sem SELECT de conferencia, sem leitura previa;
- **as contagens** rodam no painel, algumas vezes por dia, sobre um recorte
  de no maximo 92 dias.

Como todo repositorio do projeto, nada aqui commita: quem commita e quem
chamou.
"""

import uuid
from datetime import datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from src.models.menu_event_model import MenuEvent


class MenuEventRepository:
    def __init__(self, db: Session):
        self.db = db

    def insert_batch(self, rows: list[dict]) -> int:
        """Grava o lote inteiro num INSERT so.

        `insert()` do core e nao `db.add()` por objeto: o ORM montaria uma
        instancia de `MenuEvent` por evento so para descarta-la em seguida,
        e ninguem le esses objetos depois. Cinquenta eventos viram um
        comando.

        **Nao valida FK por consulta.** `restaurant_id` e `branch_id` sao FK
        no banco: um id que nao existe volta como erro de integridade, sem
        custar um round-trip a cada requisicao no caminho mais movimentado
        da API. Quem traduz esse erro em resposta e o service.
        """
        if not rows:
            return 0

        # Devolve `len(rows)` e nao o `rowcount` do resultado: o INSERT em
        # lote da sessao do ORM volta como `IteratorResult`, que nao tem
        # rowcount. Nao ha perda de informacao — o comando grava tudo ou
        # levanta, e quem levanta e tratado no service.
        self.db.execute(insert(MenuEvent), rows)
        return len(rows)

    def delete_occurred_before(self, cutoff: datetime, limit: int) -> int:
        """Apaga ATE `limit` eventos anteriores ao corte. Devolve quantos.

        Em lote, e nao de uma vez, porque esta e de longe a tabela que mais
        cresce: um `DELETE` unico de centenas de milhares de linhas segura
        lock e infla a tabela por toda a duracao da transacao. Quem repete a
        chamada ate ela devolver zero — commitando entre uma e outra — e o
        script de limpeza, que e quem detem o commit.

        A subconsulta com `LIMIT` existe porque `DELETE ... LIMIT` nao e SQL
        valido no Postgres.
        """
        alvo = (
            select(MenuEvent.id)
            .where(MenuEvent.occurred_at < cutoff)
            .limit(limit)
            .scalar_subquery()
        )
        result = self.db.execute(delete(MenuEvent).where(MenuEvent.id.in_(alvo)))
        return result.rowcount or 0

    def count_occurred_before(self, cutoff: datetime) -> int:
        """Quantos eventos o expurgo apagaria. So o `--dry-run` usa."""
        stmt = (
            select(func.count())
            .select_from(MenuEvent)
            .where(MenuEvent.occurred_at < cutoff)
        )
        return self.db.scalar(stmt) or 0

    def sessions_by_event_type(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
        source: str | None = None,
    ) -> dict[str, int]:
        """Quantas SESSOES distintas chegaram a cada degrau.

        Sessoes e nao eventos, e a diferenca e a pergunta inteira: "quantas
        pessoas abriram um produto" nao e "quantos toques houve". O front ja
        deduplica por produto dentro da sessao, mas o `DISTINCT` aqui e o que
        garante o numero mesmo se ele parar de deduplicar — e ele vai parar
        no dia em que alguem reescrever a tela.

        `end_at` e EXCLUSIVO, como em todo recorte de periodo do projeto.

        Devolve dicionario e nao lista porque quem chama precisa perguntar
        "quantas no degrau X" para os quatro degraus, inclusive os que nao
        aparecem — degrau sem nenhuma sessao simplesmente nao volta do banco.
        """
        stmt = (
            select(MenuEvent.event_type, func.count(func.distinct(MenuEvent.session_id)))
            .where(
                *self._funnel_conditions(
                    restaurant_id, start_at, end_at, branch_id, source
                )
            )
            .group_by(MenuEvent.event_type)
        )
        return {row[0]: row[1] for row in self.db.execute(stmt).all()}

    def sessions_by_source(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> list[tuple[str, int]]:
        """Quantas sessoes VISITARAM o cardapio, por origem.

        So o primeiro degrau (`menu_view`). Contar todos os tipos aqui somaria
        a mesma sessao uma vez por degrau que ela alcancou, e a origem que
        converte melhor apareceria com mais "sessoes" — que e exatamente o
        numero que a divisao por origem existe para nao confundir.
        """
        stmt = (
            select(MenuEvent.source, func.count(func.distinct(MenuEvent.session_id)))
            .where(
                *self._funnel_conditions(restaurant_id, start_at, end_at, branch_id),
                MenuEvent.event_type == "menu_view",
            )
            .group_by(MenuEvent.source)
            .order_by(func.count(func.distinct(MenuEvent.session_id)).desc())
        )
        return [(row[0], row[1]) for row in self.db.execute(stmt).all()]

    @staticmethod
    def _funnel_conditions(
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
        source: str | None = None,
    ) -> list:
        """O recorte das duas contagens, escrito uma vez.

        Mesma forma de `billable_order_conditions` e pelo mesmo motivo: as
        duas consultas do funil precisam falar do mesmo conjunto, senao a
        divisao por origem soma diferente do total dos degraus e ninguem
        consegue explicar a diferenca.

        Nulo em `branch_id` e "o restaurante inteiro"; nulo em `source` e
        "todas as origens".
        """
        conditions = [
            MenuEvent.restaurant_id == restaurant_id,
            MenuEvent.occurred_at >= start_at,
            MenuEvent.occurred_at < end_at,
        ]
        if branch_id is not None:
            conditions.append(MenuEvent.branch_id == branch_id)
        if source is not None:
            conditions.append(MenuEvent.source == source)
        return conditions
