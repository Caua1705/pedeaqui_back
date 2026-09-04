# Modelo de dados

42 tabelas mapeadas pelo ORM. Este documento cobre o que cada uma guarda, como
se ligam e por que o isolamento entre restaurantes funciona do jeito que
funciona.

A fonte da verdade do schema é `alembic/versions/`. A pasta `migrations/` tem os
12 `.sql` aplicados a mão antes do Alembic entrar — é arquivo histórico
congelado, **não rode nada de lá**.

**E a fonte da verdade não é o model.** Em 42 colunas o `nullable=` do ORM não
é o que o banco faz cumprir, e nada no projeto confere os dois lados. A lista
está na §6, e importa antes de confiar numa anotação deste documento.

---

## 1. O desenho geral

Seis diagramas e não um, e a razão é prática: 42 tabelas num `erDiagram` só
renderizam como um novelo em que nada se acha. O corte é por **assunto**, que é
como as perguntas chegam — "onde mora a taxa de entrega?", "o que o pedido
congela?" —, e cada tabela aparece no diagrama do dono dela.

Notação: `||` é obrigatório, `o|` é opcional (a FK aceita nulo), `o{` é muitos.
Então `branches |o--o{ admin_users` lê-se "um usuário do painel pode pender de
uma filial, e pode não pender de nenhuma".

**O que o Mermaid não consegue desenhar, e é justamente o que segura o
cardápio:** as quatro FKs COMPOSTAS de `products` e `categories`
(`(branch_id, category_id)`, `(branch_id, printing_sector_id)`,
`(restaurant_id, branch_id)`). Elas estão escritas por extenso na seção do
cardápio, mais abaixo — o diagrama mostra as pontas, e não o par.

### A raiz e a loja

```mermaid
erDiagram
    restaurants ||--o| restaurant_settings : "padrão que a filial herda"
    restaurants ||--o{ branches : "as lojas"
    restaurants ||--o{ admin_users : "quem entra no painel"
    restaurants ||--o{ restaurant_banners : "vitrine"
    restaurants ||--o{ restaurant_payment_credentials : "uma por ambiente"
    branches ||--o{ branch_business_hours : "a semana, weekday 0 = segunda"
    branches ||--o{ branch_delivery_time_bands : "faixa de km"
    branches ||--o{ branch_payment_methods : "formas aceitas"
    branches ||--o{ printing_sectors : "impressora é objeto físico"
    branches |o--o{ admin_users : "escopo de filial, quando há"
```

`restaurant_settings` é 1:1 **opcional**, e os seis campos comerciais dela são
só o padrão: `NULL` na filial significa "herda" (§35 da skill). O estado do dia
— `is_open`, `accepts_delivery`, `accepts_pickup` — é `NOT NULL` na filial e
não herda nada.

### O cardápio, que é da filial

```mermaid
erDiagram
    branches ||--o{ categories : "cardápio por loja"
    branches ||--o{ products : "sem herança"
    restaurants ||--o{ categories : "FK REAL, e NÃO é o filtro"
    restaurants ||--o{ products : "FK REAL, e NÃO é o filtro"
    categories ||--o{ products : "a seção do cardápio"
    printing_sectors |o--o{ products : "nulo = não imprime via de produção"
    products ||--o{ product_option_groups : "escolha o acompanhamento"
    product_option_groups ||--o{ product_options : "adicional"
    products |o--o{ ai_product_embeddings : "o índice do Rapi"
    restaurants ||--o{ ai_product_embeddings : "o recorte da busca"
```

**`restaurant_id` continua nas duas tabelas e NÃO é o filtro.** Consultar
cardápio por restaurante devolve as lojas todas misturadas, com 200 e sem log.
As duas setas de `restaurants` estão desenhadas justamente por isso: elas
existem no banco, e omiti-las do diagrama faria parecer que a consulta errada
nem seria possível.

**`products |o--o{ ai_product_embeddings` e não `||`**, e a distinção saiu do
schema real: `ai_product_embeddings.product_id` **aceita nulo** no banco, ainda
que o model o declare `nullable=False`. É uma das 42 divergências entre o ORM e
o schema real — ver §6.

### O pedido

