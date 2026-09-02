# Rodada 2 do backend — fonte da verdade

Branch: `rodada/backend-2`. Nunca commitar na `main`. Um commit por item,
verde, com push. Portão sem pipe. `down -v` ao fim de cada bloco.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

A rodada anterior está em `scratchpad/rodada-back.md` e continua válida.

---

## Regras desta rodada (do pedido)

- **Nada de produção.** Nenhum `docker exec`, nenhuma migração aplicada,
  nenhum comando contra o banco de produção. Postgres de teste e leitura de
  código, só.
- **O comando das filiais (§4.1.2 da rodada 1, commit `ec0aaae`) não roda
  nesta madrugada.** O dono roda amanhã. O commit `6fcaccc` fica esperando o
  resultado — não é para subir. Item que dependa desse resultado: anotar e
  seguir.
- Fora da rodada: produção · criar o `print_agent` · aplicar migração ·
  `float × Decimal` · entregadores · máquina de estados · merge na `main`.

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 0 | Scratchpad da rodada | **feito** |
| 1.1 | As 16 colunas: caminho de leitura que quebra | **feito** — 8 com risco real, 4 silenciosas, 4 sem risco |
| 1.2 | Teste que prova que os 2 models não são instanciados | **feito** |
| 1.3 | Revisão de alinhamento escrita, sem aplicar | **feito** — e executada contra o Postgres de teste |
| 1.4 | `divergencias_orm_schema.py` no portão como aviso | pendente |
| 2 | Dublês falsos na suíte inteira | pendente |
| 3 | Testes dependentes da hora | pendente |
| 4 | Query do `tracking_token`, pronta para colar | pendente |
| 5.1 | Plano do histórico de chat em memória | pendente |
| 5.2 | Plano do índice ANN no pgvector | pendente |
| 8 | Relatório final + armadilhas novas na skill | pendente |

---

## 0. Ponto de partida conferido

Postgres de teste de pé (`docker-compose.test.yml`), schema montado do
`schema_baseline.sql` + `alembic upgrade head`, e
`scripts/divergencias_orm_schema.py` rodado contra ele: **42 divergências**,
exatamente a decomposição da rodada 1 (16 + 20 + 6). Números confirmados, não
herdados.

As 16 da primeira classe (ORM diz NOT NULL, banco aceita NULL):

```
admin_users.is_active
ai_feedback.user_message
ai_feedback.assistant_message
ai_feedback.selected_product_ids
ai_feedback.created_at
ai_product_embeddings.product_id
ai_product_embeddings.embedding
coupon_redemptions.idempotency_key
customer_addresses.number
customer_addresses.neighborhood
customers.email
customers.password_hash
customers.birth_date
order_item_options.option_group_id
order_item_options.option_id
restaurant_coupons.valid_until
```

---

## 1.1 As 16 colunas: quem tem caminho de leitura que quebra

**Método.** Para cada uma das 16, grep do atributo em `src/`, e leitura de cada
uso: schema de resposta que declara o campo sem `| None`, expressão Python que
chama método no valor, comparação em WHERE. O que está escrito abaixo foi
conferido no código, não inferido do nome da coluna.

**Antes da lista, o que muda a probabilidade de cada uma.** Nenhum caminho de
ESCRITA do ORM produz nulo hoje em nenhuma das 16 — o SQLAlchemy manda a
coluna no INSERT porque o model a declara `nullable=False`. O nulo entra por
**linha escrita fora do ORM**: SQL manual no Supabase, script de importação,
linha anterior à criação da coluna. Três das 16 têm `DEFAULT` no banco
(`admin_users.is_active` = `true`, `ai_feedback.created_at` = `now()`,
`ai_feedback.selected_product_ids` = `'{}'`), e essas só ficam nulas por INSERT
que escreva `NULL` **de propósito** — são as menos prováveis. As outras 13 não
têm default: basta o INSERT manual esquecer a coluna.

### As 8 com risco real (500 ou porta trancada)

