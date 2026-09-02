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
| 1.4 | `divergencias_orm_schema.py` no portão como aviso | **feito** |
| 2 | Dublês falsos na suíte inteira | **feito** — 141 achados, 141 convertidos |
| 3 | Testes dependentes da hora | **feito** — 1 achado real, 3 serviços com relógio injetável |
| 4 | Query do `tracking_token`, pronta para colar | **feito** — testada nos tres estados possiveis |
| 5.1 | Plano do histórico de chat em memória | **feito** — plano escrito, nada implementado |
| 5.2 | Plano do índice ANN no pgvector | **feito** — os dois números que faltavam, medidos |
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

---

## 1.4 O script no portão, como AVISO

`scripts/divergencias_orm_schema.py` ganhou `--limite N`, e o CI o roda no job
`banco`, depois de `pytest -m db`:

```yaml
- name: Divergencias entre o ORM e o schema (aviso)
  if: always()
  continue-on-error: true
  run: python scripts/divergencias_orm_schema.py --url "$TEST_DATABASE_URL" --limite 42
```

- **acima do limite** → `::warning::` com quantas são novas e o que fazer;
- **abaixo** → `::notice::` lembrando de baixar o limite (limite folgado deixa a
  próxima entrar de graça);
- **igual** → nada.

**Aviso e não falha, e a razão não é comodidade.** As 42 são herdadas: o schema
nasceu à mão no Supabase e o `Base.metadata` foi escrito depois. Nenhuma delas
veio de um commit. Portão vermelho contra dívida herdada é portão que se aprende
a ignorar — e o dia em que ele acusasse uma divergência **nova** seria o dia em
que alguém o desligaria para conseguir entregar. `continue-on-error: true`
garante o verde do job mesmo que o passo estoure por qualquer motivo.

**O schema que ele inspeciona é o que o passo anterior deixou.** A fixture
`engine_de_teste` apaga e remonta o schema no *setup* e não o derruba no fim,
então depois de `pytest -m db` o banco está em head. Não há segunda montagem em
YAML de propósito — seria uma segunda versão do procedimento para envelhecer
sozinha, o mesmo motivo que o comentário do passo anterior já dá. Com
`if: always()`, se a suíte `db` falhar o script diz "Nenhuma tabela no schema",
que é um aviso honesto e não um número errado.

**O limite mora no `ci.yml`**, não numa constante do script: mudar o número
esperado vira um diff que alguém revisa.

O README ganhou o comando local e a explicação de por que ele é aviso.

---

## 2. Dublês falsos: a varredura, e o que ela achou

### O método, porque ele é reaproveitável

Varredura por **AST**, não por grep. Para cada `SimpleNamespace(...)` da suíte,
as chaves passadas são comparadas com:

- as colunas de cada tabela do `Base.metadata` **mais os relacionamentos**
  (`option_groups` é atributo legítimo de `Product` e não é coluna);
- os campos de **todo `BaseModel` do `src/`** — inclusive `src/ai/schemas/`,
  que a primeira versão da varredura não alcançava;
- (acrescentado depois) as **dataclasses** de `src/integrations/`, que é onde
  moram `PaymentIntent`, `PaymentWebhookEvent` e `DeliveryEstimateResult`.

Casamento ≥ 60% das chaves = candidato a dublê de **dado**. O resto dos
`SimpleNamespace` da suíte é dublê de **colaborador**, que o CLAUDE.md permite.

**Resultado: 141 dublês de dado**, em 35 arquivos.

O script está em `scratchpad/` de sessão, não no repositório — ele foi
ferramenta de garimpo, e o que fica é o conserto.

### O que a varredura corrigiu no meu próprio julgamento

Três "atributos inventados" que ela acusou **não eram invenção**: eram
casamento errado do meu lado, e o tipo real existe.

- `raw_status`/`raw_status_detail` num "`StartPaymentResponse`" → o tipo é
  **`PaymentIntent`**, a dataclass que `create_payment` devolve.
- `event_id`/`raw_status` num "`orders`" → é **`PaymentWebhookEvent`**.
- `latitude`/`longitude` numa "`DeliveryEstimateResponse`" → é
  **`DeliveryEstimateResult`**, e **essa diferença morde de verdade**: o
  `Result` (dataclass) tem as coordenadas e o `Response` (schema) **não** —
  `to_response()` as remove. `OrderService` lê `delivery_estimate.latitude`
  ao gravar o pedido. Quem dublasse o resultado com o schema da resposta
  descreveria um objeto sem as duas coordenadas.

### A fábrica: `tests/fabricas.py`

Irmã de `fabricas_db.py`, e a diferença é a única que importa: aquela recebe
`db` e **grava**; esta não toca em banco. `Branch(...)` sem `db.add()` é objeto
Python comum — serve na suíte rápida inteira.