```mermaid
erDiagram
    restaurants ||--o{ orders : "de quem é a venda"
    branches ||--o{ orders : "onde foi feito"
    customers |o--o{ orders : "nulo = pedido de convidado"
    customer_addresses |o--o{ orders : "entrega"
    restaurant_coupons |o--o{ orders : "desconto aplicado"
    orders ||--o{ order_items : "as linhas, em snapshot"
    products |o--o{ order_items : "só o id; o resto é snapshot"
    order_items ||--o{ order_item_options : "adicionais escolhidos"
    product_options |o--o{ order_item_options : "só o id"
    product_option_groups |o--o{ order_item_options : "de que grupo veio"
    orders ||--o{ order_status_history : "um evento por mudança"
    orders ||--o| order_reviews : "nota e comentário"
    orders |o--o{ idempotency_keys : "24h, guarda a resposta inteira"
```

As setas para `products` e `product_options` existem para o histórico continuar
navegável — **o valor exibido nunca sai delas.** Nome, preço unitário e preço do
adicional estão congelados em `order_items` e `order_item_options`, e
`unit_price_snapshot` **já vem com os adicionais somados** (§2 da skill).

### O entregador, que é da filial

```mermaid
erDiagram
    branches ||--o{ couriers : "o motoboy da loja"
    restaurants ||--o{ couriers : "FK real, e é o WHERE"
    couriers ||--o{ courier_assignments : "as corridas dele"
    orders ||--o{ courier_assignments : "uma ATIVA por vez"
    admin_users |o--o{ courier_assignments : "quem atribuiu"
```

`couriers` pende de **filial**, pelo critério do resto do sistema (setor de
impressão, agente, formas de pagamento): o motoboy sai de uma cozinha, e o
gerente preso a uma filial vê só os dela. Quem serve duas lojas tem dois
cadastros. O entregador **não é `admin_user`**: as credenciais dele (link de
256 bits e código de 6 dígitos) moram nesta tabela em hash.

`courier_assignments` guarda a taxa do entregador **congelada** na atribuição
(`courier_fee_snapshot`), calculada de `branches.courier_fee_base` e
`courier_fee_per_km` sobre a distância que o pedido já tinha. "Uma ativa por
vez" é um índice parcial único em `order_id WHERE unassigned_at IS NULL`;
reatribuir fecha a antiga e abre outra. Excluir um entregador é `deleted_at`,
porque o histórico daqui é o que o dono usa para pagar.

### O cliente, que é global

```mermaid
erDiagram
    customers ||--o{ customer_addresses : "endereços salvos"
    customers ||--o{ email_verification_codes : "cadastro"
    customers ||--o{ password_reset_codes : "recuperação"
    customers ||--o{ account_deletion_codes : "confirma a exclusão"
    customers ||--o{ customer_social_identities : "entrar com Google"
    customers ||--o{ customer_payment_profiles : "um por restaurante"
    restaurants ||--o{ customer_payment_profiles : "a outra metade do par"
    customer_payment_profiles ||--o{ customer_saved_cards : "token do gateway"
    customers ||--o{ cashback_transactions : "saldo por restaurante"
    customers ||--o{ coupon_redemptions : "quem já usou"
    customers |o--o{ ai_voice_sessions : "cota por cliente"
    customers |o--o{ delivery_estimates : "quando há login"
```

`customers` não pende de `restaurants`, e é o único ramo assim: a conta é da
plataforma. O que é por restaurante é o **saldo** (`cashback_transactions`) e o
**perfil de pagamento** — e a credencial salva é do lojista, então o cartão de
uma loja não vale na outra.

**São TRÊS tabelas de código de seis dígitos, e elas não se misturam de
propósito.** `email_verification_codes` confirma o cadastro (e liga a conta do
Google, no caso b), `password_reset_codes` recupera a senha e
`account_deletion_codes` confirma a exclusão da conta de quem não tem senha. A
alternativa — uma tabela com coluna `purpose` — faria a separação depender de
um `WHERE` em toda consulta, e o dia em que alguém o esquecesse seria o dia em
que um código pedido para entrar apagaria uma conta. As três guardam o e-mail
em texto puro e saem juntas na retenção e na exclusão
(`CustomerRepository.TABELAS_DE_CODIGO`).

`customer_social_identities` guarda o par `(provider, sub)` do "entrar com
Google", e **o vínculo é pelo `sub`, nunca pelo e-mail**: o e-mail muda e a
pessoa continua a mesma, então ligar por ele transformaria uma troca de
endereço no Google em perda da conta. O e-mail do provedor não é gravado ali —
uma cópia dele seria dado pessoal numa segunda tabela fora de `customers`.
Vários provedores por cliente são um estado legítimo (o Gmail pessoal e o do
trabalho); o que o UNIQUE impede é o contrário, uma conta do provedor servindo
dois clientes.

