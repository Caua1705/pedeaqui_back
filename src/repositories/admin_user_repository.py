import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.constants import PAPEIS_DE_PESSOA, PAPEL_DE_DONO
from src.models.admin_user_model import AdminUser


class AdminUserRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_email(self, email: str) -> AdminUser | None:
        # lower() dos dois lados para casar com o indice unico funcional
        # criado na migracao 20260726_0003.
        stmt = select(AdminUser).where(func.lower(AdminUser.email) == email.lower())
        return self.db.scalar(stmt)

    def get_by_id(self, admin_user_id: uuid.UUID) -> AdminUser | None:
        return self.db.get(AdminUser, admin_user_id)

    def list_by_restaurant(self, restaurant_id: uuid.UUID) -> list[AdminUser]:
        stmt = (
            select(AdminUser)
            .where(AdminUser.restaurant_id == restaurant_id)
            .order_by(AdminUser.created_at)
        )
        return list(self.db.scalars(stmt).all())

    def list_people_by_restaurant(self, restaurant_id: uuid.UUID) -> list[AdminUser]:
        """As PESSOAS do restaurante. A conta de maquina fica de fora.

        `GET /admin/users` e a tela da equipe. O agente de impressao ja tem
        tela propria em `/admin/printing`, que e onde ele faz sentido — com
        nome de impressora e estado de heartbeat ao lado. Misturado aqui ele
        vira uma linha sem telefone, sem cargo e sem ninguem por tras.

        **`IN (papeis de pessoa)`, e nao `!= PAPEL_DE_MAQUINA`** — armadilha
        47. As duas formas sao equivalentes hoje, porque `ADMIN_USER_ROLES`
        tem exatamente uma conta de maquina; deixam de ser na revisao que
        acrescentar a segunda, e ai a negacao a traz PARA A TELA DA EQUIPE
        sozinha. A forma positiva obriga quem criar o papel a decidir de que
        lado ele fica, que e a decisao que a negacao toma por omissao.
        """
        stmt = (
            select(AdminUser)
            .where(
                AdminUser.restaurant_id == restaurant_id,
                AdminUser.role.in_(PAPEIS_DE_PESSOA),
            )
            .order_by(AdminUser.created_at)
        )
        return list(self.db.scalars(stmt).all())

    def get_by_id_and_restaurant(
        self,
        admin_user_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> AdminUser | None:
        """O usuario, se ele for deste restaurante.

        O filtro entra no WHERE, e nao numa comparacao depois do `get`: e o
        mesmo motivo de toda rota /admin: quem escreve o `if` esquece, quem
        escreve o WHERE nao tem como.
        """
        stmt = select(AdminUser).where(
            AdminUser.id == admin_user_id,
            AdminUser.restaurant_id == restaurant_id,
        )
        return self.db.scalar(stmt)

    def count_active_owners(self, restaurant_id: uuid.UUID) -> int:
        """Quantos donos ATIVOS o restaurante ainda tem.

        E a conta que impede o restaurante de ficar sem dono nenhum — estado
        cuja unica saida seria `docker exec`, que e exatamente o que estas
        rotas existem para eliminar.
        """
        stmt = select(func.count(AdminUser.id)).where(
            AdminUser.restaurant_id == restaurant_id,
            AdminUser.role == PAPEL_DE_DONO,
            AdminUser.is_active.is_(True),
        )
        return int(self.db.scalar(stmt) or 0)

    def create(self, admin_user: AdminUser) -> AdminUser:
        self.db.add(admin_user)
        self.db.flush()
        return admin_user

    def save(self, admin_user: AdminUser) -> AdminUser:
        self.db.add(admin_user)
        self.db.flush()
        return admin_user
