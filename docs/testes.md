# Testes: o que roda hoje e o que falta

## As duas suites

Hoje existe **uma** suite, rápida, com fakes em memória:

```bash
pytest            # ~22s, sem banco, sem rede
```

O plano é ela continuar sendo o laço de desenvolvimento e ganhar uma irmã
lenta, marcada, que roda contra um Postgres de verdade:

```bash
pytest -m "not db"   # o laço de desenvolvimento, sem Docker aberto
pytest               # tudo, antes de commitar
```

A separação é por marcador e não por diretório, porque um teste de repositório
costuma querer viver ao lado do teste de service do mesmo assunto.

## Por que os fakes não bastam para o repositório

Nenhum repositório passa de ~67% de cobertura, e a causa é estrutural: os
testes usam fakes em memória, então **a query real nunca executa**. O fake
prova que o parâmetro chegou, não que o SQL está certo.

E o schema de teste **não pode** nascer de `Base.metadata.create_all()`: o ORM
não mapeia as sequences (inclusive a de `order_number`), os defaults e os
índices criados à mão nos 12 `.sql` de `migrations/` — é a armadilha 24. O
schema sai de `alembic upgrade head`.

O obstáculo a resolver antes de qualquer coisa é o **engine**:
`src/db/session.py` o cria no import, a partir de `settings.DATABASE_URL`. Ou
a variável está no ambiente antes de qualquer import de `src`, ou o engine
passa a ser criado sob demanda.

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