### O dinheiro: cupom, cashback e gateway

```mermaid
erDiagram
    coupon_templates ||--o{ restaurant_coupons : "a arte, catálogo global"
    restaurants ||--o{ restaurant_coupons : "o cupom da casa"
    restaurant_coupons ||--o{ coupon_redemptions : "USO: tem pedido, conta no teto"
    orders ||--o{ coupon_redemptions : "o uso, com o pedido junto"
    restaurant_coupons ||--o{ coupon_claims : "RESGATE: só concede visibilidade"
    customers ||--o{ coupon_claims : "digitou o código no Clube"
    restaurants ||--o{ cashback_rules : "enabled nasce FALSO"
    branches |o--o{ cashback_rules : "regra por loja, quando há"
    cashback_rules ||--o{ cashback_rule_weekdays : "em que dias vale"
    restaurants |o--o{ cashback_transactions : "o razão"
    orders |o--o{ cashback_transactions : "crédito, resgate, devolução"
    restaurants ||--o{ restaurant_payment_credentials : "test e production"
```

`coupon_templates` é o único catálogo **global** do schema — ele guarda a arte e
o tipo, e o cupom que desconta é o `restaurant_coupons`. Os dois precisam
concordar no tipo e no valor.

### Impressão, IA e o resto

```mermaid
erDiagram
    branches ||--o{ print_agents : "o balcão"
    branches ||--o{ print_agent_printers : "as impressoras vistas"
    branches ||--o{ print_agent_commands : "fila de comando"
    printing_sectors |o--o{ print_agent_commands : "para qual impressora"
    admin_users |o--o{ print_agent_commands : "quem mandou"
    restaurants ||--o{ ai_feedback : "texto do cliente, expurgo por retenção"
    restaurants ||--o{ ai_voice_sessions : "cota e uso"
    restaurants ||--o{ ai_usage_events : "o que a IA custou"
    branches |o--o{ ai_usage_events : "no texto, de que loja"
    ai_voice_sessions |o--o| ai_usage_events : "uma linha por sessão"
    restaurants ||--o{ admin_error_reports : "relato do lojista"
    branches |o--o{ admin_error_reports : "de que loja"
    admin_users |o--o{ admin_error_reports : "quem relatou"
    restaurants ||--o{ delivery_estimates : "estimativa guardada"
    branches ||--o{ delivery_estimates : "token de 15 min"
```

`ai_feedback` e o `comment` de `order_reviews` **não têm `customer_id`**, e nas
duas a retenção não é faxina de disco: é o mecanismo de exclusão (§38 da
skill). Esticar o prazo "porque disco é barato" troca uma coisa pela outra.

### O WhatsApp: o número da loja e o que saiu por ele

```mermaid
erDiagram
    restaurants ||--o{ whatsapp_channels : "branch_id NULO = a queda"
    branches |o--o| whatsapp_channels : "o número DAQUELA loja"
    whatsapp_channels ||--o{ whatsapp_contact_windows : "janela de 24h, com prazo"
    whatsapp_channels ||--o{ whatsapp_messages : "por qual número saiu"
    orders ||--o{ whatsapp_messages : "o aviso deste pedido"
```

`whatsapp_channels` é a única tabela do schema em que `branch_id` nulo
significa **herança** (o regime de `branches` × `restaurant_settings`, §35 da
skill) dentro de uma tabela só. Ela é tabela, e não coluna em `branches`, por
causa da pergunta do webhook — que chega com um `phone_number_id` e precisa
achar a filial: **não existe UNIQUE que atravesse duas tabelas.**

`whatsapp_contact_windows` guarda TELEFONE e não tem `customer_id`: o prazo é
o mecanismo de exclusão (§38), e aqui ele já vem escrito na linha — é a
própria janela de 24h da Meta.

Desenho inteiro em [whatsapp.md](whatsapp.md).

---

## 2. As tabelas principais

### O restaurante e suas lojas