Funções: `restaurante`, `filial`, `configuracoes`, `produto`, `cliente`,
`endereco`, `usuario_do_painel`, `forma_de_pagamento`, `horario`,
`estimativa_de_entrega`.

**O que ela não dá, e está escrito no módulo:** `default=` de coluna
(`Branch().is_open` é `None`, não `True` — o default é aplicado no INSERT), e
consistência referencial. Por isso cada função escreve explicitamente o que o
banco escreveria.

Sem um lugar único, cada teste escreve a própria filial de mentira — foi assim
que a suíte chegou a 141. Escrever `Branch(...)` à mão nos 141 também
resolveria, com o defeito de a próxima coluna obrigatória exigir 141 edições.

### Resultado: 141 → **0**

Nenhum `SimpleNamespace` da suíte dubla model, schema ou dataclass deste
repositório. Os que sobraram — e são muitos — dublam **colaborador**: um
repositório, um serviço, um cliente HTTP, o `db`. É para isso que ele serve.

Os tipos que entraram no lugar: `Branch`, `Product`, `RestaurantSetting`,
`Restaurant`, `Customer`, `CustomerAddress`, `AdminUser`, `Order`,
`OrderItem`, `OrderStatusHistory`, `BranchBusinessHour`, `BranchPaymentMethod`,
`BranchDeliveryTimeBand`, `RestaurantBanner`, `RestaurantCoupon`,
`CouponTemplate`, `CouponRedemption`, `Category`, `PrintingSector`,
`PrintAgent`, `PrintAgentCommand`, `CashbackRule`, `CashbackTransaction`,
`IdempotencyKey`, `EmailVerificationCode`, `PasswordResetCode`,
`ProductOption`, `ProductOptionGroup` · `ProductResponse`, `AddressInput`,
`OrderItemSelectedOptionInput`, `ChatLLMResponse` · `PaymentIntent`,
`PaymentWebhookEvent`, `DeliveryEstimateResult`.

### O que a conversão achou, além do que ela conserta

1. **`restaurant_settings.payment_methods` (test_menu_service).** A coluna
   **saiu da tabela** na revisão `20260820_0027` — quem manda em forma de
   pagamento é `branch_payment_methods`. O dublê ainda a passava: o teste
   estava verde descrevendo uma linha que não existe há 8 revisões.
   `RestaurantSetting(payment_methods=...)` é `TypeError`.

2. **`orders.tracking_token` (test_order_tracking, 2 lugares).** O model **não
   mapeia** essa coluna — só `tracking_token_hash`. O dublê escrevia o token em
   texto puro, descrevendo o pedido que a `20260812_0017` apagou. E o mesmo
   arquivo tem, três funções acima, um `assertFalse(hasattr(stored,
   "tracking_token"))` — o teste afirmava a ausência num lugar e a inventava no
   outro.

3. **`make_code_row` (test_auth_service) servia duas tabelas diferentes.**
   Escrevia `reset_token_hash`/`reset_token_expires_at` **sempre**, mas
   `email_verification_codes` não tem essas colunas — só
   `password_reset_codes`. Virou um parâmetro `modelo`, e agora pedir
   `reset_token_hash` de um código de e-mail levanta na hora.

4. **`DeliveryEstimateResult` × `DeliveryEstimateResponse`.** O dataclass tem
   `latitude`/`longitude`; o schema **não** (`to_response()` as remove). E
   `OrderService` lê `delivery_estimate.latitude` ao gravar o pedido. Dublar o
   resultado com a forma da resposta descreveria um objeto sem as coordenadas.

5. **`total=10` em dublê de pedido** (dois arquivos). A coluna é `Numeric`, que
   volta `Decimal`. `int` ali é outro tipo, e é o mesmo defeito do `price` que
   o CLAUDE.md registra na voz.

6. **Um teste meu, verde pelo motivo errado** — o do §1.1, com
   `discount_type="percentage"`. Está contado lá.

### O procedimento que ficou

Todo `pytest.raises` novo passa por uma conferência: **a mesma chamada com o
dado certo NÃO pode levantar.** Foi o que pegou o §1.1, e é barato.


### O zero virou trava

`scripts/dubles_de_dado.py` (a varredura, agora no repositório) e
`tests/test_dubles_de_dado.py` (quatro testes rápidos):

1. **nenhum dublê de dado na suíte** — o assert nomeia arquivo:linha, o tipo
   provável, e manda construí-lo;
2. **o varredor enxerga os três lugares** (tabela, schema Pydantic,
   dataclass) *e* os campos certos. Sem isto o teste 1 poderia estar verde
   porque o varredor perdeu os tipos em silêncio — `walk_packages` engole
   `ImportError` de propósito, para módulo quebrado não derrubar a varredura;
