"""reconcilia admin_users

Revision ID: 20260726_0003
Revises: 20260726_0002
Create Date: 2026-07-26

A tabela `admin_users` ja existe no banco de producao — foi criada fora de
qualquer migracao versionada e nunca teve modelo nem uso no codigo, entao
esta vazia. Esta revisao nao pode assumir a forma exata dela.

Por isso todo o DDL aqui e idempotente (`IF NOT EXISTS`): roda sem erro
tanto no banco onde a tabela ja existe quanto em um banco novo, e so
acrescenta o que estiver faltando. O objetivo e deixar a tabela alinhada
com src/models/admin_user_model.py, seja qual for o ponto de partida.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260726_0003"
down_revision: Union[str, None] = "20260726_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            restaurant_id uuid NOT NULL REFERENCES restaurants(id),
            branch_id uuid REFERENCES branches(id),
            name text NOT NULL,
            email text NOT NULL,
            password_hash text NOT NULL,
            role text NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # Colunas que podem faltar se a tabela foi criada com outra forma.
    op.execute("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS branch_id uuid")
    op.execute("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true")
    op.execute("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()")

    # E-mail unico e global, nao por restaurante: o login recebe so e-mail e
    # senha, sem slug. Se o mesmo e-mail existisse em dois restaurantes nao
    # haveria como decidir em qual autenticar.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_users_email
        ON admin_users (lower(email))
        """
    )

    # Listar os admins de um restaurante e a consulta de administracao mais
    # comum; sem indice ela vira seq scan conforme a base cresce.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_admin_users_restaurant_id
        ON admin_users (restaurant_id)
        """
    )

    op.execute(
        """
        ALTER TABLE admin_users DROP CONSTRAINT IF EXISTS ck_admin_users_role
        """
    )
    op.execute(
        """
        ALTER TABLE admin_users ADD CONSTRAINT ck_admin_users_role
        CHECK (role IN ('owner', 'manager', 'attendant'))
        """
    )


def downgrade() -> None:
    # A tabela nao foi criada por esta revisao no banco real, entao o
    # downgrade desfaz apenas o que ela acrescentou. Derrubar admin_users
    # aqui apagaria os lojistas cadastrados.
    op.execute("ALTER TABLE admin_users DROP CONSTRAINT IF EXISTS ck_admin_users_role")
    op.execute("DROP INDEX IF EXISTS ix_admin_users_restaurant_id")
    op.execute("DROP INDEX IF EXISTS uq_admin_users_email")
