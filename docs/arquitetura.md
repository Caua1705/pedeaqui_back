# Arquitetura do backend

Documento de navegação e depuração. Toda citação é `arquivo:linha` para você abrir direto.

---

## 1. Visão geral

Este backend é a API de um sistema de pedidos de restaurante, multi-restaurante desde a
base: cada restaurante é identificado por um `slug` na URL pública e por um `restaurant_id`
em todas as consultas. Ele serve o cardápio, autentica clientes e lojistas, calcula taxa de
entrega e prazo consultando o Google Maps, aplica cupons, grava pedidos e expõe o painel do
lojista para acompanhar e mudar status. Tem também um chat com IA ("Rapi") que recomenda
produtos por busca vetorial no cardápio. É FastAPI + SQLAlchemy sobre um PostgreSQL
(Supabase) cujo schema já existia antes do Alembic entrar.

---

## 2. Mapa de pastas

```
main.py                    monta o app: middlewares, exception handlers, routers
src/
  api/
    endpoints/             camada HTTP. Uma função por rota. Não tem regra de negócio.
    dependencies/          o que o FastAPI injeta: sessão de banco, cliente logado, lojista logado
    middleware/            middleware ASGI puro (hoje só o limite de tamanho de corpo)
    rate_limit.py          limites por rota e extração do IP real do cliente
    validation_errors.py   log extra quando o Pydantic recusa um corpo em rotas críticas
    chat.py                rotas do chat (fora de endpoints/ por histórico)
  services/                REGRA DE NEGÓCIO. Orquestra, valida, decide, commita.
  repositories/            SÓ consulta SQL. Nenhuma decisão, nenhum HTTPException de regra.
  schemas/                 DTOs Pydantic de entrada e saída (contrato HTTP)
  models/                  modelos SQLAlchemy espelhando as tabelas existentes
  integrations/            clientes de serviço externo (Google Maps Routes + Geocoding)
  ai/                      tudo de IA: prompts, embeddings, busca vetorial, chamada ao LLM
  core/                    config (`config.py`), constantes, validação de config no boot
  db/                      engine, sessionmaker, `get_db`
  utils/                   dinheiro (Decimal), normalização de texto, senha/token, URL de storage
alembic/versions/          migrações versionadas (fonte da verdade do schema hoje)
migrations/                arquivo histórico congelado dos 13 .sql aplicados a mão. NÃO rode.
scripts/                   tarefas de operação: criar lojista, limpar chaves, reindexar IA
tests/                     117 testes, sem banco real (usam fakes)
```

### Regra de quem chama quem

```
endpoint  →  service  →  repository  →  banco
   ↓           ↓
dependency   integration / ai
```

Setas permitidas:

- **endpoint → service**: sempre. O endpoint só recebe, injeta dependências e devolve.
- **endpoint → dependency**: `get_db`, `get_optional_current_customer`, `get_current_admin`.
- **service → repository**: sempre.
- **service → service**: permitido e usado. `OrderService` chama `CouponService`,
  `DeliveryEstimateService`, `IdempotencyService`, `RestaurantService`
  (`src/services/order_service.py:41-48`).
- **service → integration / ai**: permitido. Só o service fala com o mundo externo.
- **repository → qualquer coisa que não seja o banco**: **proibido**. Repositório não
  levanta erro de regra, não decide, não commita (exceto `AIRepository.save_embedding`,
  usado por script fora do ciclo HTTP — `src/repositories/ai_repository.py:110`).
- **endpoint → repository**: **proibido**. Não existe hoje; não crie.

Quem commita: **o service**, sempre. O repositório faz `flush()` para pegar o id gerado, e
o `commit()` fica no service para que várias escritas caiam na mesma transação
(`src/services/order_service.py:212`). Não há `controllers/` — os endpoints são a camada
controller.

---

## 3. O caminho de um pedido

`POST /restaurants/{restaurant_slug}/orders`

### 3.1 Antes de chegar no seu código

1. **Limite de corpo** — `src/api/middleware/body_size.py:32`. Corpo acima de
   `MAX_REQUEST_BODY_BYTES` (262 144 bytes, `src/core/config.py:56`) leva **413** antes de o
   FastAPI carregar o JSON na memória. O middleware corta durante a leitura, não depois.
2. **CORS** — `main.py:66-72`. É o mais externo de propósito, para que até 413 e 429 saiam
   com os cabeçalhos de CORS.
3. **Rate limit** — `src/api/endpoints/orders.py:28`, limite em `src/api/rate_limit.py:39`
   (`10/minute;60/hour` por IP). Estourou → **429**. O IP sai do header `x-real-ip`
   (`src/api/rate_limit.py:44-51`), não do socket, porque atrás do Traefik o socket é o proxy.
4. **Validação do corpo (Pydantic)** — `src/schemas/order_schema.py:62-82`. Aqui morrem, com
   **422**, os erros de forma: `items` vazio ou com mais de 100 itens (`:72`), quantidade
   fora de 1..99 (`:54`), mais de 30 opções por item (`:56-59`), `coupon_id` **e**
   `coupon_code` juntos (`:77-79`), telefone com menos de 8 dígitos (`:20-31`). O
   `coupon_code` vira maiúsculas em `:81` e o telefone vira só dígitos em `:28`.
5. **Cliente logado (opcional)** — `src/api/dependencies/customer_auth.py:23-35`. Token
   ausente, inválido ou expirado **não** dá erro aqui: devolve `None` e o pedido segue como
   convidado. É de propósito — pedido de visitante é suportado.
6. **Idempotency-Key** — `src/services/idempotency_service.py:179-208`. Sem header, passa
   sem proteção com um `warning` no log; com `IDEMPOTENCY_REQUIRED=true` vira 400. Acima de
   200 caracteres, 400.

### 3.2 Dentro do `OrderService.create_order`

Entrada: `src/services/order_service.py:50`.

```
 57  restaurante ativo pelo slug ................ 404 se não achar
 58  order_type ∈ {delivery, pickup} ............ 400
 60  filial ativa E do restaurante .............. 400
 64  resolve o endereço ......................... 401 / 404
 65  exige cliente (logado ou no corpo) ......... 401
─────────────────────────────────────────────────────────────
 71  ►►► RESERVA A CHAVE DE IDEMPOTÊNCIA ◄◄◄
─────────────────────────────────────────────────────────────
 79  endereço completo se for delivery .......... 400
 80  carrega restaurant_settings
 81  estimativa de entrega (Google Maps) ........ 400 se fora de área
 87  produtos válidos, ativos, DESTE restaurante  400
 89  valida opções selecionadas ................. 400
 93  calcula subtotal
 94  valor mínimo do pedido ..................... 400
 95  taxa de serviço
 96  taxa de entrega
─────────────────────────────────────────────────────────────
105  ►►► INÍCIO DO try/except ◄◄◄
106  trava e valida o cupom (SELECT FOR UPDATE) . 400 / 401 / 404
117  total = subtotal + serviço + entrega − descontos
123  normaliza o telefone do snapshot
126  INSERT orders
166  INSERT coupon_redemptions
169  INSERT order_items
180  INSERT order_item_options
187  INSERT order_status_history ("pending")
207  UPDATE idempotency_keys → completed + resposta
212  ►►► COMMIT ◄◄◄
214  except: ROLLBACK e propaga
```