3. **um dublê plantado num `tmp_path` é ACUSADO** — a prova de que o vermelho
   existe;
4. **um dublê de colaborador plantado é DEIXADO PASSAR** — se este virar
   vermelho, a regra deixou de ser seguível.

A varredura por dataclass, que a versão de garimpo não tinha, achou mais 8:
`ActivePaymentCredential` dublada sem `environment` — e é `environment` que
decide se o cartão é oferecido (`sandbox` não oferece).

### As outras duas classes do item 2

- **Dublê por dicionário:** um só (`test_coupons.py:79`), e já estava certo —
  é um `dict` de kwargs que alimenta `Customer(**values)`. Não há dicionário
  fazendo papel de model na suíte.
- **Mock permissivo:** **não há `MagicMock`/`Mock` em lugar nenhum** — a suíte
  usa só `patch`, e os fakes são classes escritas à mão com contrato explícito.
  O que existe é outra coisa: 24 usos de
  `patch.object(OrderService, "to_order_detail_response", return_value="detail")`
  seguidos de `assertEqual(result, "detail")` — asserção **tautológica**, que
  só diz que o dublê devolveu o que mandaram devolver.

  Em 23 delas há assertions de verdade ao lado (`order.status`, `db.events`,
  o histórico), então a linha é ruído. **Numa não havia**:
  `test_owner_restaurant_reads_its_own_order`, cujo nome promete isolamento e
  cuja única asserção era a tautologia — ele passaria com o repositório
  ignorando `restaurant_id` inteiro. Ganhou as duas asserções que provam o
  que o nome diz: o par `(order_id, restaurant_id)` que chegou ao repositório,
  e que o objeto serializado é o pedido do B.


---

## 3. Testes que dependem da hora

### O método: rodar a suíte inteira com o relógio congelado

Não bastava grepar `datetime.now()` — 102 ocorrências, e a maioria é legítima.
O que decide é **empírico**: um plugin de pytest descartável (não entra no
repositório) troca `datetime`/`date` por subclasses congeladas em **todo módulo
cujo arquivo está sob a raiz** do projeto, e a suíte rápida roda em instantes
adversariais. O que falhar num instante e passar noutro é a classe procurada.

**Dois detalhes do plugin custaram a acertar, e os dois valem registro:**

1. **Congelar em `pytest_configure` não alcança os testes.** Os módulos de
   teste só existem em `sys.modules` depois da coleta. Congelando só o `src/`,
   os dois lados da conta ficam em relógios diferentes — e isso produz falha
   que *parece* achado. O plugin congela duas vezes: em `pytest_configure` e
   em `pytest_collection_modifyitems`.
2. **Filtrar por nome de módulo não alcança os testes.** `pythonpath = .` faz o
   pytest importá-los como `test_x`, sem prefixo `tests.`. O filtro certo é
   pelo caminho do `__file__`.

Antes de acertar os dois, a varredura acusava 24 falhas; depois, 17 — e as 7
que sumiram eram do próprio plugin.

Instantes rodados: **12:00**, **23:59:30**, **00:00:30 do dia seguinte**,
**domingo 03:00** e **31/12 23:00**, todos no fuso da loja.

### O achado

**`tests/test_branch_hours.py::EnsureOpenTests::test_open_branch_returns_the_current_period`
falhava 59 segundos por dia** — o único teste que falhou às 23:59:30 e passou
às 12:00.

O comentário dele dizia, com todas as letras: *"Dia inteiro aberto para não
depender da hora em que o teste roda."* A faixa era `00:00–23:59`, e
`_period_covers_same_day` compara `current_time <= closes_at`: às **23:59:30**
a faixa não cobre nada, a filial fica fechada e `ensure_branch_is_open` levanta
400. O vermelho não apontaria para a hora em lugar nenhum.

**Por que só esse método:** `find_current_period` sempre aceitou um `now`;
`ensure_branch_is_open` não tinha como passar um — lia o relógio da máquina.

### O conserto: relógio injetável em três serviços

Mesmo desenho que `CouponService.clock` já usava — a convenção existia e não
tinha sido aplicada aqui:

| Serviço | O que lia o relógio |
|---|---|
| `BranchHoursService` | `ensure_branch_is_open` (agora aceita `now`) e `find_current_period` |
| `DeliveryEstimateService` | `_resolve_prep_time` |
| `BranchAvailabilityService` | `list_availability` |

Como os testes constroem esses serviços por `__new__`, **esquecer de injetar é
`AttributeError` alto** em vez de silenciosamente voltar ao relógio da máquina.
Foi o que aconteceu: 65 testes de `test_delivery_estimate` e
`test_branch_availability` ficaram vermelhos até declararem o instante — e os
dois arquivos carregavam o mesmo buraco de 23:59 sem saber.

