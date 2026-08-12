"""revoga truncate, references e trigger de anon e authenticated

Revision ID: 20260812_0014
Revises: 20260812_0013
Create Date: 2026-08-12

Achado A14 da auditoria de 12/08/2026.

O QUE FOI ENCONTRADO. Nas 30 tabelas de `public`, os papeis `anon` e
`authenticated` — os dois que a API REST publica do Supabase usa — nao tinham
nenhum privilegio de leitura ou escrita normal (o DML ja tinha sido revogado
em algum momento), mas continuavam com `TRUNCATE`, `REFERENCES`, `TRIGGER` e
`MAINTAIN`. Sobra do default do Supabase, que concede tudo, quando o DML foi
tirado e o resto ficou.

POR QUE `TRUNCATE` E DIFERENTE DOS OUTROS. **RLS nao filtra TRUNCATE.** A
politica de linha protege SELECT, INSERT, UPDATE e DELETE; um TRUNCATE
autorizado esvazia a tabela inteira, com RLS ligada e tudo. As 30 tabelas tem
RLS ligada e zero politicas, o que fecha a leitura — e nao fechava esta porta.

NAO ERA ALCANCAVEL, E MESMO ASSIM SAI. O PostgREST so emite SELECT, INSERT,
UPDATE, DELETE e chamada de funcao; nao existe caminho HTTP que gere um
TRUNCATE, e nao ha funcao nossa exposta. Ou seja: hoje isto e resíduo, nao
buraco. Sai porque o privilegio nao compra nada — ninguem precisa dele — e
porque "nao e alcancavel hoje" e uma frase que envelhece mal. `TRIGGER`
segue junto pelo mesmo raciocinio: anexar funcao a tabela nossa nao e coisa
que o publico anonimo deva poder fazer se um dia houver caminho.

O DEFAULT TAMBEM MUDA, e essa e a metade que importa a longo prazo. Sem
mexer em `ALTER DEFAULT PRIVILEGES`, a proxima tabela criada por uma revisao
do Alembic nasceria com os mesmos privilegios de volta, e o REVOKE de hoje
seria um conserto que dura ate a proxima migracao.

TOLERANTE A BANCO SEM ESSES PAPEIS. `anon` e `authenticated` sao criados pelo
Supabase; o Postgres descartavel da suite `db` (docker-compose.test.yml) nao
os tem. Sem o `DO` que confere `pg_roles`, esta revisao derrubaria a suite
inteira com "role anon does not exist".

`service_role` fica de fora de proposito: e chave de servidor, usada pelo
painel do Supabase e pelo upload de imagem, e revogar privilegio dela nao
fecha nenhuma porta publica — so cria surpresa em ferramenta administrativa.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260812_0014"
down_revision: Union[str, None] = "20260812_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PAPEIS_PUBLICOS = ("anon", "authenticated")

# MAINTAIN so existe do PG 17 em diante (VACUUM/ANALYZE/REINDEX delegados).
# Producao e 17.6 e a imagem de teste e pg17, mas o REVOKE fica separado para
# um banco mais antigo nao derrubar a revisao inteira por causa dele.
PRIVILEGIOS = "TRUNCATE, REFERENCES, TRIGGER"


def _para_cada_papel(comando: str) -> None:
    """Roda o comando so para os papeis que existirem neste banco."""
    for papel in PAPEIS_PUBLICOS:
        op.execute(
            f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{papel}') THEN
                {comando.format(papel=papel)}
              END IF;
            END
            $$;
            """
        )


def upgrade() -> None:
    # 1. As tabelas que ja existem.
    _para_cada_papel(
        f"REVOKE {PRIVILEGIOS} ON ALL TABLES IN SCHEMA public FROM {{papel}};"
    )
    # 2. As que ainda vao existir. Sem isto, a proxima revisao que criar tabela
    #    devolve os privilegios em silencio.
    _para_cada_papel(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE {PRIVILEGIOS} ON TABLES FROM {{papel}};"
    )
    # 3. MAINTAIN, separado por ser exclusivo do PG 17+.
    _para_cada_papel(
        """
        BEGIN
          EXECUTE 'REVOKE MAINTAIN ON ALL TABLES IN SCHEMA public FROM {papel}';
          EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                  'REVOKE MAINTAIN ON TABLES FROM {papel}';
        EXCEPTION WHEN syntax_error OR feature_not_supported THEN
          RAISE NOTICE 'MAINTAIN nao existe neste Postgres; ignorado.';
        END;
        """
    )


def downgrade() -> None:
    # Devolve o que o Supabase concede por padrao. O DML NAO volta: ele ja
    # estava revogado antes desta revisao, e reconceder SELECT em `customers`
    # para `anon` seria abrir a porta que a auditoria inteira existe para
    # manter fechada.
    _para_cada_papel(
        f"GRANT {PRIVILEGIOS} ON ALL TABLES IN SCHEMA public TO {{papel}};"
    )
    _para_cada_papel(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT {PRIVILEGIOS} ON TABLES TO {{papel}};"
    )