| Coluna | O que quebra | Onde |
|---|---|---|
| `restaurant_coupons.valid_until` | `AttributeError` em `CouponService._aware(None)` | **checkout e preview de cupom** |
| `restaurant_coupons.valid_until` | `ValidationError` (`valid_until: datetime`) | lista de campanhas do painel |
| `customers.email` | `ValidationError` (`CurrentCustomerResponse.email: str`) | `GET /customers/me` e exportação LGPD |
| `customers.birth_date` | `ValidationError` (`birth_date: date`) | os mesmos dois |
| `customer_addresses.number` | `ValidationError` (`CustomerAddressResponse.number: str`) | `GET /me/addresses` e exportação |
| `customer_addresses.neighborhood` | idem | idem |
| `order_item_options.option_group_id` | `ValidationError` (`OrderItemOptionGroupResponse.option_group_id: UUID`) | detalhe do pedido, comanda, painel |
| `order_item_options.option_id` | `ValidationError` (`OrderItemOptionResponse.option_id: UUID`) | idem |
| `admin_users.is_active` | `ValidationError` (`AdminUserDetailResponse.is_active: bool`) | `GET /admin/users` |

**A pior é `valid_until`, e por um motivo específico.** É a única que quebra em
Python **antes** de chegar a um schema de resposta, e o lugar onde quebra é o
caminho do dinheiro. A consulta que protege a vitrine
(`list_in_window`, com `valid_until >= now`) **não protege o checkout**: linha
nula nunca casa naquele WHERE, mas `preview` e `lock_and_validate_for_order`
chegam ao cupom por `_find_coupon` (id ou código), que não filtra janela
nenhuma. Um cupom com `valid_until` nulo é invisível na lista e derruba quem
digitar o código.

**Duas notas que mudam a leitura da tabela.** As duas de
`order_item_options` são as menos alcançáveis: as FKs para
`product_option_groups` e `product_options` são `NO ACTION` (conferido em
`pg_constraint`), então apagar um grupo de opções é **recusado** em vez de
anular a linha histórica — não há `ON DELETE SET NULL` para produzir o nulo. E
`admin_users.is_active` no login não quebra: `if not admin_user.is_active` com
`None` é falsy, então é **falha fechada** — a pessoa não entra, e o sintoma que
chega é "usuário ou senha inválido" para quem tem os dois certos.

### As 4 sem crash, com consequência silenciosa

- **`ai_feedback.created_at`** — o DELETE de retenção é
  `WHERE created_at < cutoff`, e linha nula **nunca casa**. O texto em claro do
  que o cliente digitou para o Rapi ficaria para sempre, e é exatamente o
  resíduo que `feedback_retention_cutoff` existe para apagar (LGPD). Não é 500;
  é dado pessoal que não sai.
- **`coupon_redemptions.idempotency_key`** — a coluna tem `UNIQUE`, e no
  Postgres **NULL é distinto de qualquer outro NULL**. A garantia de
  idempotência some justamente nas linhas nulas: duas resgatariam o mesmo
  cupom sem o índice reclamar. Nenhuma leitura em Python — a chave é escrita e
  nunca consultada por este repositório.
- **`ai_product_embeddings.product_id`** — a busca vetorial faz
  `JOIN products p ON p.id = ape.product_id`, então a linha some do resultado;
  e `list_stale_products` faz `LEFT JOIN ... ON ape.product_id = ...`, então a
  linha órfã nunca é reaproveitada nem limpa. Produto sem indexação, sem erro
  em lugar nenhum.
- **`ai_product_embeddings.embedding`** — `1 - (NULL <=> vetor) >= :min` é
  `NULL`, que não é verdadeiro: a linha é filtrada pelo WHERE. O produto
  simplesmente não existe para a busca falada, em silêncio. É a mesma forma do
  defeito que o CLAUDE.md registra na voz.

### As 4 sem risco nenhum

- **`customers.password_hash`** — `verify_password` declara
  `password_hash: str | None` e devolve `False` sem hash. É o **único** dos 16
  em que a defesa já está escrita, e ela é falha fechada.
- **`ai_feedback.user_message`**, **`.assistant_message`**,
  **`.selected_product_ids`** — nenhuma rota lê a tabela (está escrito em
  `feedback_retention_cutoff` e conferido por grep). `assistant_message`
  aparece só num `WHERE ... = :valor`, onde nulo nunca casa: o voto viraria
  linha nova em vez de UPDATE. Consequência mínima.

### O que ficou executável