Também virou instante fixo o `created_at=datetime.now()` de
`test_admin_order_filters` (a lista não lê a hora para nada).

### O buraco de 23:59 NÃO foi consertado, e está afirmado

`test_a_faixa_ate_23_59_nao_cobre_23_59_30` afirma o comportamento. Ele está
**correto**: o lojista disse "até 23:59", e 23:59:30 é depois disso.

**Uma correção ao que eu tinha escrito antes:** cheguei a documentar que
`00:00–00:00` significaria 24 horas. Conferi contra a função e **é falso** —
`opens_at <= closes_at` é verdadeiro, então cai no ramo do mesmo dia e cobre
exatamente a meia-noite. Hoje não há forma limpa de cadastrar 24 horas:
precisaria de `closes_at` com segundos, e nem o painel nem `BusinessHourInput`
pedem segundos. Custo de mexer no formato do cadastro > custo de um minuto por
dia; fica anotado, não mexido.

### O resultado, e o limite honesto do método

Depois do conserto: **nenhuma falha nova em nenhum dos cinco instantes.** As 17
que sobram são artefato do plugin — PyJWT valida `exp` com `time.time()`, que é
C e não passa pelo `datetime` que o plugin troca. Elas são **idênticas nos cinco
instantes** (nem uma a mais, nem uma a menos), que é como se sabe que são ruído.

**O que este método NÃO cobre: a suíte `db`.** Lá o `created_at` sai do
`server_default now()`, que é o relógio do *Postgres* — congelar o Python
produziria falha falsa. Esses foram lidos à mão, um a um: `periodo_de_relatorio`
(que já tem o cuidado documentado, com `hoje + 1` por causa do fuso),
`test_admin_avaliacoes_db` e `test_cardapio_por_filial_e2e_db` (janela de ±1
dia), e as quatro suítes de retenção (margens em DIAS). Nenhum com margem de
borda.

---

## 4. `tracking_token` — a query para colar amanhã

A rodada 1 disse *"pelos três indícios, provavelmente já foi aplicado"*.
Provavelmente não serve para o item mais perigoso da lista. Abaixo está o que
**responde**.

**Nada disto foi executado contra produção.** As duas consultas foram testadas
contra o Postgres de teste, **nos três estados possíveis**, forçados à mão
dentro de uma transação revertida.

São duas, e a separação não é estética: a consulta 2 lê
`orders.tracking_token_hash`, e o Postgres resolve nomes de coluna **antes** de
executar. Numa base anterior à `0016` a coluna não existe e a consulta inteira
morreria com `column does not exist` — falhando exatamente no cenário que ela
existe para detectar. A consulta 1 só toca catálogo e sobrevive a qualquer
estado.

### Consulta 1 — o schema. Rode SEMPRE, e primeiro

Cola direto no SQL editor do Supabase. Só leitura, `ACCESS SHARE`, sem
`docker exec`.

```sql
SELECT 'coluna em claro (tracking_token)' AS o_que,
       CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='public' AND table_name='orders'
               AND column_name='tracking_token')
            THEN 'EXISTE' ELSE 'nao existe' END AS resposta
UNION ALL
SELECT 'coluna do hash (tracking_token_hash)',
       CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='public' AND table_name='orders'
               AND column_name='tracking_token_hash')
            THEN 'EXISTE' ELSE 'nao existe' END
UNION ALL
SELECT 'o hash aceita NULL?',
       COALESCE((SELECT CASE WHEN is_nullable='YES' THEN 'ACEITA'
                             ELSE 'NOT NULL' END
                   FROM information_schema.columns
                  WHERE table_schema='public' AND table_name='orders'
                    AND column_name='tracking_token_hash'), 'a coluna nem existe')
UNION ALL
SELECT 'indice ix_orders_tracking_token_hash',
       COALESCE((SELECT CASE WHEN indisunique THEN 'EXISTE e e UNIQUE'
                             ELSE 'existe e NAO e unique' END
                   FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                  WHERE c.relname='ix_orders_tracking_token_hash'), 'nao existe')
UNION ALL
SELECT 'revisao do alembic',
       COALESCE((SELECT string_agg(version_num, ', ') FROM alembic_version), 'tabela vazia')
ORDER BY 1;
```

### O que cada resultado significa

Leia pelas **duas primeiras linhas**; as outras três confirmam.

| `tracking_token` | `tracking_token_hash` | Onde você está | O que fazer |
|---|---|---|---|
| não existe | EXISTE | **as duas revisões já rodaram** | **nada.** O deploy não está pendente; `docs/deploy-hash-do-tracking-token.md` e a §1 da rodada 1 não se aplicam mais |
| EXISTE | EXISTE | **entre as duas etapas** | falta só a `0017`. Rode a consulta 2 e siga da "ETAPA 2" da rodada 1 |
| EXISTE | não existe | **antes da `0016`** | o roteiro da rodada 1 vale inteiro, do começo |
| não existe | não existe | **não é o banco certo** | pare. Confira a conexão — `orders` sem nenhuma das duas não é um estado que este repositório produz |

