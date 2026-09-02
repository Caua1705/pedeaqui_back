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