| Tabela | Guarda | Notas |
|---|---|---|
| `restaurants` | nome, `slug` (UNIQUE), cores da marca, logo, `is_active`, `description`, `assistant_notes` | O `slug` é a identidade pública na URL. Os dois textos têm públicos **opostos**: `description` é a vitrine (sai em `RestaurantPublicResponse`), `assistant_notes` é o contexto do assistente de IA e não sai em resposta pública nenhuma |
| `restaurant_settings` | `min_order_value`, `service_fee_enabled/amount`, `estimated_delivery_time_min/max`, `default_delivery_fee`, `platform_commission_percent`, `voice_enabled` | **1:1 opcional.** Os seis primeiros são só o **padrão** que a filial herda — nenhum pedido os lê direto (ver `operacao-por-filial.md`) |
| `branches` | endereço, lat/lng, regras de entrega da loja (base, por km, piso, teto, raio), **`is_open` / `accepts_delivery` / `accepts_pickup`** e as sobrescritas comerciais | A taxa de entrega e a **operação do dia** são por filial. Nas colunas comerciais homônimas de `restaurant_settings`, **NULL significa "herda"**. O endereço tem **dois conjuntos**, e só um vale — ver abaixo |
| `branch_business_hours` | uma linha por faixa de horário: `weekday`, `opens_at`, `closes_at`, `prep_time_min/max`, `is_closed` | **`weekday` 0 = segunda** (é o `datetime.weekday()` do Python) |
| `branch_payment_methods` | `payment_flow` (`online`/`delivery`), `method_type`, `label`, `enabled`, `earns_cashback` | É a fonte da verdade das formas de pagamento aceitas — e por isso "quais formas geram cashback" é coluna daqui, não lista nova |
| `printing_sectors` | `branch_id`, nome, `sort_order`, `is_active` | Pende de **filial**, porque impressora é objeto físico dentro de uma loja |
| `admin_users` | `restaurant_id`, `branch_id` (nullable), `role`, e-mail UNIQUE global | `role` ∈ owner, manager, attendant, print_agent |

#### Os dois conjuntos de endereço de `branches`

A tabela tem **treze** colunas de endereço, e elas não são treze campos: são
dois conjuntos que dizem a mesma coisa.

- **O vivo (5):** `address`, `neighborhood`, `city`, `state`, `zipcode`. É o
  que `AdminBranchUpdate` deixa o lojista escrever no painel, e as quatro
  primeiras são `NOT NULL`.
- **O morto (6):** `address_street`, `address_number`, `address_neighborhood`,
  `address_city`, `address_state`, `address_zipcode`. Resto do schema
  pré-Alembic — estão no `schema_baseline.sql` e nenhuma revisão as toca.
  **Nada no código escreve nelas.**
- E `latitude`/`longitude`, que são a origem do cálculo de rota e não têm par.

**Até a correção, o conjunto morto vencia na leitura.** O
`RestaurantService._build_address` resolvia `branch.address_street or
branch.address`, e o resultado é a resposta de `GET /restaurants/{slug}` — o
endereço que o cliente vê no app. Numa filial com as `address_*` preenchidas, o
lojista corrigia o endereço no painel e o app continuava mostrando o antigo:
sem erro, sem log, sem tela onde conferir, porque o painel exibe o valor novo
(ele lê `branch.address`). É a família da armadilha 35 — a coluna crua responde
"o que está sobrescrito", que quase nunca é a pergunta.

Hoje `_build_address` **lê só o conjunto vivo**, e as cinco colunas mortas com
par ficaram órfãs, prontas para sair numa revisão futura.

**`address_number` é a exceção e continua sendo lida.** Ela não tem par vivo —
não existe `branches.number` nem campo de número em `AdminBranchUpdate` —,
então não sobrescreve ninguém, e largá-la apagaria o número da casa do endereço
público de toda filial que o tenha preenchido. Das seis, é a única que ainda
não dá para largar: quem for fazer o `DROP COLUMN` precisa primeiro decidir
onde o número passa a ser escrito.

### O cardápio

| Tabela | Guarda | Notas |
|---|---|---|
| `categories` | `restaurant_id`, **`branch_id`**, nome, `slug`, `sort_order`, `is_active` | Pende de **filial** desde a revisão `20260820_0026`. `slug` é único por `(branch_id, slug)` |
| `products` | `restaurant_id`, **`branch_id`**, `category_id`, `printing_sector_id` (**NULLABLE**), **`catalog_key`** (NULLABLE), `price`, `is_active`, `is_available` | Pende de **filial**: cada loja tem os próprios produtos e preços, sem herança. `printing_sector_id` nulo = **não gera via de produção** (decisão do lojista, não falta de configuração). `catalog_key` liga o mesmo item entre lojas e serve **só a relatório** |
| `product_option_groups` | `min_select`, `max_select`, `is_required`, `is_active` | Os "escolha o acompanhamento" |
| `product_options` | `additional_price`, `is_active` | |