As três linhas de confirmação, e o que uma resposta fora do esperado significa:

- **`o hash aceita NULL?`** — `NOT NULL` só depois da `0017`. Se disser
  `ACEITA` com `tracking_token` já ausente, alguém rodou o `DROP COLUMN` sem o
  `SET NOT NULL`: schema num estado que nenhuma revisão produz.
- **`indice ix_orders_tracking_token_hash`** — nasce na `0016` e nunca sai.
  `nao existe` com a coluna do hash presente significa índice derrubado à mão,
  e **a unicidade do token não está mais garantida** — dois pedidos podem
  compartilhar link de acompanhamento. É o achado mais grave que esta consulta
  pode produzir.
- **`revisao do alembic`** — se for `20260902_0044` (o head de hoje), as duas
  passaram no caminho e as duas primeiras linhas têm que concordar. **Se
  discordarem, acredite nas colunas, não na revisão**: `alembic_version` é uma
  linha de texto que um `stamp` errado reescreve sem tocar em nada. Mais de um
  valor ali é banco com cabeça dividida — pare e não migre.

### Consulta 2 — os pedidos. Só se a coluna do hash EXISTIR

```sql
SELECT count(*)                                                AS pedidos_no_total,
       count(*) FILTER (WHERE tracking_token_hash IS NULL)     AS sem_hash,
       count(*) FILTER (WHERE tracking_token_hash IS NOT NULL) AS com_hash,
       min(created_at)::date                                   AS pedido_mais_antigo,
       max(created_at)::date                                   AS pedido_mais_novo
  FROM orders;
```

- **`sem_hash = 0`** é a condição de entrada da etapa 2. A `0017` conta isso
  dentro da própria transação e **levanta antes do `DROP COLUMN`** se sobrar
  alguma — então um número diferente de zero aqui não é perigo, é aviso de que
  ela abortaria.
- **`sem_hash > 0` com `tracking_token` já ausente** é outra coisa, e é grave:
  esses pedidos **não têm link de acompanhamento em lugar nenhum**. Não há rota
  de reemissão. Anote os ids antes de qualquer outra coisa.
- **`pedidos_no_total`** é o número que faltava na §1.2 da rodada 1 para
  dimensionar a janela do backfill. Se a consulta 1 disser que a `0016` ainda
  não rodou, é este número que multiplica o custo do round-trip.

### Por que não `scripts/estado_da_producao.py`

Ele responde outras quatro perguntas (git_sha, revisão, Redis, Mercado Pago) e
**não olha as colunas de `orders`**. A revisão que ele imprime sai de
`alembic_version` — justamente a fonte em que não se deve confiar sozinha aqui.
Rode os dois: ele para o panorama, estas duas para o `tracking_token`.

### Como as duas foram conferidas

Contra o Postgres de teste, com os três estados forçados dentro de uma
transação revertida (`ALTER TABLE ... DROP COLUMN` / `ADD COLUMN`, depois
`ROLLBACK`). Saída de cada um:

| Estado forçado | `tracking_token` | `tracking_token_hash` | aceita NULL? | índice |
|---|---|---|---|---|
| head de hoje | não existe | EXISTE | NOT NULL | EXISTE e é UNIQUE |
| antes da `0016` | EXISTE | não existe | a coluna nem existe | não existe |
| entre as etapas | EXISTE | EXISTE | ACEITA | EXISTE e é UNIQUE |

A linha do meio é a que importa: é o estado em que a consulta 2 quebraria, e a
consulta 1 respondeu inteira.

### O que ficou dependendo do resultado

Nada desta rodada. Se a consulta 1 disser "antes da `0016`", a §1 da rodada 1
volta à mesa com dois números novos (a contagem e o ms/round-trip da §1.2) —
mas isso é decisão de outra madrugada.

---

## 5.1 Histórico do chat em memória — o plano, sem implementar

**Nada foi implementado.** Isto é para você ver antes.

### O que existe hoje, exatamente

`src/services/chat_service.py`, três funções de módulo e um dicionário:

```python
_SESSION_HISTORY: dict[str, SessionState] = {}      # linha 52
_MAX_SESSION_MESSAGES = 20                          # 10 turnos
_SESSION_TTL = timedelta(hours=1)
```

`_get_session_conversation` lê, `_store_session_turn` escreve, e
`_cleanup_inactive_sessions` varre o dicionário a cada turno apagando sessão
parada há mais de uma hora.

