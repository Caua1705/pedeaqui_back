# Alinhar o schema ao ORM: o roteiro, escrito e não aplicado

**Nada aqui foi executado contra produção.** As duas revisões estão prontas em
`alembic/preparadas/`, que o Alembic não lê. Este documento é o que decide se e
quando elas saem de lá.

O problema que ele resolve está em `docs/modelo-de-dados.md`, §6, e em
`scripts/divergencias_orm_schema.py`: em **15 colunas**, o model declara
`nullable=False` e o banco aceita `NULL`. A anotação nunca virou DDL — o schema
nasceu à mão no Supabase, `Base.metadata.create_all()` não é usado em lugar
nenhum, e nada compara os dois lados.

O que essas 15 custam hoje está levantado, uma a uma, em
`scratchpad/rodada-back-2.md` §1.1, e provado em
`tests/test_colunas_em_desacordo.py`: **oito têm caminho de leitura que
responde 500**, a pior sendo `restaurant_coupons.valid_until`, que derruba a
avaliação de cupom no checkout.

---

## Por que não é só `SET NOT NULL`

`ALTER TABLE t ALTER COLUMN c SET NOT NULL` faz duas coisas ao mesmo tempo:
toma **`ACCESS EXCLUSIVE`** sobre a tabela e **varre a tabela inteira** para
provar que não há nulo. `ACCESS EXCLUSIVE` bloqueia `SELECT` também — não é
"trava escrita", é trava tudo.

Multiplicado por 15 colunas em `customers`, `orders`-adjacentes e
`customer_addresses`, isso é a plataforma parada pelo tempo somado das
varreduras. E como `alembic/env.py` abre **uma transação para o upgrade
inteiro**, os locks só saem no commit final: a janela é a soma, não o máximo.

O caminho em duas etapas troca isso por:

| | lock | varre? |
|---|---|---|
| `ADD CONSTRAINT ... CHECK (c IS NOT NULL) NOT VALID` | `ACCESS EXCLUSIVE`, **milissegundos** | não |
| `VALIDATE CONSTRAINT` | `SHARE UPDATE EXCLUSIVE` — **não bloqueia leitura nem escrita** | sim |
| `SET NOT NULL` (com a CHECK já válida) | `ACCESS EXCLUSIVE`, **instantâneo** | não¹ |
| `DROP CONSTRAINT` | `ACCESS EXCLUSIVE`, instantâneo | não |

¹ Desde o Postgres 12, `SET NOT NULL` aceita uma `CHECK (c IS NOT NULL)`
válida como prova e pula o scan. Produção é Supabase PG 17.

**E há um ganho que não é de lock: a etapa 1 já cobra a regra das linhas
novas.** Do commit dela em diante o buraco para de crescer, mesmo que a etapa 2
demore semanas. Isso não é dedução do manual: está afirmado em
`tests/test_revisoes_preparadas.py::test_depois_da_etapa_1_o_nulo_NOVO_ja_e_recusado`,
contra o Postgres de verdade.

### As duas etapas NÃO podem ir no mesmo `alembic upgrade`

Pelo mesmo motivo da transação única do `env.py`: num upgrade que aplicasse as
duas, o `VALIDATE` da etapa 2 rodaria dentro da transação que ainda segura o
`ACCESS EXCLUSIVE` do `ADD CONSTRAINT` da etapa 1, e todo o ganho de lock
evapora. São **duas execuções**, com `ALEMBIC_TARGET` na primeira — o mesmo
mecanismo do deploy do `tracking_token`.

---

## Etapa 0 — contar os nulos. Antes de tudo, e só leitura

```bash
docker exec pedeaqui-api python scripts/nulos_nas_colunas_em_desacordo.py
```

Ele conta, por coluna, quantas linhas são nulas hoje. `count(*)` toma apenas
`ACCESS SHARE`: não tranca nada, não escreve nada.

| Saída | O que fazer |
|---|---|
| `0 com linha nula hoje` (código de saída 0) | as duas etapas valem como estão |
| qualquer coluna com `DECIDIR O DADO ANTES` (código 2) | **pare.** Ver abaixo |

**Coluna com nulo é decisão de dado, não de migração.** Para cada uma, três
saídas, e a escolha é do dono:

1. **preencher** — só onde existe valor certo (`customer_addresses.number` →
   `"S/N"`, por exemplo);
2. **apagar as linhas** — só onde a linha nula não significa nada
   (`ai_product_embeddings` com `product_id` nulo é lixo de índice: nenhuma
   consulta a enxerga, e o `JOIN products` já a descarta);
3. **tirar a coluna da revisão** — e então o alinhamento é do outro lado: o
   *model* passa a dizer `nullable=True`, porque o banco estava certo e a
   anotação é que mentia.

Para ver as linhas ofensoras de uma coluna:

```sql
SELECT * FROM customer_addresses WHERE number IS NULL LIMIT 20;
```

### A que SAIU desta lista, e por quê

`restaurant_coupons.valid_until` estava aqui e **saiu em 03/09/2026** — foram
16 e passaram a ser 15.

Cupom **sem data de fim** é uma campanha plausível, e havia precedente no
próprio schema: a revisão `20260828_0043` tornou `code` nulo *com significado*
("cupom que aplica sozinho"). Nas outras 15 o banco estava frouxo e o model
certo; naquela era o contrário — **o banco já permitia a campanha permanente e
quem mentia era o model.**

O conserto foi no código: `src/services/coupon_window.py`, `datetime | None` no
model e nos três schemas. Alinhar a coluna teria apagado uma possibilidade de
produto.

