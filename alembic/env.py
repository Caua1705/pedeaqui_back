"""Configuracao do Alembic.

A URL do banco vem de src.core.config.settings, nunca do alembic.ini, para
que o segredo fique so no .env e migracao e aplicacao nao possam divergir.

`target_metadata` aponta para o Base do projeto com todos os modelos
importados. Isso habilita `alembic revision --autogenerate`, mas leia o
diff gerado antes de aplicar: o banco de producao tem objetos que o ORM
nao mapeia (sequences, defaults, indices criados a mao nos 13 .sql
antigos), e o autogenerate tenta remove-los.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.core.config import settings
from src.db.base import Base

# Importado pelo efeito colateral de registrar todos os modelos no Base
# antes de o autogenerate comparar metadata com o banco.
import src.models  # noqa: F401


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