**A chave é o `session_id` que o CLIENTE manda** (`ChatRequest.session_id`,
`min_length=1`), e `POST /chat` **não tem autenticação nenhuma** — de
propósito: o Rapi existe para quem ainda não tem conta.

### Por que isso não está quebrado hoje, e por que é frágil

O `Dockerfile` sobe `uvicorn` **sem `--workers`**: um processo. Com um
processo só, o dicionário funciona.

O que ele já custa hoje:

- **todo deploy zera a conversa.** Quem estava no meio de um pedido volta a
  falar com um assistente que não lembra do que ele acabou de dizer. Não é
  hipótese — é o comportamento de toda subida de imagem;
- **é o único estado do processo que o cliente percebe.** `MenuGeneration`,
  o cache de embedding e o contador de resgates também vivem no processo, mas
  os três só custam trabalho repetido. Este custa a conversa.

E o que ele custaria no dia em que alguém precisar de mais capacidade:
`--workers 2` no `CMD`, ou uma segunda réplica atrás do Traefik, e a conversa
passa a **existir em um worker e não no outro**. O cliente alterna entre
lembrar e esquecer conforme o balanceamento. **Sem erro, sem log, sem nada** —
a mesma forma dos piores defeitos que esta rodada catalogou.

**É essa a razão de mexer agora**: hoje o conserto é uma troca de
implementação com um worker de testemunha; depois de escalar, ele vira
depuração de um sintoma intermitente em produção.

### Para onde ele passa a morar: Redis

Não há decisão nova a tomar sobre a peça. **O Redis já é dependência** e já
guarda três coisas do mesmo caminho (`cliente_redis()` em
`src/ai/services/chat_cache.py`): o cache de embedding, o `MenuGeneration` e o
contador do rate limit. `REDIS_URL` é opcional e `startup_checks` já avisa,
sem derrubar o boot, quando ela falta.

**Postgres foi considerado e recusado.** Uma tabela `chat_sessions` daria
durabilidade que ninguém pediu, e traria o custo que o Redis não tem: escrita
no caminho quente de toda mensagem, `VACUUM` de linhas com TTL de uma hora, e
uma tabela nova de dado pessoal em claro para a LGPD cuidar — quando o valor
inteiro do histórico morre em 60 minutos.

### O que muda no código

Um módulo novo e três chamadas. **Nenhuma assinatura pública muda**, e
`ChatService._answer` não sabe onde o histórico mora.

1. **`src/ai/services/chat_history.py`** — uma classe com o mesmo contrato das
   três funções de hoje:

   - `ler(session_id) -> list[SessionMessage]`
   - `gravar(session_id, mensagem_do_cliente, resposta, agora)`

   Com Redis: uma chave `chat:hist:v1:{sha256(session_id)[:32]}`, valor JSON da
   lista, `EXPIRE 3600` renovado a cada escrita — **o TTL do Redis substitui o
   `_cleanup_inactive_sessions` inteiro**, e é mais correto que ele: hoje a
   limpeza só roda quando alguém conversa, então sessão morta de madrugada
   sobrevive até a manhã seguinte.

   **`session_id` vai HASHEADO na chave, nunca cru.** É a mesma regra que
   `ChatCache.embedding_key` já aplica à mensagem, e pelo mesmo motivo escrito
   lá: chave aparece em `KEYS`, em `MONITOR`, em qualquer dump. O `session_id`
   é escolhido pelo cliente e nada impede que um front use o telefone ou o
   e-mail da pessoa como identificador de sessão.

2. **Sem `REDIS_URL`, cai no dicionário de hoje.** Mesma disciplina do
   `chat_cache`: `cliente_redis()` devolvendo `None` significa "não há Redis",
   não erro. A bancada local (`bancada.bat`) continua funcionando sem Redis, e
   o comportamento nela é exatamente o de hoje.

3. **Falha do Redis no meio não pode derrubar o turno.** `try/except` em volta
   das duas operações: histórico vazio é uma resposta pior, não um 500. É o
   mesmo tratamento que `MenuGeneration.current` já dá.

### O custo, item a item

| | |
|---|---|
| **Latência** | duas idas ao Redis por turno (um `GET`, um `SETEX`), no caminho de uma requisição que já espera **~44 ms só na busca vetorial** e centenas no modelo. Redis na mesma rede é sub-milissegundo; o `socket_timeout=1` de `cliente_redis()` é o teto do pior caso |
| **Memória no Redis** | 20 mensagens × ~200 bytes = **~4 KB por sessão ativa**, e o TTL de 1 h limita o total às sessões da última hora. Duas casas de sessões simultâneas é uma casa de MB |
| **Escrita** | uma por turno. O Redis já leva mais escrita que isso pelo rate limit |
| **Código** | ~80 linhas no módulo novo, 3 linhas trocadas no `chat_service`, e `_cleanup_inactive_sessions` **apagada** |
| **Teste** | a suíte rápida não tem Redis, e continua não tendo: o dublê é o caminho `None`. O que precisa de teste `db`... nada. O que precisa de teste é o **contrato**: mesma sequência de chamadas nos dois backends produz a mesma conversa |