**Nada é apagado no cardápio, só desativado** (`is_active=false`), porque
`order_items.product_id` referencia `products` por FK. Apagar um produto
quebraria pedidos antigos.

**O cardápio é da FILIAL, sem herança.** Não existe "produto do restaurante"
nem sobrescrita por loja: a picanha do Centro e a da Aldeota são duas linhas
independentes. Quatro FKs compostas garantem que as pontas não divirjam:

```
products (branch_id, category_id)        -> categories (branch_id, id)
products (branch_id, printing_sector_id) -> printing_sectors (branch_id, id)
products (restaurant_id, branch_id)      -> branches (restaurant_id, id)
categories (restaurant_id, branch_id)    -> branches (restaurant_id, id)
```

A segunda é a que torna **impossível de gravar** o produto de uma loja
apontando para a impressora de outra — o estado que a armadilha 13 do skill
descrevia. Documento completo: [`cardapio-por-filial.md`](cardapio-por-filial.md).

### O pedido

| Tabela | Guarda |
|---|---|
| `orders` | a linha central — ver detalhe abaixo |
| `order_items` | um item, com **snapshots**: `product_name_snapshot`, `unit_price_snapshot`, `total` |
| `order_item_options` | os adicionais escolhidos, também em snapshot (`option_name_snapshot`, `additional_price_snapshot`) |
| `order_status_history` | um evento por mudança: `status`, `changed_by`, `note` |

`orders` carrega, em blocos:

```
identidade      id, order_number (sequence GLOBAL, UNIQUE),
                tracking_token_hash (UNIQUE — sha-256; o token em claro
                nunca é gravado, ver revisões 0016/0017)
dono            restaurant_id (NOT NULL), branch_id, customer_id (NULLABLE)
snapshot        customer_name_snapshot, customer_phone_snapshot  (ambos NOT NULL)
operação        order_type, status, notes
dinheiro        payment_method, payment_flow, payment_status, paid_at,
                payment_provider, provider_payment_id
valores         subtotal, delivery_fee, service_fee, discount_total, total
desconto        coupon_id, coupon_code_snapshot, coupon_discount_amount,
                cashback_redeemed_amount
comissão        commission_percent, commission_base_amount, commission_amount
endereço        address_street/number/neighborhood/complement/reference/city/state/zipcode
entrega         delivery_latitude/longitude, delivery_distance_km,
                delivery_travel_time_min, delivery_prep_time_min/max,
                delivery_eta_min/max, delivery_estimate_provider, delivery_estimated_at
```

Por que tanto snapshot: o pedido tem que continuar legível se o cliente mudar de
nome, trocar o telefone ou apagar a conta, e se o lojista mudar o preço ou
renomear o produto. `customer_address_id` inclusive é `ON DELETE SET NULL` —
o endereço textual fica em colunas próprias do pedido.

### Cupom e cashback

| Tabela | Guarda | Notas |
|---|---|---|
| `coupon_templates` | catálogo GLOBAL de arte/tipo | |
| `restaurant_coupons` | `code` (**nullable**), `visibility`, `target_segment`, `discount_type`, `discount_value`, `max_discount_amount`, limites e janelas | UNIQUE `(restaurant_id, code)`; `code` nulo = **aplica sozinho no checkout** |
| `coupon_redemptions` | **USO**: `coupon_id`, `customer_id`, `order_id`, `discount_amount`, `status` | UNIQUE em `order_id` **e** em `idempotency_key` |
| `coupon_claims` | **RESGATE**: `coupon_id`, `customer_id`, `claimed_at` | UNIQUE `(coupon_id, customer_id)`. Sem pedido e sem valor — só concede visibilidade. Ver [cupons.md](cupons.md) |
| `cashback_transactions` | `customer_id`, `amount`, `type`, `status` | Ver o aviso abaixo |
| `cashback_rules` | `restaurant_id`, `branch_id` (NULL = padrão da rede), `enabled`, `default_percent`, `min_redeem_balance`, `expiry_days` | Herança por **linha**: a filial tem a regra inteira ou herda a inteira. Ver [cashback.md](cashback.md) |
| `cashback_rule_weekdays` | `rule_id`, `weekday` (0 = segunda), `percent` | O percentual do dia fraco. **Dia ausente herda `default_percent`**, nunca zero |

**O resgate de cashback está pela metade.** `cashback_redeemed` é literalmente
`ZERO` no cálculo do pedido, e `cashback_redeemed_amount` sempre grava zero. A
tabela existe, o saldo é lido e listado, e `CustomerRepository.lock_customer` já
está escrito para o resgate — mas ninguém o chama. O crédito ao completar pedido
também não existe. **O saldo que o cliente vê só muda se alguém escrever na
tabela por fora.**