`tests/test_colunas_em_desacordo.py` — 12 testes rápidos, um por caminho de
leitura, construindo o **tipo real** transiente com a coluna nula
(`RestaurantCoupon(...)`, `Customer(...)`, `OrderItem(...)` sem sessão e sem
banco), como o CLAUDE.md manda.

**E foi ele que já pagou por si.** Escrito com `discount_type="percentage"`, o
teste do cupom passava **pelo motivo errado**: o `ValidationError` vinha do
`Literal` de `discount_type` e não do `valid_until` nulo. Um teste verde
descrevendo um defeito que ele não estava exercitando — a classe exata do item
2 desta rodada, encontrada dentro do item 1. O que a pegou foi conferir que a
**mesma chamada com o dado certo NÃO levanta**; passou a ser o procedimento
para todo teste de `pytest.raises` daqui em diante. Valor certo: `"percent"`.

---

## 1.2 As 2 que mordem: a trava para quando alguém instanciar

`ai_product_embeddings.content` e `coupon_templates.image_path` são a
interseção mais perigosa do levantamento:

    banco diz NOT NULL  +  coluna SEM DEFAULT  +  model diz nullable

Nessa combinação, `Modelo(...)` **sem** aquela coluna passa no type checker e
passa no `flush()` — o SQLAlchemy nem manda a coluna, porque a anotação diz que
ela é opcional — e estoura `IntegrityError` no INSERT, em runtime. Nenhum teste
existente veria isso antes de acontecer em produção.

**Por que não acontece hoje, e por que o motivo é frágil.** Nada instancia os
dois: o índice vetorial é `INSERT` cru em `AIRepository` (o tipo `vector` não
passa pelo ORM) e `coupon_templates` não tem rota de escrita — o painel escolhe
uma arte que já existe, nunca cria. As duas ausências são **circunstância, não
decisão**. Um seeder de arte nova seria exatamente
`CouponTemplate(name=..., discount_type=...)`.

`tests/test_models_nunca_instanciados.py` — três testes:

1. e 2. varredura **AST** (não grep) de `src/` e `scripts/` atrás de
   `ast.Call` cujo alvo seja um dos dois models. AST importa: para o grep,
   `AIProductEmbedding.content_hash` — que é uso legítimo, e existe em
   `AIRepository` — e `AIProductEmbedding(...)` são a mesma string e coisas
   opostas. O `assert` não proíbe instanciar: ele **explica** e manda passar a
   coluna, corrigir o model, e só então apagar a trava.
3. um teste `db` que mostra uma vez o que a trava evita: `AIProductEmbedding`
   sem `content` → `IntegrityError` do Postgres de verdade. Contra o banco e
   não com dublê de propósito — a regra mora no DDL, e um dublê responderia o
   que o ORM acha, que é o lado errado.

**Visto vermelho.** Acrescentei `CouponTemplate(...)` e `AIProductEmbedding()`
a um script, rodei, e os dois testes falharam nomeando `arquivo:linha`. Depois
restaurei o script.

`tests/` fica de fora da varredura de propósito: é lá que os models **podem**
ser construídos, inclusive no terceiro teste deste mesmo arquivo.

---

## 1.3 A revisão de alinhamento, escrita e não aplicada

**Nada foi executado contra produção.** As duas revisões moram em
`alembic/preparadas/`, diretório que o Alembic **não lê** (`script_location =
alembic` faz ele enxergar só `versions/`).

### Por que duas etapas, e não um `SET NOT NULL`

`SET NOT NULL` toma `ACCESS EXCLUSIVE` **e** varre a tabela. `ACCESS EXCLUSIVE`
bloqueia `SELECT` também. Vezes 16 colunas, numa transação só (o `env.py` não
usa `transaction_per_migration`), isso é a plataforma parada pela **soma** das
varreduras.

O caminho escolhido:

| | lock | varre? |
|---|---|---|
| `ADD CONSTRAINT ... CHECK (c IS NOT NULL) NOT VALID` | `ACCESS EXCLUSIVE`, ms | não |
| `VALIDATE CONSTRAINT` | `SHARE UPDATE EXCLUSIVE` — não bloqueia leitura nem escrita | sim |
| `SET NOT NULL` com a CHECK válida | `ACCESS EXCLUSIVE`, instantâneo | **não** (PG ≥ 12 aceita a CHECK como prova) |
| `DROP CONSTRAINT` | instantâneo | não |

