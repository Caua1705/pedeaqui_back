# migrations/ — arquivo historico (congelado)

Os `.sql` desta pasta foram aplicados a mao no banco de producao antes da
Fase 1. Eles estao aqui como registro do que aconteceu com o schema, e
**nao devem ser executados de novo**: quase todos fariam `CREATE TABLE` em
tabela existente ou repetiriam um `ALTER` ja aplicado.

A evolucao de schema agora e do Alembic (`alembic/versions/`). O estado
representado por estes arquivos e exatamente o que a revisao baseline
`20260726_0001` assume que ja existe no banco.

Nao adicione `.sql` novo aqui. Use `alembic revision`.