A revisão `20260822_0032` põe o schema de pé (configuração, CHECK e índices do
razão) mas **não liga nada**: `cashback_rules.enabled` nasce falso em todo
restaurante. O desenho combinado — quando credita, como o resgate trava, como
expira — está em [cashback.md](cashback.md).

### Infraestrutura

| Tabela | Guarda | Notas |
|---|---|---|
| `idempotency_keys` | `scope`, `key`, `request_fingerprint`, `status`, `response_body` (JSONB), `expires_at` | UNIQUE `(scope, key)`. É esse índice que serializa as requisições concorrentes |
| `delivery_estimates` | `token` (UNIQUE), `address_fingerprint`, taxa/distância/ETA, `expires_at` | Reaproveitada na criação do pedido |
| `restaurant_payment_credentials` | `public_key`, `access_token_encrypted`, `webhook_secret_encrypted`, `environment` | Cifrados com Fernet. Uma linha por `(restaurant_id, environment)` |
| `admin_error_reports` | `description`, `error_log`, `screen`, `order_number`, e o `restaurant_id`/`branch_id`/`admin_user_id` que saem do TOKEN | O "deu erro" do painel. Credencial é mascarada antes do INSERT; o resto **vence em 90 dias**, e é assim que sai do banco — a tabela não tem `customer_id` (armadilha 38). `order_number` é número solto, sem FK: é o que uma pessoa digitou |
| `ai_usage_events` | `surface` (`text`/`voice`), `model`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `cost_usd`, `voice_session_id` | Uma linha por turno do `/chat` e uma por **sessão** de voz. `cost_usd` **nulo é "modelo sem preço em `src/ai/custo.py`", nunca zero**; `cached_input_tokens` é subconjunto de `input_tokens`. UNIQUE parcial em `voice_session_id`: o aviso de fim da voz chega duas vezes, e sem ele o custo da conversa dobraria |

---

## 3. Como o isolamento entre restaurantes funciona

**Não há RLS nem schema por tenant.** O isolamento é filtro explícito por
`restaurant_id` em toda consulta, e ele mora nos **repositórios**.

Tabelas que carregam `restaurant_id` próprio: `branches`, `categories`,
`products`, `restaurant_settings`, `restaurant_banners`, `restaurant_coupons`,
`orders`, `admin_users`, `ai_product_embeddings`, `ai_feedback`,
`cashback_transactions`, `restaurant_payment_credentials`, `delivery_estimates`.

**Em `categories` e `products` o filtro que vale é `branch_id`**, e o
`restaurant_id` fica ao lado como redundância amarrada por FK composta:
consultar cardápio por restaurante devolve as lojas todas misturadas.

Duas defesas complementares:

1. **O `restaurant_id` do lojista vem sempre do token, nunca da URL.** Nenhuma
   rota `/admin` aceita restaurante como parâmetro. A listagem de pedidos aceitava
   um slug na URL e isso foi corrigido — uma rota que *recebe* restaurante é uma
   rota que pode esquecer de conferi-lo.
2. **Divergência responde 404, não 403.** Um 403 confirmaria que aquele
   `restaurant_id` existe. Para o lojista, o que não é dele simplesmente não
   existe.

`OrderRepository.get_order_detail` exige `restaurant_id` como parâmetro
obrigatório de propósito: enquanto o filtro era só por `Order.id`, qualquer
lojista com um UUID de pedido em mãos lia o pedido de outro restaurante, com
nome, telefone e endereço do cliente dentro.

O escopo **por filial** é a segunda camada, aplicada em
`api/dependencies/admin_scope.py`. Ver
[autenticacao-e-escopo.md](autenticacao-e-escopo.md).

---

## 4. Por que `customers` é global e `orders` pertence a um restaurante

**`customers` é global** porque a conta é do cliente, não do restaurante.
`email`, `phone` e `cpf` têm UNIQUE de tabela inteira — não existe
`customers.restaurant_id`.

Consequências práticas:

- Uma conta serve para pedir em qualquer restaurante da plataforma. É o modelo de
  marketplace (iFood), não o de app-por-restaurante.
- Endereços salvos e saldo de cashback são do cliente e atravessam restaurantes.
- O login busca por e-mail/telefone sem nenhum escopo de restaurante.
- **O mesmo CPF não se cadastra duas vezes**, nem para restaurantes diferentes.
  Se um dia a plataforma quiser contas separadas por restaurante, esses três
  UNIQUE são o que precisa mudar primeiro.