Detalhe por etapa:

| # | O que faz | Onde | Erro |
|---|---|---|---|
| 1 | Restaurante ativo pelo slug | `order_service.py:57` → `restaurant_service.py:42-49` | 404 |
| 2 | `order_type` válido | `order_service.py:58` → `:253-255`; lista em `core/constants.py:3` | 400 |
| 3 | Filial ativa **e** do restaurante | `order_service.py:60` → `branch_repository.py:15-21` | 400 |
| 4 | Resolve endereço | `order_service.py:64` → `:271-279` | 401/404 |
| 5 | Cliente identificado | `order_service.py:65` → `:257-260` | 401 |
| 6 | **Idempotência** | `order_service.py:71-77` | 409/422 |
| 7 | Endereço completo p/ entrega | `order_service.py:79` → `:262-269` | 400 |
| 8 | Settings do restaurante | `order_service.py:80` → `menu_repository.py:20-22` | — |
| 9 | Estimativa de entrega | `order_service.py:81-86` → `:281-316` | 400 |
| 10 | Produtos válidos | `order_service.py:87` → `:317-323` | 400 |
| 11 | Opções por item | `order_service.py:89-92` → `:325-378` | 400 |
| 12 | Subtotal | `order_service.py:93` → `:380-394` | — |
| 13 | Valor mínimo | `order_service.py:94` → `:401-407` | 400 |
| 14 | Taxa de serviço | `order_service.py:95` → `:396-399` | — |
| 15 | Cupom (travado) | `order_service.py:106-114` → `coupon_service.py:240-268` | 400/401/404 |
| 16 | Gravação | `order_service.py:126-189` | — |
| 17 | Commit | `order_service.py:212` | — |

**Passo 4 (endereço) em detalhe** — `order_service.py:271-279`:
- `customer_address_id` sem login → **401**. Um convidado não pode apontar para endereço de conta.
- Com login + `customer_address_id` → busca em `customer_addresses` filtrando **também** por
  `customer_id` (`customer_repository.py:121-126`). Não é seu, é 404.
- Sem `customer_address_id` → usa o `address` inline do corpo.

**Passo 10 (produtos)** — `order_service.py:317-323` chama
`product_repository.py:55-66`, que filtra por `restaurant_id`, `is_active` e `is_available`.
Se o número de produtos encontrados não bater com o número de ids únicos pedidos, é **400**
genérico ("Produto inválido ou indisponível"). Não diz qual — de propósito, para não virar
oráculo de quais UUIDs existem.

**Passo 11 (opções)** — `order_service.py:325-378`. Valida, nessa ordem: grupo pertence ao
produto e está ativo (`:332-336`), opção pertence ao grupo e está ativa (`:337-345`), opção
não repetida (`:346-350`); depois, por grupo: obrigatório foi preenchido (`:358-362`), mínimo
(`:363-367`) e máximo (`:368-372`).

### 3.3 Onde entra a idempotência

`src/services/order_service.py:71-77`, e a posição é intencional (comentário em `:67-70`).

Ela fica **antes de qualquer escrita e antes da chamada paga ao Google Maps**, mas **depois**
das validações baratas (restaurante, filial, endereço). Assim um reenvio devolve a resposta
gravada sem gastar rota do Maps nem criar um segundo pedido, e um payload obviamente inválido
não queima chave.

A reserva é um `INSERT ... ON CONFLICT DO NOTHING` — `src/repositories/idempotency_repository.py:36-48`:

- Pegou a reserva (voltou um id) → segue e executa.
- Não pegou → lê a linha existente (`idempotency_service.py:104`) e decide:
  - fingerprint diferente → **422** (`:114-124`): mesma chave, corpo diferente, é reciclagem.
  - `in_progress` → **409** (`:126-135`).
  - `completed` com resposta → devolve a resposta gravada (`:137-143`), e o
    `order_service.py:77` a converte de volta em `CreateOrderResponse`.

O escopo da chave é `restaurant_id | rota | solicitante` — `idempotency_service.py:48-64`.
O solicitante é `customer:<id>` quando há login, senão `phone:<dígitos>`
(`order_service.py:220-237`). Sem o solicitante no escopo, um cliente adivinhando a chave de
outro receberia o pedido alheio, com nome, telefone e endereço dentro.

O fingerprint é o sha256 do corpo canonicalizado com `sort_keys=True`
(`idempotency_service.py:66-74`) — a ordem das chaves no JSON não muda o hash.

**O ponto que faz o mecanismo funcionar**: a reserva fica na *mesma transação* do INSERT do
pedido. Ela não é commitada separadamente. Se qualquer coisa falhar antes do
`db.commit()` da linha 206, a reserva some junto e a chave volta a ser usável. Não existe
estado "chave reservada, pedido nenhum" preso no banco.

### 3.4 Onde os preços são recalculados

**Todos**, sempre, no servidor. O corpo da requisição não traz preço nenhum — veja
`CreateOrderRequest` em `src/schemas/order_schema.py:62-74`: o cliente manda `product_id`,
`quantity` e `selected_options`, e nada mais.

| Valor | Fonte | Onde |
|---|---|---|
| Preço unitário | `products.price` do banco + soma de `product_options.additional_price` | `order_service.py:417-418` |
| Subtotal | Σ (preço + opções) × quantidade | `order_service.py:380-394` |
| Taxa de entrega | base + km × valor/km, com piso e teto da filial | `delivery_estimate_service.py:493-512` |
| Taxa de serviço | `restaurant_settings.service_fee_amount`, se habilitada | `order_service.py:396-399` |
| Desconto do cupom | tipo e valor do cupom no banco | `coupon_service.py:47-64` |
| Total | `subtotal + serviço + entrega − descontos` | `order_service.py:117` |

Tudo em `Decimal`, arredondado com `ROUND_HALF_UP` para 2 casas em `src/utils/money.py:15-16`.
Nunca `float` no cálculo — `float` só aparece na serialização da resposta
(`money_to_float`, `src/utils/money.py:19-20`).

Os preços também são **congelados em snapshot** dentro do pedido: `product_name_snapshot`,
`unit_price_snapshot`, `option_name_snapshot`, `additional_price_snapshot`
(`order_service.py:409-440`). Mudar o preço do produto amanhã não altera pedido de ontem.

### 3.5 Onde o cupom é travado

`src/services/coupon_service.py:240-268`, chamado de `order_service.py:107`.

1. **Sem cliente autenticado → 401** (`coupon_service.py:250-251`). Convidado não usa cupom.
2. **`SELECT ... FOR UPDATE`** na linha do cupom — `coupon_repository.py:47-58` →
   `:28-29` / `:43-44`. O lock é do registro do cupom, e vale até o commit da transação do
   pedido. É isso que impede dois pedidos simultâneos de furarem o `total_usage_limit`: o
   segundo espera o primeiro commitar e só então conta os resgates.
3. **Avaliação** — `coupon_service.py:66-140`, nesta ordem: cupom é deste restaurante (`:83`),
   está ativo (`:85`), é público (`:87`), já começou (`:89`), não expirou (`:91`), limite total
   não estourado (`:93-95`), limite por cliente (`:111-114`), cooldown em dias (`:115-127`),
   regra de primeiro pedido (`:128-129`) e, por último, valor mínimo do cupom (`:131-136`).
