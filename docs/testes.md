# Testes: o que roda hoje e o que falta

## As duas suítes

```bash
pytest -m "not db"   # o laço de desenvolvimento, sem Docker aberto
pytest               # tudo, antes de commitar
```

A separação é por marcador e não por diretório, porque um teste de repositório
costuma querer viver ao lado do teste de service do mesmo assunto.

`--strict-markers` está ligado: um marcador com nome errado é erro de coleta, e
não um teste que silenciosamente escapa do filtro `-m`.

## Por que os fakes não bastam para o repositório

Nenhum repositório passa de ~67% de cobertura, e a causa é estrutural: os
testes usam fakes em memória, então **a query real nunca executa**. O fake
prova que o parâmetro chegou, não que o SQL está certo.

## Como o banco de teste é montado

```bash
docker compose -f docker-compose.test.yml up -d
pytest -m db
docker compose -f docker-compose.test.yml down -v
```

Postgres **17**, o mesmo major do Supabase, em `localhost:55432`
(`TEST_DATABASE_URL` sobrepõe). Sem volume: os dados vivem em `tmpfs` e morrem
com o container. Imagem `pgvector/pgvector:pg17` por causa de uma coluna só,
`ai_product_embeddings.embedding`.

O schema **não** nasce de `Base.metadata.create_all()`: o ORM não mapeia as
sequences (inclusive a de `order_number`), os defaults nem os índices criados à
mão nos 12 `.sql` de `migrations/` — é a armadilha 24.

Ele nasce de três passos, em `tests/conftest.py`:

1. `alembic/schema_baseline.sql` — `pg_dump --schema-only` do banco de
   produção na revisão `20260810_0012`;
2. `alembic stamp 20260810_0012` — marca essa foto como aplicada;
3. `alembic upgrade head` — aplica o que vier **depois** dela.

O `.sql` no meio não é atalho, é a única saída: a revisão de baseline do
Alembic (`20260726_0001`) é um **no-op**, porque o schema é anterior ao Alembic
e nunca teve um `CREATE TABLE` versionado. Num banco vazio, `alembic upgrade
head` sozinho morre na revisão `0002` com `relation "orders" does not exist`.
Quem chegar aqui querendo entender o arquivo: o cabeçalho dele conta a história
inteira, inclusive quando (quase nunca) regerá-lo.

Toda revisão nova daqui em diante entra pelo passo 3 e o arquivo do passo 1 não
precisa saber dela.

### As duas fixtures

| Fixture | Escopo | O que faz |
|---|---|---|
| `engine_de_teste` | sessão | derruba e remonta o schema uma vez por execução |
| `db` | função | sessão dentro de uma transação revertida no fim do teste |

A `db` entra na transação com `join_transaction_mode="create_savepoint"`: um
`commit()` do código sob teste solta um savepoint em vez de gravar, então dá
para testar service (que commita) e repositório (que não) com a mesma fixture.

**Sequence não volta no rollback**, de propósito — `nextval` é imune. Teste que
dependa do valor exato de `order_number` está errado pelo mesmo motivo da
armadilha 19.

Antes de apagar qualquer coisa, a fixture recusa banco que não seja local **e**
cujo nome não termine em `_teste`. O primeiro comando que ela executa é um
`DROP SCHEMA public CASCADE`.

### No CI

O job `banco` do workflow sobe a **mesma** imagem (`pgvector/pgvector:pg17`) como
`services:` do GitHub Actions, na mesma porta 55432, e roda `pytest -m db`. O
schema sai da fixture, e não de um passo de shell no YAML: um segundo
procedimento em paralelo envelheceria sozinho, e o que quebraria em silêncio
seria justamente o que ninguém roda na própria máquina.

## A fila do marcador `db`

### 1. `AdminMenuRepository.product_ids_blocked_by_required_group` — **primeiro**

Cobertura do módulo: **40%**, e a consulta nova está dentro dessa fatia.

É a que mais precisa: ela decide se um produto aparece como "fora de venda"
para o lojista, e é **a mesma regra escrita duas vezes** — em Python
(`services/menu_rules.blocking_required_group`, para as rotas de um produto) e
em SQL (aqui, uma consulta para a página inteira, para a listagem não virar
uma leitura por produto).

Hoje só a versão em Python é exercitada de verdade.
`test_admin_product_availability.py::TestTheTwoExpressionsAgree` compara a
regra contra uma **tradução fiel** do `NOT EXISTS` — o que pega alguém mudar
uma e esquecer a outra, mas não prova que o SQL faz o que a tradução diz.

O que o teste com Postgres precisa cobrir:

- grupo obrigatório ativo sem nenhuma opção ativa → produto marcado;
- grupo obrigatório **sem nenhuma linha de opção** → também marcado (é por
  isso que a consulta é `NOT EXISTS` e não `count(ativas) = 0`);
- grupo opcional vazio e grupo obrigatório desativado → **não** marcam;
- uma opção ativa entre várias inativas → não marca;
- a consulta é **uma só** para a página inteira, e o `IN (...)` respeita o
  recorte da página.

### 2. `customer_repository` (33%) e `order_repository` (40%)

Os dois mais baixos e os mais usados.

### 3. O resto, por ordem de cobertura

`coupon_repository` (36%), `admin_menu_repository` (o que sobrar),
`admin_report_repository` (49%), `admin_settings_repository` (50%).