**`orders` pertence a um restaurante** porque um pedido é uma transação com
*aquele* estabelecimento: tem a filial que prepara, os produtos daquele cardápio,
a taxa daquela filial e o histórico que aquele lojista opera.
`orders.restaurant_id` é NOT NULL; `orders.customer_id` é NULLABLE porque pedido
de convidado é suportado.

**`orders.order_number` é uma sequence GLOBAL e UNIQUE na tabela inteira**, não
uma numeração por restaurante. O primeiro pedido de um restaurante novo pode ser
o número 5471. Se o lojista pedir numeração própria, isso é mudança de schema.

---

## 5. Índices: a convenção

O prefixo canônico é `ix_`. Índices criados a mão antes do Alembic usam outros
nomes (`idx_`), e adotá-los sob o nome da convenção é uma revisão própria — não
basta criar o novo com `IF NOT EXISTS`, porque **`IF NOT EXISTS` casa por NOME,
não por definição**: dois índices com nomes diferentes sobre a mesma coluna são,
para o Postgres, dois índices, mantidos os dois em toda escrita.

`scripts/audit_indexes.py` procura as duas coisas: colisão de nome e duplicata de
definição.

Ver também a armadilha do `if_not_exists` em
`.claude/skills/rapidex-backend/SKILL.md`.

---

## 6. O ORM não é o schema: 42 divergências

Este é o documento do modelo de dados, então ele precisa dizer uma coisa
desconfortável: **em 42 colunas, o que o model declara não é o que o banco faz
cumprir.**

O motivo é histórico e está na primeira seção — o schema nasceu à mão no
Supabase, virou `alembic/schema_baseline.sql`, e o `Base.metadata` foi escrito
*depois*, olhando para tabelas que já existiam. Nada confere as duas coisas uma
contra a outra: `Base.metadata.create_all()` não é usado em lugar nenhum (nem na
suíte `db` — o schema de teste sai do baseline mais as revisões), então o
`nullable=` do model **nunca vira DDL**. Ele é uma anotação que o SQLAlchemy usa
para o type checker e para decidir se manda a coluna no INSERT. Mais nada.

Reproduzir a lista, que é só leitura e roda contra qualquer banco:

```bash
# contra o schema que o repositório constrói
python scripts/divergencias_orm_schema.py \
  --url postgresql+psycopg://pedeaqui:pedeaqui@localhost:55432/pedeaqui_teste
# contra produção
docker exec pedeaqui-api python scripts/divergencias_orm_schema.py
```

As três classes custam coisas diferentes, e é por isso que o script as separa.

### 6.1 ORM diz NOT NULL, banco aceita NULL — 16 colunas

**Risco de leitura.** O model promete `Mapped[str]`, o banco pode entregar
`None`, e quem escrever `cliente.email.lower()` confiando na anotação leva
`AttributeError` numa linha antiga — não no teste, que constrói a linha
preenchida.

| Tabela | Colunas |
|---|---|
| `customers` | `email`, `password_hash`, `birth_date` |
| `ai_feedback` | `user_message`, `assistant_message`, `selected_product_ids`, `created_at` |
| `ai_product_embeddings` | `product_id`, `embedding` |
| `order_item_options` | `option_group_id`, `option_id` |
| `customer_addresses` | `number`, `neighborhood` |
| `admin_users` | `is_active` |
| `coupon_redemptions` | `idempotency_key` |
| `restaurant_coupons` | `valid_until` |

Nenhuma delas é regra que o código relaxou depois: são restrições que o model
**afirma** e que o banco nunca recebeu. Se hoje existe linha nula em alguma é
pergunta de produção, e o `SELECT count(*) WHERE <coluna> IS NULL` responde.

A de `ai_product_embeddings.product_id` é a que já mudou um diagrama: é por
causa dela que o cardápio acima traz `products |o--o{ ai_product_embeddings` e
não `||`.

### 6.2 Banco diz NOT NULL, ORM diz nullable — 20 colunas

**Risco de escrita, mas só onde não há `DEFAULT`** — e é essa distinção que a
saída do script imprime coluna por coluna.

**Dezoito são benignas.** `created_at`/`updated_at` com `DEFAULT now()`,
`sort_order` com `DEFAULT 0`, `is_active` com `DEFAULT true`,
`coupon_templates.discount_value` com `DEFAULT 0`,
`ai_product_embeddings.metadata` com `DEFAULT '{}'::jsonb`. Omitir no INSERT é
seguro, o banco preenche. O que a anotação faz é enganar quem lê: um
`if produto.sort_order is None` é código morto, porque o valor nunca chega nulo.