4. Inválido → **400** com o motivo técnico no `detail` (`coupon_service.py:266-267`).

O resgate (`coupon_redemptions`) é gravado **depois** do `INSERT` do pedido, em
`order_service.py:166-167` → `coupon_service.py:270-280`, porque precisa do `order_id`.
A tabela tem `UNIQUE(order_id)` e `UNIQUE(idempotency_key)`
(`src/models/coupon_redemption_model.py:15-18`), e o serviço ainda checa antes de inserir
(`coupon_service.py:271-273`) — cinto e suspensório.

### 3.6 O que acontece dentro da transação e o que é gravado

O bloco `try` vai de `order_service.py:105` a `:216`. Dentro dele, tudo em uma transação:

```
orders                 1 linha    order_service.py:126-165
coupon_redemptions     0 ou 1     order_service.py:166-167
order_items            N linhas   order_service.py:169-179
order_item_options     0..N       order_service.py:180-186
order_status_history   1 linha    order_service.py:187-189  (status "pending", changed_by "system")
idempotency_keys       1 UPDATE   order_service.py:207-211  (completed + response_body em JSONB)
                       ─────────
                       COMMIT     order_service.py:212
```

A resposta é montada em `:190-203` e gravada em `idempotency_keys.response_body`
**antes** do commit (`:207-211`), com o motivo explicado em `:204-206`: assim a resposta
armazenada e o pedido entram no banco atomicamente. Se o commit falhar, não sobra chave
apontando para pedido inexistente.

O `order_number` é uma sequence do Postgres (`src/models/order_model.py:17`), preenchida no
`flush()` da linha 165 e já legível na resposta.

### 3.7 Se algo falhar no meio

**Dentro do `try` (linhas 105–213)**: `except Exception: self.db.rollback(); raise`
(`order_service.py:214-216`). Volta tudo — pedido, itens, opções, histórico, resgate de cupom
e a reserva da chave de idempotência. O lock `FOR UPDATE` do cupom é liberado pelo rollback.
O erro sobe: `HTTPException` vira a resposta HTTP correspondente; qualquer outra exceção vira
500.

**Entre o `begin()` da idempotência (71) e o `try` (105)** — endereço inválido, fora de área,
produto indisponível, abaixo do mínimo: **não há `rollback()` explícito**, e não precisa. Nada
foi commitado; a sessão é fechada em `src/db/session.py:21-26` no `finally` do `get_db`, e
fechar sem commit descarta a transação inteira, incluindo a reserva. O efeito é o mesmo, só
não está escrito. Vale saber, porque olhando o código isolado parece um vazamento.

**O que a transação NÃO desfaz**: a chamada ao Google Maps (já foi cobrada) e o que já entrou
no cache de estimativa (`delivery_estimate_service.py:349`). Nenhum dos dois tem efeito no
banco, então não corrompe nada — só custa.

---

## 4. Outros fluxos

### 4.1 Login do cliente

`POST /auth/login` → `src/api/endpoints/auth.py:41-48` → `src/services/auth_service.py:168-201`

```
identificador com "@"? → e-mail normalizado : só dígitos do telefone   auth_service.py:169-171
busca o cliente                                                        auth_service.py:172
senha confere?  não → 401 "Credenciais invalidas"                      auth_service.py:174-175
conta ativa?    não → 403                                              auth_service.py:176-177
e-mail verificado? não → 200 com requires_email_verification=true      auth_service.py:178-183
                        (não é erro; é um estado do fluxo)
emite JWT HS256, purpose="customer_access", type="customer"            auth_service.py:185-190
```

Validade: `CUSTOMER_ACCESS_TOKEN_MINUTES`, padrão 10080 min = 7 dias (`src/core/config.py:27`).
Rate limit `10/minute` (`src/api/rate_limit.py:33`). A senha é bcrypt
(`src/utils/security.py:16`), com fallback de verificação para hashes `pbkdf2_sha256` antigos
(`src/utils/security.py:49-56`).

`POST /auth/forgot-password` tem um piso de latência de 0,6s (`auth_service.py:58, 61-65`)
e **sempre** devolve a mesma mensagem — falha é só logada (`auth_service.py:203-214`) — para
que nem a resposta nem o tempo dela digam se o e-mail existe.

### 4.2 Login do lojista

`POST /admin/auth/login` → `src/api/endpoints/admin_auth.py:19-26` → `src/services/admin_auth_service.py:52-83`

```
busca admin_users por e-mail normalizado                       admin_auth_service.py:54
verify_password roda MESMO sem usuário encontrado              admin_auth_service.py:57-58
   └─ senão o tempo do bcrypt denunciaria quais e-mails existem
não achou ou senha errada → 401 (mensagem idêntica nos dois)   admin_auth_service.py:60-65
inativo → 403                                                  admin_auth_service.py:66-71
emite JWT com restaurant_id e role no payload                  admin_auth_service.py:85-101
```

Validade: 720 min = 12h, um turno (`src/core/config.py:34`). Rate limit `10/minute;60/hour`
(`src/api/rate_limit.py:36`). Segredo: `ADMIN_AUTH_SECRET`, ou o do cliente se não definido
(`src/utils/security.py:134-143`).

O `restaurant_id` dentro do token é **informativo, para o painel**. A autorização não usa:
`get_admin_from_token` recarrega o usuário do banco a cada requisição
(`admin_auth_service.py:129`), senão um lojista desativado ou movido de restaurante
continuaria valendo até o token expirar. O comentário está em `admin_auth_service.py:93-96`.

### 4.3 Estimativa de entrega

`POST /restaurants/{slug}/delivery/estimate` → `src/api/endpoints/delivery.py:17-39` →
`src/services/delivery_estimate_service.py:133-407`

```
restaurante ativo ......................................... :147
restaurant_settings.accepts_delivery == false → não atende  :149-151
filial (a informada, ou a is_main) ........................ :154 → :409-431
branch.accepts_delivery == false → não atende ............. :155-158
endereço (address_id do cliente, ou inline) ............... :168 → :433-454
tempo de preparo do dia/faixa da filial ................... :179 → :474-490
   └─ sem business hours para hoje → NÃO ATENDE ("prep_time_unavailable")  :190-196
coordenadas: usa lat/lng se vierem, senão geocodifica ..... :201-202 → :456-472
cache (Redis, ou memória) ................................. :211 → :57-111
   └─ hit → devolve sem chamar o Google
Google Routes: compute_route .............................. :248
taxa de entrega = base + km × valor/km, com piso e teto ... :249 → :493-512
   └─ filial sem base/por-km configurados → não atende ..... :253-280
distância > delivery_max_distance_km → fora da área ....... :281-309
eta_min = prep_min + tempo de viagem ...................... :311-312
eta_max = prep_max + tempo de viagem
grava no cache e devolve .................................. :349
```

Falhas do Google:
- **Endereço não localizado** (`ZERO_RESULTS`) → `route_not_found`, não atende
  (`:350-365`). É cacheado como negativo por 120s (`config.py:50`).