**E as duas etapas não podem ir no mesmo `alembic upgrade`** — pelo mesmo
motivo da transação única: o `VALIDATE` rodaria dentro da transação que ainda
segura o `ACCESS EXCLUSIVE` do `ADD CONSTRAINT`, e o ganho evapora. São duas
execuções, com `ALEMBIC_TARGET` na primeira.

Ganho que não é de lock: **a etapa 1 já recusa nulo novo**. Do commit dela em
diante o buraco para de crescer, mesmo que a etapa 2 demore semanas — e quando
o `VALIDATE` rodar, não há corrida com escrita concorrente.

### Esta migração não tem ponto sem volta

**Nenhum dado é tocado pelas duas etapas.** Os dois downgrades são completos.
Vale dizer em voz alta porque é o oposto exato do outro roteiro do repositório:
a `0017` do `tracking_token` recria a coluna vazia e não há volta.

### A conferência de nulos, antes

`scripts/nulos_nas_colunas_em_desacordo.py` (novo, só leitura, `count(*)` toma
apenas `ACCESS SHARE`). Uma consulta por **tabela**, com
`count(*) FILTER (WHERE col IS NULL)` — tabela grande varrida uma vez, não uma
vez por coluna. A lista de colunas **não está escrita nele**: sai de
`divergencias_orm_schema.comparar()`, extraída para função nesta rodada. Uma
segunda cópia da regra seria a divergência entre os dois scripts esperando
acontecer — o defeito que o próprio script existe para nomear.

Saída: `PRONTA para SET NOT NULL` (0 nulos) ou `DECIDIR O DADO ANTES` (N > 0),
com código de saída 2 no segundo caso.

### **A revisão foi executada de verdade — contra o Postgres de teste**

`tests/test_revisoes_preparadas.py` tem seis testes; o sexto é o que muda a
natureza da entrega:

- as duas revisões **não estão na cadeia** — perguntado ao `ScriptDirectory`
  do Alembic, que é quem o `upgrade` consulta, e não ao caminho do arquivo;
- as duas etapas listam **as mesmas** 16 colunas (a duplicação da lista é
  deliberada: import entre revisões quebraria no `git mv`);
- a lista ainda **descreve o schema de hoje** — comparada com
  `comparar(...).orm_mais_estrito` do banco montado. Revisão futura que alinhe
  ou crie divergência derruba este teste, em vez de a preparada envelhecer
  calada;
- **as duas etapas rodam, as 16 colunas ficam `NOT NULL` de verdade, e os dois
  downgrades devolvem tudo** — dentro de uma transação que volta (o Postgres
  tem DDL transacional), com `finally`, para não estragar o schema de sessão
  que os outros 612 testes `db` compartilham.

Ou seja: "pronta" aqui quer dizer **executada**. O primeiro lugar onde essa
migração roda deixou de ser produção, de madrugada, com a API fora do ar.

### Onde a decisão continua sendo do dono

1. **Etapa 0 em produção**: contar os nulos. Coluna com nulo é decisão de
   **dado** — preencher, apagar a linha, ou tirar a coluna da revisão.
2. **`restaurant_coupons.valid_until` é a única das 16 em que a resposta pode
   ser o contrário.** Cupom sem data de fim é campanha plausível, e há
   precedente: a `20260828_0043` tornou `code` nulo *com significado*. Se o
   produto quiser campanha permanente, o certo é relaxar o model e os schemas,
   não apertar o banco. Hoje não é isso — os três schemas exigem a data e
   `_aware(None)` levanta —, mas a pergunta é de produto e fica aqui escrita.

### Arquivos

`alembic/preparadas/LEIA-ME.md` · `alembic/preparadas/alinhamento_orm_schema_etapa_1.py`
· `alembic/preparadas/alinhamento_orm_schema_etapa_2.py` ·
`docs/alinhamento-orm-schema.md` · `scripts/nulos_nas_colunas_em_desacordo.py`
· `scripts/divergencias_orm_schema.py` (extraída `comparar()`, saída idêntica)
· `tests/test_revisoes_preparadas.py`.

**Portão: 2812 testes verdes** (2200 rápidos + 612 `db`), ruff limpo, openapi e
lock em dia.
