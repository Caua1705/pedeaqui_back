import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models.admin_error_report_model import AdminErrorReport


class AdminErrorReportRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        admin_user_id: uuid.UUID,
        description: str,
        error_log: str | None,
        screen: str | None,
        order_number: int | None,
    ) -> AdminErrorReport:
        """NAO commita: quem commita e o service (regra de camadas)."""
        relato = AdminErrorReport(
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            admin_user_id=admin_user_id,
            description=description,
            error_log=error_log,
            screen=screen,
            order_number=order_number,
        )
        self.db.add(relato)
        self.db.flush()
        return relato

    def list_recent(self, limit: int) -> list[AdminErrorReport]:
        """Os ultimos, do mais novo para o mais velho.

        Sem recorte de restaurante porque quem le e a PLATAFORMA, pelo
        `scripts/error_reports.py`. Nao ha rota que exponha isto ao painel.
        """
        stmt = (
            select(AdminErrorReport)
            .order_by(AdminErrorReport.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def delete_created_before(self, cutoff: datetime) -> int:
        """Apaga a linha inteira. E o mecanismo de exclusao desta tabela.

        `DELETE` e nao `UPDATE` — ao contrario de `order_reviews`, aqui nao ha
        metade que valha a pena guardar: sem a descricao e sem o log, sobra um
        carimbo de que alguem relatou alguma coisa um dia.
        """
        stmt = delete(AdminErrorReport).where(AdminErrorReport.created_at < cutoff)
        return self.db.execute(stmt).rowcount or 0