- **Google fora do ar / chave errada** → `route_unavailable`, `fallback=true`, **sem taxa**
  (`:366-398`). Não atende.

Dentro do fluxo de pedido, qualquer `serviceable=false` vira **400** com a mensagem do motivo
(`order_service.py:311-315`).

O log é verboso de propósito (`[Delivery estimate debug]`, `:139`, `:159`, `:169`, `:182`,
`:628-661`). Para depurar uma taxa errada, procure `event=delivery_fee_algorithm`: ele imprime
distância, `delivery_base_fee`, `delivery_fee_per_km`, piso, teto, raio máximo, taxa bruta e
taxa final, tudo numa linha.

### 4.4 Chat do Rapi

`POST /chat` → `src/api/chat.py:34-45` → `src/services/chat_service.py:51-124`

```
rate limit 20/minute;200/hour ....................... rate_limit.py:40
corpo: restaurant_id, session_id, message (≤ limites) chat.py:18-21
loga digest sha256 da mensagem, NÃO a mensagem ...... chat_service.py:63-72
restaurante ativo? não → 404 (antes de gastar token)  chat_service.py:77 → :126-131
histórico da sessão (memória do processo, 1h, 20 msg) chat_service.py:82 → :238-240
embedding da pergunta (cache 1h) .................... retrieval_service.py:31-40
busca vetorial pgvector, top 5, FILTRADA por
   restaurant_id e produto ativo .................... ai_repository.py:24-46
chama o LLM (gpt-5-mini, structured output) ......... chat_service.py:141-146
descarta ids que o LLM inventou ..................... chat_service.py:157-177
recarrega os produtos do banco pelo id .............. chat_service.py:179-200
monta a resposta .................................... chat_service.py:202-235
grava o turno na sessão ............................. chat_service.py:106
```

Dois pontos de defesa que valem conhecer:

- `_validate_selected_product_ids` (`chat_service.py:157-177`) só aceita ids que estavam no
  contexto recuperado. Se o LLM alucinar um produto, ele é simplesmente descartado.
- `_hydrate_products` (`:179-200`) busca de novo no banco filtrando por `restaurant_id`. O
  preço e o nome que chegam ao cliente vêm do banco, nunca do que o LLM escreveu.
- Se o LLM disse `response_type="products"` mas nenhum id sobreviveu, a resposta é rebaixada
  para `"text"` com um `warning` no log (`:210-219`).

Os embeddings são gerados fora do ciclo HTTP, por `scripts/reindex_ai.py`. Produto novo ou
renomeado não aparece no chat até reindexar.

---

## 5. Onde ficam as regras de negócio

| Regra | Arquivo:linha |
|---|---|
| **Taxa de entrega** = base + (km × valor/km), com piso e teto | `src/services/delivery_estimate_service.py:493-512` |
| Filial sem `delivery_base_fee`/`delivery_fee_per_km` → não atende | `src/services/delivery_estimate_service.py:496-497`, `:253-280` |
| **Raio máximo de entrega** | `src/services/delivery_estimate_service.py:515-517` |
| **Tempo de preparo** (por filial, dia da semana e faixa de horário) | `src/services/delivery_estimate_service.py:474-490` |
| Escolha da faixa de horário (a atual, ou a primeira do dia) | `src/services/delivery_estimate_service.py:520-539` |
| ETA = preparo + tempo de viagem | `src/services/delivery_estimate_service.py:310-312` |
| Fuso horário do preparo (America/Fortaleza) | `src/services/delivery_estimate_service.py:30`, `:475` |
| TTL do cache de estimativa (600s / 120s negativo) | `src/core/config.py:49-50` |
| **Valor mínimo do pedido** (compara com o **subtotal**) | `src/services/order_service.py:401-407` |
| **Taxa de serviço** (liga/desliga + valor, por restaurante) | `src/services/order_service.py:396-399` |
| Subtotal = Σ (preço do produto + opções) × quantidade | `src/services/order_service.py:380-394` |
| Total = subtotal + serviço + entrega − descontos | `src/services/order_service.py:117` |
| Arredondamento de dinheiro (2 casas, HALF_UP) | `src/utils/money.py:15-16` |
| **Elegibilidade de cupom** (ordem completa das checagens) | `src/services/coupon_service.py:66-140` |
| Cálculo do desconto (fixo / percentual / frete grátis) | `src/services/coupon_service.py:47-64` |
| Teto do desconto percentual (`max_discount_amount`) | `src/services/coupon_service.py:57-58` |
| Cupom exige cliente autenticado | `src/services/coupon_service.py:250-251` |
| Trava do cupom (`SELECT FOR UPDATE`) | `src/repositories/coupon_repository.py:47-58` |
| Cooldown em dias entre usos | `src/services/coupon_service.py:115-127` |
| "Só no primeiro pedido" | `src/services/coupon_service.py:128-129`, `coupon_repository.py:124-130` |
| Estorno do cupom ao cancelar/rejeitar | `src/services/admin_order_service.py:112-113` → `coupon_service.py:282-285` |
| Opções obrigatórias, mínimo e máximo por grupo | `src/services/order_service.py:353-372` |
| Endereço obrigatório para entrega | `src/services/order_service.py:262-269` |
| Telefone gravado e comparado só em dígitos | `src/schemas/order_schema.py:20-31`, `order_service.py:123`, `:232`, `:244-248` |
| Tipos de pedido, status e papéis de lojista válidos | `src/core/constants.py:1-16` |
| Limites do pedido (100 itens, 30 opções, qtd 99) | `src/schemas/order_schema.py:11-13` |
| TTL da idempotência (24h) | `src/core/config.py:60` |
| Idempotência obrigatória ou não | `src/core/config.py:64`, `src/services/idempotency_service.py:193-200` |
| Validade dos tokens (cliente 7d / lojista 12h) | `src/core/config.py:27`, `:34` |
| Rate limits por rota | `src/api/rate_limit.py:33-41` |
| Limite de corpo da requisição | `src/core/config.py:56` |
| Escopo do lojista por restaurante | `src/api/dependencies/admin_auth.py:34-44` |
| Saldo de cashback disponível | `src/services/cashback_service.py:25-27` |
| TTL / tamanho da sessão do chat (1h, 20 mensagens) | `src/services/chat_service.py:23-24` |

---

## 6. Banco de dados

### 6.1 As tabelas principais

```
                          restaurants  ◄──────── raiz de tudo
                               │
        ┌──────────────┬───────┼────────────┬──────────────┬───────────────┐
        │              │       │            │              │               │
restaurant_settings  branches  │       categories    restaurant_       admin_users
     (1 : 1)            │      │            │          coupons
                        │      │            │             │
              ┌─────────┴──┐   │       products          coupon_
              │            │   │            │           redemptions
      branch_business  branch_ │      product_option_        │
          _hours       payment_│          groups             │
                       methods │            │                │
                               │      product_options        │
                               │                             │
                            orders ◄────────────────────────┘
                               │
              ┌────────────────┼──────────────────┐
              │                │                  │
        order_items    order_status_history   (idempotency_keys.order_id)
              │
       order_item_options


   customers  ◄──── GLOBAL, fora da árvore do restaurante
       │
       ├── customer_addresses
       ├── email_verification_codes / password_reset_codes
       ├── cashback_transactions
       ├── coupon_redemptions
       └── orders (customer_id, opcional)


   coupon_templates  ◄──── GLOBAL (catálogo de arte/tipo). restaurant_coupons aponta pra ele.
   ai_product_embeddings / ai_feedback  ◄──── por restaurante
```

