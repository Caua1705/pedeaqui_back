# Alinhar o schema ao ORM: o roteiro, e onde ele está hoje

**Estado em 04/09/2026.** A **etapa 1 está na cadeia**, como
`alembic/versions/20260905_0055_alinhamento_orm_schema_etapa_1.py`: ela saiu de
`preparadas/` deliberadamente e entra no próximo `docker compose up`. A **etapa
2 continua em `alembic/preparadas/`**, que o Alembic não lê.

**Nada aqui escreveu em produção ainda** — ela está em `20260904_0050`, cinco
revisões atrás do código. O que já rodou contra ela é só leitura: a etapa 0.

O problema que ele resolve está em `docs/modelo-de-dados.md`, §6, e em
`scripts/divergencias_orm_schema.py`: em **15 colunas**, o model declara
`nullable=False` e o banco aceita `NULL`. A anotação nunca virou DDL — o schema
nasceu à mão no Supabase, `Base.metadata.create_all()` não é usado em lugar
nenhum, e nada compara os dois lados.

O que essas 15 custam hoje está levantado, uma a uma, em
`scratchpad/rodada-back-2.md` §1.1, e provado em
`tests/test_colunas_em_desacordo.py`: **sete têm caminho de leitura que
responde 500** — e duas são piores por NÃO darem: `ai_feedback.created_at` nulo
faz o DELETE de retenção nunca alcançar a linha (dado pessoal em claro que não
sai), e `coupon_redemptions.idempotency_key` nulo perde a garantia do `UNIQUE`.

A oitava dos 500 era `restaurant_coupons.valid_until`, que **saiu da lista** em
03/09/2026 — ver abaixo.

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

**Rodado em 04/09/2026, contra produção: `15 coluna(s) em desacordo | 0 com
linha nula hoje`.** As 15 saíram `PRONTA para SET NOT NULL`, e o número que
importa junto é o tamanho: a maior das oito tabelas envolvidas é
`ai_product_embeddings`, com **272 linhas**; `order_item_options` e
`coupon_redemptions` estão **vazias**, `customers` tem 3 e `admin_users`, 4.

Isso não dispensa refazer a contagem imediatamente antes da etapa 2 — o número
envelhece, e impedir que ele cresça é justamente o que a etapa 1 faz. O que ele
decide é outra coisa: **em nenhuma das duas etapas o custo de lock é mensurável
hoje.** O desenho em duas etapas continua certo, e a razão dele deixou de ser o
tamanho da tabela (ver "O custo real, com os números de hoje", no fim).

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

Enquanto o arquivo estiver em `alembic/preparadas/`, nada acontece — é o que
`tests/test_revisoes_preparadas.py` garante.

**A etapa 1 já foi movida**, em 04/09/2026 (commit `cb1db8f`): ela é
`20260905_0055`, com `down_revision = "20260905_0054"`. Não há nada a fazer por
ela aqui.

Para a etapa 2, quando for a hora:

```bash
git mv alembic/preparadas/alinhamento_orm_schema_etapa_2.py \
       alembic/versions/<data>_<n>_alinhamento_etapa_2.py
```

E então, **no arquivo**, trocar os marcadores: `revision = "<data>_<n>"` e
`down_revision = "<head do dia>"` — que sai de `alembic heads`, e **não** é
necessariamente `20260905_0055`: outras revisões podem ter entrado no meio. Os
marcadores `PREPARADA_...` são propositalmente inválidos: mover sem trocá-los
quebra alto, na coleta, em vez de baixo.

`tests/test_revisoes_preparadas.py` sai junto nesse commit — o diretório fica
vazio e o próprio arquivo diz isso.

---

## Etapa 1 — a promessa

**Ela não vai sozinha.** Produção está em `20260904_0050` e a etapa 1 é
`20260905_0055`: o `alembic upgrade` aplica **cinco** revisões nesta subida. As
quatro da frente são a rodada do WhatsApp — `0051` cria as três tabelas do
canal, `0052` e `0054` acrescentam coluna a elas, `0053` troca uma CHECK. Todas
tocam **apenas** tabelas `whatsapp_*`, que nascem nesta mesma leva e portanto
estão vazias: nenhuma varre nada, nenhuma trava tabela em uso.