**Duas mordem, e são as únicas sem `DEFAULT`:**

- `ai_product_embeddings.content`
- `coupon_templates.image_path`

Nas duas, `Modelo(...)` sem a coluna passa no type checker e estoura
`IntegrityError` em runtime. Hoje isso não acontece por acidente de caminho:
**nenhum dos dois models é instanciado em `src/` ou `scripts/`** — o índice
vetorial é escrito por `INSERT` cru em `AIProductRepository`, e
`coupon_templates` não tem rota de escrita. A armadilha está armada para o
primeiro que trocar o SQL cru pelo ORM.

### 6.3 Coluna que o ORM não mapeia — 6

Existem no banco e o model não as enxerga, então `Modelo.created_at` é
`AttributeError` numa tabela que **tem** `created_at`:

| Tabela | Colunas |
|---|---|
| `product_options` | `created_at`, `updated_at` |
| `product_option_groups` | `created_at`, `updated_at` |
| `order_item_options` | `created_at` |
| `ai_product_embeddings` | `created_at` |

Todas com `DEFAULT now()`, então elas se preenchem sozinhas e ninguém sente
falta — até alguém precisar ordenar adicional por data de criação e concluir,
olhando o model, que o dado não existe. Existe.

### 6.4 O que fazer com isto

**Nada, por enquanto, e de propósito.** Alinhar os dois lados é uma revisão que
começa com `ALTER TABLE ... SET NOT NULL` — que varre a tabela inteira e falha
se houver uma linha nula, e é justamente onde não se sabe se há. É a mesma dança
de duas etapas da `0016`/`0017`, para um defeito que ainda não deu sintoma.

O que este documento faz é tirar a lista do escuro. **A anotação do model não é
autoridade sobre o schema** — quando a resposta importar, a autoridade é
`alembic/versions/` e, em último caso, o banco.

## 7. `ON DELETE`: o levantamento, e a decisão que ele pede

**Levantado em 05/09/2026**, contra o Postgres de teste depois de
`alembic upgrade head` — que para este fim é o schema real. **Não é um achado
anterior**: é uma varredura feita agora, para a decisão ficar registrada com
número em vez de impressão.

| `ON DELETE` | FKs |
|---|---|
| **NO ACTION** (o default, quando não se escreve nada) | **39** |
| `CASCADE` | 35 |
| `SET NULL` | 8 |
| `RESTRICT` | 2 |

### O que as 39 significam, e por que a maioria está certa

`NO ACTION` quer dizer *"o `DELETE` do pai falha enquanto houver filho"*. Num
sistema em que **nada é apagado** — cardápio se desativa (`is_active`), canal de
WhatsApp se desativa, pedido nunca some —, esse é o comportamento desejado na
maior parte dos casos, e não uma omissão.

As que são **deliberadas e já têm o motivo escrito no código**:

- `whatsapp_messages.channel_id` — apagar o canal apagaria o registro de que o
  cliente foi avisado, que é o único lugar onde isso é visível depois;
- `order_items.product_id`, `order_item_options.option_id` /
  `.option_group_id` — o histórico que o cliente ainda consulta;
- `orders.customer_id` — exclusão de conta neste sistema é **anonimização**
  (`customer_anonymization_service`), não `DELETE`. A FK que barra o `DELETE`
  é o que impede alguém de trocar um mecanismo pelo outro sem perceber.

### A decisão que sobra, e é de produto

Não é "consertar 39". É responder, para as poucas em que o `DELETE` do pai é um
caminho real de operação, se o certo é falhar (hoje), levar os filhos junto
(`CASCADE`) ou soltá-los (`SET NULL`). As candidatas são as tabelas
**operacionais e descartáveis**, não as de histórico — `delivery_estimates`,
`ai_product_embeddings`, `print_agent_*`, `ai_usage_events`.

Duas coisas para quem for decidir:

- **`CASCADE` numa tabela de histórico é irreversível e silencioso.** É o
  oposto do que este schema faz em todo lugar, e o dia em que doer é o dia em
  que já apagou.
- **A varredura acima é a forma de conferir de novo.** Ela é uma consulta ao
  `pg_constraint`, roda em segundos, e é mais confiável que a memória de quem
  levantou — inclusive a de quem levantou hoje.