### 6.2 Como o isolamento entre restaurantes funciona

Não há RLS nem schema por tenant. O isolamento é **filtro explícito por `restaurant_id` em
toda consulta**, e ele mora nos repositórios. As tabelas que carregam `restaurant_id`
próprio:

`branches`, `categories`, `products`, `restaurant_settings`, `restaurant_banners`,
`restaurant_coupons`, `orders`, `admin_users`, `ai_product_embeddings`, `ai_feedback`,
`cashback_transactions`.

Onde o filtro é aplicado:

| Consulta | Filtro | Onde |
|---|---|---|
| Produtos de um pedido | `Product.restaurant_id == restaurant_id` | `product_repository.py:60` |
| Filial | `Branch.restaurant_id == restaurant_id` | `branch_repository.py:18` |
| Cupom por id ou código | `RestaurantCoupon.restaurant_id == restaurant_id` | `coupon_repository.py:26`, `:40` |
| Pedido no painel do lojista | `Order.restaurant_id == restaurant_id` | `order_repository.py:81` |
| Lista de pedidos | `Order.restaurant_id == restaurant_id` | `order_repository.py:66` |
| Consulta pública de pedido | `restaurant_id` + `order_number` + telefone (normalizado) | `order_repository.py:52-55` |
| Busca vetorial do chat | `ape.restaurant_id` **e** `p.restaurant_id` | `ai_repository.py:39-40` |
| Cardápio, categorias, banners, settings | `restaurant_id` | `menu_repository.py:20-82` |

Duas defesas complementares:

1. **O `restaurant_id` do lojista vem sempre do token**, nunca da URL —
   `src/api/endpoints/admin_orders.py:32`, `:45`, `:65`. Quando a URL também traz restaurante,
   os dois são confrontados: `admin_order_service.py:43-44` (por slug) e
   `src/api/dependencies/admin_auth.py:34-44` (por id).
2. **Divergência responde 404, não 403** (`dependencies/admin_auth.py:38-44`,
   `admin_order_service.py:69-72`).
   Um 403 confirmaria que aquele `restaurant_id` existe. Para o lojista, o que não é dele
   simplesmente não existe.

`OrderRepository.get_order_detail` exige `restaurant_id` como parâmetro obrigatório de
propósito — o comentário em `order_repository.py:74-77` registra que enquanto o filtro era só
por `Order.id`, qualquer lojista com um UUID de pedido em mãos lia o pedido de outro
restaurante, com nome, telefone e endereço do cliente dentro.

### 6.3 Por que `customers` é global e `orders` pertence a um restaurante

**`customers` é global** porque a conta é do cliente, não do restaurante. `email`, `phone` e
`cpf` têm `UNIQUE` de tabela inteira (`src/models/customer_model.py:18-20`) — não existe
`customers.restaurant_id`. Consequências práticas:

- Uma conta serve para pedir em qualquer restaurante da plataforma. É o modelo de marketplace
  (iFood), não o de app-por-restaurante.
- Endereços salvos (`customer_addresses`) e saldo de cashback são do cliente e atravessam
  restaurantes.
- O login (`auth_service.py:172`) busca por e-mail/telefone sem nenhum escopo de restaurante.
- **O mesmo CPF não pode se cadastrar duas vezes**, nem para restaurantes diferentes. Se um
  dia a plataforma quiser contas separadas por restaurante, esses três `UNIQUE` são o que
  precisa mudar primeiro.

**`orders` pertence a um restaurante** porque um pedido é uma transação com *aquele*
estabelecimento: ele tem a filial que vai preparar, os produtos daquele cardápio, a taxa
daquela filial e o histórico que aquele lojista opera. `orders.restaurant_id` é
`NOT NULL` (`src/models/order_model.py:18`); `orders.customer_id` é **nullable**
(`:20`) porque pedido de convidado é suportado.

Daí a assimetria que aparece no código:

- Quando há login, o pedido guarda `customer_id` e `customer_address_id`
  (`order_service.py:129-130`).
- Quando não há, guarda **só os snapshots** de nome e telefone
  (`customer_name_snapshot`, `customer_phone_snapshot`, `order_model.py:22-23`, ambos
  `NOT NULL`). O convidado é rastreável pelo telefone e por nada mais.
- Os snapshots existem **mesmo com login**, e são preenchidos sempre
  (`order_service.py:118-125`): o pedido tem que continuar legível se o cliente mudar o nome,
  trocar o telefone ou apagar a conta. `customer_address_id` inclusive tem
  `ON DELETE SET NULL` (`order_model.py:21`), enquanto o endereço textual fica gravado em
  colunas próprias do pedido (`:36-43`).

Uma consequência que confunde: **`orders.order_number` é uma sequence global e `UNIQUE` na
tabela inteira** (`order_model.py:17`), não uma numeração por restaurante. O primeiro pedido
de um restaurante novo pode ser o número 5471. Se o lojista pedir numeração própria, isso é
mudança de schema.

---

## 7. Autenticação

Dois tipos de token, ambos JWT HS256, ambos no header `Authorization: Bearer <token>`.
Não há refresh token: expirou, loga de novo.

### 7.1 Token de cliente

| | |
|---|---|
| Emitido em | `POST /auth/login` → `src/services/auth_service.py:185-190` |
| Segredo | `CUSTOMER_AUTH_SECRET` (`src/utils/security.py:127-131`) |
| Claims | `sub` = customer_id, `purpose` = `customer_access`, `type` = `customer` |
| Validade | 7 dias (`src/core/config.py:27`) |
| Verificado em | `src/api/dependencies/customer_auth.py:14-35` |

**Obrigatório** (`get_current_customer`, `customer_auth.py:14-21`): todas as rotas
`/customers/me/*` (perfil, endereços, histórico, cashback) e
`POST /restaurants/{slug}/coupons/preview`. Sem token → 401; conta inativa → 403
(`auth_service.py:288-299`).

**Opcional** (`get_optional_current_customer`, `customer_auth.py:23-35`): criação de pedido,
estimativa de entrega e listagem de cupons disponíveis. Token ruim aqui **não** dá erro —
devolve `None` e a rota segue como visitante (`:31-32`). É por isso que um token expirado no
checkout produz o sintoma "meu cupom sumiu" e não "sessão expirada": sem cliente, o cupom
recusa com 401 lá no fundo (`coupon_service.py:250-251`) e a lista de cupons omite os que
exigem login (`coupon_service.py:102-109`).

O que o token de cliente **não** permite: nada em `/admin/*`. O `purpose` é conferido na
decodificação (`src/utils/security.py:118-119`) e o `/admin` exige `purpose="admin_access"`.

### 7.2 Token de lojista