Como a etapa 1 **é** o head de hoje, `ALEMBIC_TARGET` não é necessário — ele
existe para parar ANTES do head, e aqui não há nada depois dela. Deixe o `.env`
sem a variável:

```bash
cd /caminho/do/projeto
grep ALEMBIC_TARGET .env                     # não pode sobrar nada

GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
docker logs -f pedeaqui-api
#   esperado: [entrypoint] alembic upgrade head   e NENHUM "ATENCAO".
```

`ALEMBIC_TARGET=<a etapa 1>` volta a ser obrigatório **no dia em que a etapa 2
entrar na cadeia** e as duas ainda não tiverem sido aplicadas — é o que impede
o mesmo `upgrade` de rodar as duas, e o motivo está logo acima.

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
total tem que cair de **35 para 20**.

(Eram 41 quando este roteiro foi escrito. As seis da terceira classe — coluna
que o ORM não mapeava — foram fechadas em 04/09/2026, no código, sem DDL
nenhum.)

**Rollback da etapa 2:** também completo — `DROP NOT NULL` e a CHECK volta como
`NOT VALID`. Nenhum dado é tocado por nenhuma das duas etapas. **Esta migração
não tem ponto sem volta**, e é bom dizê-lo em voz alta porque é o oposto do
outro roteiro deste repositório.

---

## O que este alinhamento NÃO cobre

As outras **20** divergências, que não precisam de DDL nenhum:

- **20 — o banco diz NOT NULL e o model diz nullable.** Correção é editar o
  model. 18 são benignas (têm `DEFAULT`, então omitir no INSERT é seguro e a
  anotação só engana quem lê); duas mordem —
  `ai_product_embeddings.content` e `coupon_templates.image_path`, sem default,
  onde um `Modelo(...)` incompleto estoura `IntegrityError`. Hoje não estoura
  porque nada instancia os dois models, e
  `tests/test_models_nunca_instanciados.py` avisa quando isso mudar.
- **6 — coluna que o ORM não mapeia.** **Feito em 04/09/2026**, no commit
  `cb1db8f`: eram os `created_at`/`updated_at` de `product_options`,
  `product_option_groups`, `order_item_options` e `ai_product_embeddings`. Essa
  classe está em zero.

As 20 são edição de código Python, revisável em PR normal, sem janela e sem
lock. Ficaram de fora desta rodada por serem outra decisão, não por serem
menos importantes.

---

## O custo real, com os números de hoje

A tabela de locks acima descreve o mecanismo. Estes são os números, medidos em
04/09/2026 — e eles mudam a conversa, porque **nenhuma das duas etapas tem
custo de lock mensurável neste banco.**

| Tabela | Linhas | Tamanho |
|---|---|---|
| `ai_product_embeddings` | 272 | 3,2 MB |
| `customer_addresses` | 5 | 80 kB |
| `admin_users` | 4 | 80 kB |
| `customers` | 3 | 96 kB |
| `ai_feedback` | 1 | 64 kB |
| `coupon_redemptions` | 0 | 64 kB |
| `order_item_options` | 0 | 24 kB |

A etapa 1 não varre nada por desenho, e a etapa 2 varreria 3,6 MB somados.
**O risco que sobra não é o tamanho: é a FILA.** `ACCESS EXCLUSIVE` não convive
com nenhuma outra transação tocando aquela tabela — uma consulta longa em curso
segura o `ALTER`, e todo mundo que chegar depois fica atrás dele. Isso não
depende de haver uma linha ou um milhão, e é o motivo de rodar fora do
movimento.

### O detalhe da etapa 2 que só aparece quando as tabelas crescerem

O `env.py` abre **uma transação para o upgrade inteiro**, e lock só se solta no
commit. Na etapa 2, o `SET NOT NULL` da primeira coluna toma `ACCESS EXCLUSIVE`
sobre `admin_users` e **segura até o fim da revisão** — inclusive durante os 14
`VALIDATE` seguintes. Como a lista é alfabética, a tabela que fica travada por
mais tempo é a primeira, e a que fica menos é a última:

> tempo travado de uma tabela ≈ soma das varreduras que vêm DEPOIS dela.

Hoje isso é irrelevante (a soma inteira é de milissegundos). No dia em que
`order_item_options` tiver milhões de linhas, é o número que decide a janela —
e a saída, se ela apertar, é quebrar a etapa 2 em uma revisão por tabela, não
reordenar a lista.