**O critério que saiu disso, e que decide a próxima:** tratar o nulo no código
só tem duas formas, e as duas custam mais que a migração — *relaxar o schema de
resposta* publica no contrato um estado que não deve existir e empurra o `if`
para todo cliente; *inventar um valor* faz o backend mentir, o que é pior que o
500 porque o 500 é visível. **Trate no código quando o nulo tem SIGNIFICADO de
produto, ou quando o tratamento não é nem uma coisa nem outra. Fora disso,
alinhe o schema.**

---

## Etapa 0b — mover as revisões para a cadeia

Enquanto os arquivos estiverem em `alembic/preparadas/`, nada acontece — é o
que `tests/test_revisoes_preparadas.py` garante. Para aplicar:

```bash
git mv alembic/preparadas/alinhamento_orm_schema_etapa_1.py \
       alembic/versions/20261231_0045_alinhamento_etapa_1.py
git mv alembic/preparadas/alinhamento_orm_schema_etapa_2.py \
       alembic/versions/20261231_0046_alinhamento_etapa_2.py
```

E então, **em cada arquivo**, trocar os marcadores:

- etapa 1: `revision = "20261231_0045"`, `down_revision = "<head do dia>"`;
- etapa 2: `revision = "20261231_0046"`, `down_revision = "20261231_0045"`.

O head do dia sai de `alembic heads`. Os marcadores são propositalmente
inválidos: mover sem trocá-los quebra alto, na coleta, em vez de baixo.

`tests/test_revisoes_preparadas.py` sai junto no mesmo commit — o diretório
fica vazio e o próprio arquivo diz isso.

---

## Etapa 1 — a promessa

```bash
cd /caminho/do/projeto
echo "ALEMBIC_TARGET=20261231_0045" >> .env
grep ALEMBIC_TARGET .env

GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
docker logs -f pedeaqui-api
#   esperado: [entrypoint] alembic upgrade 20261231_0045
#             [entrypoint] ATENCAO: ALEMBIC_TARGET=... — o banco NAO esta em head.
#   o ATENCAO e esperado NESTA etapa.
```

**Conferência** — as 15 restrições existem e estão `NOT VALID`:

```sql
SELECT conrelid::regclass AS tabela, conname, convalidated
  FROM pg_constraint
 WHERE conname LIKE 'ck_%_nao_nula'
 ORDER BY 1, 2;
```

Espere `convalidated = false` nas 15. É o estado correto no fim desta etapa.

**Deixe assar.** Um dia inteiro de operação normal, no mínimo. O que se ganha
com a espera: a etapa 1 já recusa nulo novo, então quando o `VALIDATE` rodar
não há corrida com escrita concorrente — se ele falhar, é por linha que já
existia, e não por algo que entrou no meio.

**Rollback da etapa 1: completo e barato.** `alembic downgrade` derruba as 15
restrições. Nenhum dado foi tocado, nenhuma coluna mudou. É o raro downgrade
honesto — ao contrário do da `0017` do `tracking_token`, que recria a coluna
vazia.

---

## Etapa 2 — a cobrança

Refaça a etapa 0 imediatamente antes. Depois:

```bash
sed -i '/^ALEMBIC_TARGET=/d' .env
grep ALEMBIC_TARGET .env                     # não pode sobrar nada
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d
docker logs -f pedeaqui-api
#   esperado: [entrypoint] alembic upgrade head   e NENHUM "ATENCAO".
```

**Se o `VALIDATE` falhar**, a transação inteira volta: nenhuma das 15 colunas
fica alterada e as restrições `NOT VALID` continuam no lugar, cobrando as
linhas novas. Não existe estado pela metade. O erro nomeia a restrição, e o
nome dela é `ck_<tabela>_<coluna>_nao_nula` — a coluna está no nome.

**Isto também está demonstrado, e não apenas descrito:**
`test_a_etapa_2_falha_no_VALIDATE_quando_sobra_nulo_antigo` planta uma linha
nula, roda as duas etapas e prova que a segunda morre no **primeiro** comando,
com a mensagem `violated by some row` — e que, depois do rollback, numa conexão
nova, a coluna continua nulável e a restrição não existe.

**Conferência final:**

```bash
docker exec pedeaqui-api python scripts/divergencias_orm_schema.py
```

A seção "ORM diz NOT NULL, banco aceita NULL" tem que dizer **`Nenhuma.`**, e o
total tem que cair de 41 para 26.

**Rollback da etapa 2:** também completo — `DROP NOT NULL` e a CHECK volta como
`NOT VALID`. Nenhum dado é tocado por nenhuma das duas etapas. **Esta migração
não tem ponto sem volta**, e é bom dizê-lo em voz alta porque é o oposto do
outro roteiro deste repositório.

---

## O que este alinhamento NÃO cobre

As outras **26** divergências, que não precisam de DDL nenhum:

- **20 — o banco diz NOT NULL e o model diz nullable.** Correção é editar o
  model. 18 são benignas (têm `DEFAULT`, então omitir no INSERT é seguro e a
  anotação só engana quem lê); duas mordem —
  `ai_product_embeddings.content` e `coupon_templates.image_path`, sem default,
  onde um `Modelo(...)` incompleto estoura `IntegrityError`. Hoje não estoura
  porque nada instancia os dois models, e
  `tests/test_models_nunca_instanciados.py` avisa quando isso mudar.
- **6 — coluna que o ORM não mapeia** (`created_at`/`updated_at` de
  `product_options`, `product_option_groups`, `order_item_options` e
  `ai_product_embeddings`). Mapear é uma linha por coluna, **mas muda o que o
  ORM traz em todo `SELECT`** — é mudança de comportamento, não de anotação, e
  merece revisão própria.

As 26 são edição de código Python, revisável em PR normal, sem janela e sem
lock. Ficaram de fora desta rodada por serem outra decisão, não por serem
menos importantes.