### O que isto NÃO resolve, e é importante dizer

- **Não deduplica conversa entre abas.** O `session_id` continua vindo do
  cliente; duas abas com ids diferentes continuam sendo duas conversas. Isso é
  o desenho, não um efeito colateral.
- **Não sobrevive ao Redis reiniciar.** E não deve: histórico de uma hora não
  é dado durável, e persistir aumentaria a superfície de LGPD sem nenhum
  ganho para o cliente.
- **Não muda nada para a voz.** O atendente de voz não usa
  `_SESSION_HISTORY` (grep em `src/ai/voice/` não devolve nada); o histórico
  dele é do lado da OpenAI, na sessão do Realtime.

### A parte de LGPD, que decide o formato

`ai_feedback.user_message` já mostrou que **gente escreve endereço e telefone
para o Rapi** — é o que `feedback_retention_cutoff` existe para apagar, e está
escrito lá. O histórico do chat é exatamente o mesmo texto.

Três consequências, e as três já estão no plano acima:

1. **Chave hasheada**, pelo motivo do `ChatCache.embedding_key`.
2. **TTL de 1 h no próprio Redis**, e não uma varredura que só roda quando
   alguém conversa. Fica no lado do banco de dados, sobrevive a deploy, e não
   depende de tráfego para acontecer.
3. **Nada de persistência.** Se o Redis de produção tiver `RDB`/`AOF` ligado,
   o texto do cliente passa a existir em disco além do TTL — e a exclusão de
   conta (`CustomerAnonymizationService`) não alcança Redis, exatamente como
   não alcançava `ai_feedback`. **Conferir antes de subir:**
   `CONFIG GET save` e `CONFIG GET appendonly`. Se estiverem ligados, ou se
   desliga para este uso, ou o plano precisa de outra conversa.

### O que eu faria, e o que fica para você decidir

Faria na ordem: módulo novo com o backend de memória primeiro (refatoração
pura, comportamento idêntico, portão verde), e o backend Redis num segundo
commit — assim o primeiro é revisável sem Redis na mesa.

**O que não decido sozinho:** se o Redis de produção tem persistência ligada
(item 3 acima) e se você quer o histórico sobrevivendo a deploy — que é o
ganho principal e também o que aumenta a janela em que o texto do cliente
existe fora do processo.

---

## 5.2 Índice ANN no pgvector — os dois números que faltavam

**Este item não pedia um plano novo: ele já existe.**
`docs/busca-vetorial-e-indice-ann.md` decidiu a questão **duas vezes**
(17/08/2026 por latência, 26/08/2026 por correção) e a decisão é **não criar o
índice**. Escrever um plano do zero seria fazer a pergunta voltar pela quarta
vez.

O que fiz foi outra coisa: o documento **nomeia dois números que ele mesmo não
mediu**, e os dois rodam contra o Postgres de teste. Medi os dois. Nada foi
aplicado a lugar nenhum.

### Buraco 1 — "Custo de construir: **não medido**"

Está escrito assim, com todas as letras, na seção "O que fica registrado".
Agora está:

| Cardápio | `ivfflat` | `hnsw m=16` |
|---|---|---|
| 161 produtos | **0,01 s** | **0,12 s** |
| 5.000 produtos | **0,94 s** | **2,65 s** |
| 5.000 produtos (segunda execução) | **0,46 s** | **6,30 s** |

**A terceira linha é o achado, não ruído a ignorar.** A mesma construção, no
mesmo tamanho, variou de 2,65 s a 6,30 s entre duas execuções da mesma tarde.
Máquina de desenvolvimento tem outra carga; a leitura honesta é **ordem de
grandeza de segundos**, não um número para planejar janela.

E para a decisão que importa isso basta: **segundos de `CREATE INDEX` cabem
dentro do `alembic upgrade` do entrypoint, com a API fora do ar** — não é o
caso da armadilha 5, que é de índice que leva minutos. O item "Como criar, se
um dia for criado" do documento pode passar a dizer isso.

### Buraco 2 — "medido ANTES do filtro por filial, tabela não refeita"

A revisão `20260820_0026` acrescentou `p.branch_id` à consulta, e o documento
avisa que suas tabelas são anteriores a ela. Refeitas, **com** o filtro:

**161 produtos (o cardápio do Júnior), 300 consultas:**