| | |
|---|---|
| Emitido em | `POST /admin/auth/login` → `src/services/admin_auth_service.py:85-101` |
| Segredo | `ADMIN_AUTH_SECRET`, com fallback para o do cliente (`src/utils/security.py:134-143`) |
| Claims | `sub` = admin_user_id, `purpose` = `admin_access`, `type` = `admin`, + `restaurant_id` e `role` (informativos) |
| Validade | 12 horas (`src/core/config.py:34`) |
| Verificado em | `src/api/dependencies/admin_auth.py:23-31` → `admin_auth_service.py:103-138` |

**Permite**, sempre restrito ao `restaurant_id` do usuário no banco:

- `GET /admin/auth/me`
- `GET /admin/restaurants/{slug}/orders` — lista de pedidos
- `GET /admin/orders/{id}` — detalhe do pedido
- `PATCH /admin/orders/{id}/status` — mudar status
- `GET|POST|PATCH /admin/restaurants/{id}/coupons` — campanhas de cupom

A verificação, em ordem (`admin_auth_service.py:103-138`): assinatura e expiração (`:105-115`),
`purpose` = `admin_access` (dentro de `decode_signed_token`, `security.py:118-119`), `type` =
`admin` (`:117-120`), `sub` é UUID válido (`:122-127`), **usuário recarregado do banco**
(`:129`), usuário ainda ativo (`:134-137`).

Recarregar do banco a cada requisição é o ponto importante: o `restaurant_id` que autoriza é o
da linha em `admin_users`, não o do token. Desativar um lojista ou movê-lo de restaurante tem
efeito imediato.

O escopo por restaurante é aplicado em dois lugares: `ensure_restaurant_scope`
(`dependencies/admin_auth.py:34-44`) para rotas com `restaurant_id` na URL, e a comparação em
`admin_order_service.py:43-44` para rotas com slug.

### 7.3 Como os dois convivem com o mesmo segredo

Se `ADMIN_AUTH_SECRET` estiver vazio, os dois tipos são assinados com a mesma chave
(`security.py:143`). Um token de cliente **ainda assim não vira token de admin**, porque o
`purpose` é diferente e é conferido na decodificação. Funciona, mas o startup emite warning
(`src/core/startup_checks.py:52-58`) e o recomendado é ter segredo próprio: aí vazar um não
alcança o outro.

---

## 8. Como operar

### 8.1 Subir local (Windows)

```powershell
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env    # e preencha os segredos
uvicorn main:app --reload
```

Docs interativas: `http://localhost:8000/docs` — **só se `APP_ENV` não for `production`**
(`main.py:26-36`, `src/core/config.py:75-79`). Em produção `/docs`, `/redoc` e
`/openapi.json` retornam 404 de propósito.

Sanidade: `curl http://localhost:8000/health`.

### 8.2 Subir em produção

```bash
docker compose up -d --build
docker logs -f pedeaqui-api
```

O compose não expõe porta: o Traefik roteia pela rede externa `n8n_default`
(`docker-compose.yml:8-13` labels, `:17-19` a rede). O container roda como usuário sem
privilégios e com `/app` somente-leitura (`Dockerfile:14-19`).

### 8.3 Migrações

Fonte da verdade: `alembic/versions/`. A pasta `migrations/` é **arquivo histórico congelado**
— não rode nada de lá (`migrations/README.md`).

Banco que **já tem o schema** (produção e seu dev atual) — só uma vez, e não executa DDL:

```powershell
alembic stamp 20260726_0001
alembic current    # deve mostrar 20260726_0001
```

Dia a dia:

```powershell
alembic upgrade head
alembic revision -m "descricao"
alembic revision --autogenerate -m "descricao"
alembic downgrade -1
alembic history --verbose
```

**Sempre leia o arquivo gerado pelo `--autogenerate` antes de aplicar.** O banco de produção
tem objetos que o ORM não mapeia — sequences, defaults, índices criados à mão nos 13 `.sql`
antigos — e o autogenerate vai propor derrubá-los. Apague essas linhas
(`alembic/env.py:6-11`).

Em produção a migração **não** roda no start do container, de propósito: um `upgrade head`
automático no boot derruba a API junto com uma migração ruim, e com mais de um container eles
correm um contra o outro. É passo explícito, depois do deploy:

```bash
docker exec pedeaqui-api alembic current       # onde estamos
docker compose up -d --build                   # sobe o código novo
docker exec pedeaqui-api alembic upgrade head  # migra
docker exec pedeaqui-api alembic current       # confirma
```

Faça backup antes. É Supabase/Postgres, DDL ruim custa caro.

### 8.4 Criar um lojista

Não há tela de cadastro, e o primeiro admin não pode nascer pela API (não há ninguém
autenticado para autorizar). Use o script:

```bash
docker exec -it pedeaqui-api python scripts/create_admin_user.py \
  --restaurant-slug junior-da-picanha \
  --name "Junior" \
  --email junior@exemplo.com \
  --role owner
```

O `-it` é necessário: a senha é pedida em prompt oculto e **não** é argumento de linha de
comando de propósito — iria para o histórico do shell e para o `ps`
(`scripts/create_admin_user.py:8-10`, `:49-59`). Mínimo de 10 caracteres (`:46`). Papéis:
`owner`, `manager`, `attendant` (`src/core/constants.py:1`).

Local, com o venv ativo: `py scripts/create_admin_user.py --restaurant-slug ... --role owner`.

### 8.5 Rodar os testes

```powershell
py -m pytest tests -q          # 117 testes, ~7s
py -m pytest tests -v          # com nome de cada teste
py -m pytest tests/test_idempotency.py -v
```

Não precisam de banco: usam fakes em memória. Precisam de um `.env` válido, porque
`src.core.config` é importado na cadeia e `pydantic-settings` levanta `ValidationError` se
faltar variável obrigatória.

O que os testes **não** cobrem, e você tem que verificar à mão: o `ON CONFLICT DO NOTHING`
real, o bloqueio entre transações concorrentes, o round-trip do JSONB e a limpeza por TTL
(`tests/test_idempotency.py:6-11`).

### 8.6 Ver logs

Todo log da aplicação vai para o logger `uvicorn.error`.

```bash
docker logs -f pedeaqui-api
docker logs pedeaqui-api --since 30m | grep "\[Delivery estimate debug\]"
docker logs pedeaqui-api --since 1h  | grep "\[Idempotency\]"
docker logs pedeaqui-api             | grep "\[AdminAuth\]"
docker logs pedeaqui-api             | grep "\[Startup\]"
```

Prefixos e o que investigam:

| Prefixo | Investiga | Onde |
|---|---|---|
| `[Startup]` | config faltando ou arriscada no boot | `src/core/startup_checks.py:76-86` |
| `[Delivery estimate debug]` | taxa/prazo errados, passo a passo | `delivery_estimate_service.py:139`, `:612-661` |
| `[Delivery Google]` | latência e status HTTP do Google | `google_maps_routes_client.py:69-73`, `:118-122` |
| `[Idempotency]` | reenvio devolvendo resposta gravada | `idempotency_service.py:138-142`, `:199` |
| `[AdminAuth]` | quem logou no painel e de qual restaurante | `admin_auth_service.py:73-78` |
| `[AI /chat]`, `[AI /chat perf]`, `[AI /chat cache]` | chat: tempo por etapa e cache | `chat_service.py:59-124` |
| `[Contract validation]` | 422 em `/delivery/estimate` e import de endereço | `validation_errors.py:31-47` |
| `[Body limit]` | requisição rejeitada com 413 | `body_size.py:76-80` |
| `[Rate limit]` (429) | resposta em `rate_limit.py:64-75` | — |

Nunca há dado pessoal nesses logs por escolha: a mensagem do chat vira digest sha256
(`chat_service.py:63-72`) e a chave de idempotência também (`idempotency_service.py:211-212`).
Não passe a logar o conteúdo cru para depurar.

### 8.7 Limpeza e manutenção

```bash
docker exec pedeaqui-api python scripts/cleanup_idempotency_keys.py --dry-run
docker exec pedeaqui-api python scripts/cleanup_idempotency_keys.py
docker exec pedeaqui-api python scripts/reindex_ai.py
```

O primeiro apaga chaves vencidas — seguro, passado o TTL a chave já não protege nada
(`scripts/cleanup_idempotency_keys.py:1-6`). Bota no cron. O segundo regenera os embeddings
dos produtos; rode depois de mexer no cardápio, senão o chat continua recomendando o cardápio
velho.

### 8.8 Quando o container não sobe

Ordem de diagnóstico:

**1. Leia os logs. A causa quase sempre está escrita.**

```bash
docker logs pedeaqui-api --tail 50
```

**2. `StartupConfigurationError` na primeira tela.**
É a validação de boot (`src/core/startup_checks.py:75-86`) recusando de propósito. Hoje só
uma coisa derruba: `GOOGLE_MAPS_ROUTES_API_KEY` vazia com
`DELIVERY_ESTIMATE_PROVIDER=google_routes` (`:23-30`). Preencha a chave (com Routes API **e**
Geocoding API habilitadas no projeto GCP) ou mude o provider. Esse erro existe porque antes a
API subia normalmente e recusava **todo** pedido de entrega em silêncio.

**3. `ValidationError` do pydantic-settings, antes de qualquer log da aplicação.**
Falta variável obrigatória no `.env`. As sem default em `src/core/config.py:15-41`:
`DATABASE_URL`, `SUPABASE_URL`, `CUSTOMER_AUTH_SECRET`, `EMAIL_CODE_SECRET`,
`PASSWORD_RESET_SECRET`, `OPENAI_API_KEY`. Compare com `.env.example`, que está anotado por
categoria.

**4. Erro de conexão com o banco.**
`DATABASE_URL` precisa do driver: `postgresql+psycopg://...`. Sem o `+psycopg` o SQLAlchemy
tenta `psycopg2` e o comportamento muda. Confira também se o IP do servidor está liberado no
Supabase.

**5. Sobe mas o Traefik dá 404 / gateway error.**
Não é o app. Confira se o container está na rede externa `n8n_default`
(`docker-compose.yml:17-21`) e se o DNS de `api.pederapidex.com` aponta para o servidor.
Teste por dentro: `docker exec pedeaqui-api curl -s localhost:8000/health`.

**6. Sobe e responde, mas `/docs` dá 404.**
Comportamento correto com `APP_ENV=production`. Para ligar sem sair de produção, defina
`ENABLE_API_DOCS=true` (`src/core/config.py:75-79`).

**7. Warnings no boot que NÃO derrubam** (`startup_checks.py:35-72`) — vale ler, cada um tem
consequência real: `REDIS_URL` vazia (cache e rate limit só em memória),
`ADMIN_AUTH_SECRET` vazia (segredo compartilhado com o cliente), `INTERNAL_API_KEY` ainda
presente (não é mais usada por rota nenhuma), `RATE_LIMIT_CLIENT_IP_HEADER` vazio (todos os
clientes no mesmo balde de rate limit).

---

## 9. Armadilhas conhecidas

### 9.1 Telefone: normalizado em todo lugar (corrigido) — cuidado ao mexer

**Era um bug, foi corrigido.** Fica registrado porque explica por que a normalização aparece
em quatro lugares e por que nenhum deles pode sair.

O que acontecia: `customer_phone_snapshot` era gravado cru, mas a consulta pública compara por
igualdade exata. Quem digitava `(85) 99999-9999` no checkout não achava o próprio pedido
depois procurando por `85999999999`. E o escopo da idempotência já normalizava o mesmo campo,
então os dois lados discordavam.

A regra hoje é uma só — **tudo que escreve ou compara telefone passa por `normalize_digits`**
(`src/utils/normalization.py:12-13`), a mesma função do cadastro:

| Ponto | Onde |
|---|---|
| Entrada do pedido (contrato HTTP) | `src/schemas/order_schema.py:20-31` |
| Escrita do snapshot | `src/services/order_service.py:119-125` |
| Escopo da idempotência | `src/services/order_service.py:232` |
| Consulta pública do pedido | `src/services/order_service.py:241-248` |

A normalização na escrita (`order_service.py:123`) é **redundante com o schema de propósito**:
o snapshot também pode vir de `current_customer.phone`, que o schema do pedido não toca. Não
remova achando que é duplicação.

`OrderRepository.get_order_by_number_and_phone` espera receber o telefone já normalizado — o
aviso está em `order_repository.py:44-47`. Passar valor cru ali ressuscita o bug.

O schema agora também **rejeita telefone com menos de 8 dígitos** (`order_schema.py:29-31`).
Antes, `min_length=8` contava a pontuação: `"--------"` passava e virava um snapshot que
consulta nenhuma encontraria.

**Resíduo conhecido**: a normalização é dígitos-apenas, então `+55 85 99999-9999` vira
`5585999999999` e **não** casa com `85999999999`. O código de país não é removido. É a mesma
propriedade do cadastro (`auth_service.py:75`), e só vira problema se o frontend passar a
mandar DDI. Se um dia mandar, o lugar de resolver é `normalize_digits` ou uma função nova ao
lado dela — nunca em um dos quatro pontos isolados.

Testes que travam isso: `tests/test_phone_normalization.py`.

### 9.2 `is_open` e `accepts_pickup` não bloqueiam nada

`restaurant_settings.is_open` e `accepts_pickup` existem (`restaurant_setting_model.py:24-30`)
e são devolvidos no cardápio (`menu_service.py:64-66`), mas **`create_order` nunca os
consulta**. Um `grep` confirma: fora do `menu_service`, ninguém lê esses campos.

Efeito: restaurante "fechado" continua aceitando pedido pela API. Quem bloqueia hoje é o
frontend. Só `accepts_delivery` é aplicado, e apenas dentro da estimativa de entrega
(`delivery_estimate_service.py:149-158`) — então "não aceita entrega" funciona, "está fechado"
e "não aceita retirada" não.

### 9.3 `payment_method` não é validado

`PAYMENT_METHODS` está definido em `src/core/constants.py:16` e **não é usado em lugar nenhum**.
`CreateOrderRequest.payment_method` é uma string livre de até 50 caracteres
(`order_schema.py:69`) e vai direto para `orders.payment_method`. Qualquer texto entra.

### 9.4 Sem `branch_business_hours` para hoje, nenhuma entrega passa

`_resolve_prep_time` (`delivery_estimate_service.py:474-490`) devolve `None, None` quando não
há faixa cadastrada para o dia da semana atual, e isso vira `prep_time_unavailable` /
não atende (`:190-196`), que no fluxo de pedido é **400**.