| Cenário | Mediana | p95 | Servidor | O planejador leu | Mínimo de linhas |
|---|---|---|---|---|---|
| **sem índice** | **43,85 ms** | 49,85 ms | **1,32 ms** | `Seq Scan` | 5 |
| ivfflat `lists=12` | 44,32 ms | 50,29 ms | 1,77 ms | `Seq Scan` | 5 |
| hnsw `m=16` | 44,04 ms | 48,09 ms | 1,05 ms | `Seq Scan` | 5 |

Conclusão **inalterada**: o planejador ignora o índice, e o ganho é −1,1% e
−0,4% — ruído. O filtro por filial não mudou o sentido de nada.

**5.000 produtos num restaurante só, 300 consultas:**

| Cenário | Mediana | p95 | Servidor | O planejador leu | Mínimo de linhas |
|---|---|---|---|---|---|
| **sem índice** | 66,89 ms | **88,42 ms** | **26,19 ms** | `Seq Scan` | 5 |
| ivfflat `lists=50` | **44,00 ms** | 48,22 ms | **0,48 ms** | `Index Scan` | 5 |
| hnsw `m=16` | 44,13 ms | **45,33 ms** | 0,68 ms | `Index Scan` | 5 |

### Uma correção de método, e ela vale mais que os números

**As tabelas do documento não podem ser lidas umas contra as outras, e as
minhas também não.** A mesma consulta, no mesmo tamanho de 161 produtos, deu
**52,04 ms** em 17/08 e **43,85 ms** hoje. Nada no repositório mudou para
justificar 8 ms; o que mudou foi a máquina e a carga dela.

O que é comparável é **dentro de uma execução**, que é onde os três cenários
enfrentam as mesmas condições. Toda leitura acima é dentro da execução.

### O que MUDOU de figura em 5.000 produtos

O documento registra, para esse tamanho, ANN **mais lento** na mediana (51,84
contra 46,42 ms) e melhor só na cauda. Hoje o ANN venceu **também na mediana**:
66,89 → 44,00 ms, **+52%**.

A parte que não mudou, e que é a que importa: **o trabalho do servidor
desaba** — 26,19 ms → 0,48 ms. É a mesma história dos dois documentos: acima de
alguns milhares de produtos num cardápio, a varredura sai de baixo do custo
fixo de transportar o vetor e passa a aparecer.

### O que NÃO reproduziu, e por que isso não absolve o ANN

O documento decide pela **recall**, e a evidência é uma frase:

> Uma consulta em 400 com `hnsw` devolveu **zero produtos** onde a busca exata
> devolveu cinco.

**Não reproduzi.** Duas execuções, uma com 300 consultas e outra com **2.000**
e semente diferente: `mínimo 5` em todos os cenários, inclusive `hnsw`.

E isto **não** é motivo para criar o índice — é motivo para tratá-lo pior:

- 1 em 400 na medição original e 0 em 2.300 hoje descrevem a mesma coisa —
  um evento **raro o bastante para não aparecer num benchmark e frequente o
  bastante para ter aparecido**. É a pior faixa possível: não dá para
  reproduzir sob demanda e não dá para descartar;
- os vetores são **sintéticos**, agrupados em 12 temas. O próprio documento já
  avisa que a recall do ANN depende do conteúdo, e o conteúdo aqui não é o de
  um cardápio;
- e o argumento de 26/08 **não é estatístico**. Ele é sobre o que a busca vazia
  *significa* desde a armadilha 46: com ANN, "não temos" passa a querer dizer
  "não temos **ou** o grafo não passou por lá", e as duas chegam ao cliente
  como a mesma frase. Uma taxa de erro baixa não devolve o dono da negativa.

### O que eu recomendo, e o que não recomendo

**Não criar o índice.** A decisão do documento continua de pé, e as medições de
hoje não a tocam — elas fecham dois buracos de documentação e confirmam o
gatilho.

**O gatilho segue o mesmo:** um único restaurante passando de ~3.000 produtos
ativos indexados. O maior cardápio em produção tinha 136. A consulta que
responde está no documento e é só leitura:

```sql
SELECT r.slug, count(*) AS produtos_indexados
  FROM ai_product_embeddings ape
  JOIN restaurants r ON r.id = ape.restaurant_id
 GROUP BY r.slug
 ORDER BY 2 DESC;
```

**O que eu faria antes de qualquer índice, se latência virasse prioridade** —
e o documento já aponta para lá: atacar os ~43 ms de custo fixo, não os 1,3 ms
de varredura. Das duas pistas que ele dá (parâmetro binário em vez de ~20 KB de
texto, e mais acerto no cache de embedding), **a primeira nunca foi medida** e
é a maior fatia isolada da latência do Rapi hoje. Fica anotado como o próximo
item de desempenho — e ele **não** depende de decisão de produção nenhuma.