Sintoma clássico: "de repente parou de aceitar entrega e ninguém mexeu em nada" — mexeram sim,
apagaram ou nunca cadastraram o horário daquele dia da semana. Confira
`branch_business_hours` para `weekday` de hoje (0 = segunda, `datetime.weekday()`), com
`prep_time_min` e `prep_time_max` preenchidos.

Detalhe relacionado: se a hora atual não cair em nenhuma faixa do dia, o código **não** falha —
usa a primeira faixa cadastrada (`:537-539`). Fora do horário, o prazo mostrado é o da primeira
faixa, não um erro.

### 9.5 Google Maps fora do ar = zero pedidos de entrega

`GoogleMapsUnavailableError` produz `serviceable=false` com `fallback=true` e **sem taxa**
(`delivery_estimate_service.py:366-390`), e `_estimate_delivery` transforma isso em 400
(`order_service.py:311-315`). Não existe taxa de fallback.

Foi decisão consciente: cobrar uma taxa chutada é pior que recusar. Por isso a chave do Maps é
validada no boot e derruba a API se faltar (`startup_checks.py:23-30`) — antes disso, a API
subia e recusava todo pedido de entrega sem erro visível.

Se o Google cair, a saída operacional é o retirada (`pickup`), que não passa pela estimativa
(`order_service.py:288-289`).

### 9.6 O cupom só é validado depois da chamada paga ao Maps

Ordem em `create_order`: estimativa de entrega na linha 81, cupom na 106. Um cliente com cupom
inválido tentando várias vezes queima chamadas do Google a cada tentativa.

Não é bug — a taxa de entrega é **insumo** do desconto: `free_delivery` desconta exatamente o
valor da entrega (`coupon_service.py:60-61`), então o cupom não pode ser avaliado antes de a
taxa existir. O que reduz o desperdício é o cache de estimativa
(`delivery_estimate_service.py:211`) e o front chamar `POST /coupons/preview`
(`coupons.py:49-56`) antes de fechar o pedido.

### 9.7 A reserva de idempotência é liberada por rollback implícito

Entre `begin()` (`order_service.py:71`) e o `try` (`:105`) há seis validações que podem
levantar `HTTPException`, e **não** há `rollback()` nesse trecho. Está correto — a sessão é
fechada sem commit em `src/db/session.py:24-26` e o Postgres descarta a transação — mas é
implícito.

O que isso significa para você: **se algum dia alguém colocar um `db.commit()` entre as linhas
71 e 105**, a reserva passa a ser persistida sem o pedido, e a chave fica queimada por 24h
retornando 409 para um pedido que nunca existiu. Não commite nesse trecho.

### 9.8 Sem `Idempotency-Key`, o pedido passa sem proteção

`normalize_idempotency_key` (`idempotency_service.py:179-208`) aceita a requisição sem o header
e só loga um warning. O raciocínio está escrito em `:184-192`: rejeitar com 400 transforma um
risco de "pedido duplicado, que o lojista cancela em dois cliques" em "pedido perdido e cliente
sem jantar" — o modo de falha caro. O flag `IDEMPOTENCY_REQUIRED` (`config.py:64`) existe para
o dia em que der para afirmar que não há app antigo em campo.

Se aparecerem pedidos duplicados, o primeiro `grep` é
`"requisicao sem Idempotency-Key"` no log.

### 9.9 Nada em memória sobrevive a mais de um worker

Três estruturas são dicionários no processo:

| O quê | Onde | Efeito com N workers |
|---|---|---|
| Histórico de sessão do chat | `chat_service.py:37` | o Rapi "esquece" a conversa quando a requisição cai em outro worker |
| Cache de estimativa de entrega | `delivery_estimate_service.py:58` | chamadas repetidas (e pagas) ao Google |
| Contadores de rate limit | `rate_limit.py:57` (`memory://`) | limite efetivo vira N × o configurado |

Hoje o deploy é um container/um worker, então funciona. Definir `REDIS_URL` resolve os dois
últimos sem tocar em código (o limiter e o cache já leem a variável). O histórico do chat
**não** tem caminho de Redis — continuaria em memória.

O startup avisa sobre isso (`startup_checks.py:38-50`).

### 9.10 O escopo por filial do lojista existe no banco mas não é aplicado

`admin_users.branch_id` é gravado (`admin_user_model.py:30-32`) e o script até aceita
`--branch-id` (`create_admin_user.py:68-72`), mas **nenhuma rota filtra por ele** — todo
lojista enxerga os pedidos de todas as filiais do restaurante. Está registrado no próprio
modelo (`admin_user_model.py:16-19`). Se você criar um usuário achando que ele fica preso a
uma filial, não fica.

### 9.11 Não há máquina de estados de pedido

`update_order_status` (`admin_order_service.py:75-137`) valida que o status **existe** em
`ORDER_STATUSES` (`:83-84`) e nada mais. `completed → pending` é aceito, `cancelled →
out_for_delivery` também. O único efeito colateral condicionado ao destino é o estorno do
cupom, em `cancelled` e `rejected` (`:112-113`).

Como o estorno só olha o destino e `reverse_redemption` é idempotente
(`coupon_repository.py:157-164`), cancelar e "descancelar" **não** devolve o cupom ao cliente —
o resgate fica `reversed` para sempre.

### 9.12 Resgate de cashback está pela metade

`cashback_redeemed` é literalmente `ZERO` no cálculo do pedido (`order_service.py:115`), e o
campo `cashback_redeemed_amount` sempre grava zero (`:136`). A tabela existe
(`cashback_transaction_model.py`), o saldo é lido e listado (`cashback_service.py:25-54`), e
`CustomerRepository.lock_customer` (`customer_repository.py:23-25`) já está escrito para o
resgate — mas ninguém o chama. O crédito de cashback ao completar pedido também não existe.

Ou seja: o saldo que o cliente vê só muda se alguém escrever na tabela por fora.

### 9.13 O cardápio filtra por `is_available`, o carrinho também — e isso gera 400 mudo

`product_repository.py:55-66` exige `is_active` **e** `is_available`. Se o lojista marcar um
produto como indisponível enquanto o cliente está com ele no carrinho, o pedido falha com
`"Produto inválido ou indisponível"` (`order_service.py:322`) sem dizer qual produto. É
proposital (não virar oráculo de UUIDs), mas na hora de depurar significa comparar os
`product_id` do corpo com o que o banco tem.

### 9.14 `alembic --autogenerate` quer apagar metade do banco

O schema de produção nasceu antes do Alembic e tem objetos que o ORM não mapeia: sequences
(incluindo a de `order_number`), defaults e índices criados à mão nos 13 `.sql` de
`migrations/`. O `--autogenerate` compara ORM × banco e propõe `DROP` em tudo que não achar
no ORM. Está avisado em `alembic/env.py:6-11` e no `README.md:102-105`.

**Leia sempre o arquivo gerado antes de aplicar, e apague as linhas de drop.**

E, em banco que já tem o schema, a baseline é `alembic stamp 20260726_0001`, nunca
`upgrade` — aplicar a baseline como migração falha.
