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
tests/                     245 testes, sem banco real (usam fakes)
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

Entrada: `src/services/order_service.py:66`.

```
 73  restaurante ativo pelo slug ................ 404 se não achar
 74  order_type ∈ {delivery, pickup} ............ 400
 76  filial ativa E do restaurante .............. 400
 80  resolve o endereço ......................... 401 / 404
 81  exige cliente (logado ou no corpo) ......... 401
 86  carrega restaurant_settings
 87  loja aberta / aceita este tipo de pedido ... 400   ← Fase 2
 88  filial dentro do horário de funcionamento .. 400   ← Fase 2
 89  forma de pagamento válida e habilitada ..... 400   ← Fase 2
─────────────────────────────────────────────────────────────
 95  ►►► RESERVA A CHAVE DE IDEMPOTÊNCIA ◄◄◄
─────────────────────────────────────────────────────────────
103  endereço completo se for delivery .......... 400
104  estimativa de entrega ...................... 400 se fora de área
       └─ reaproveita delivery_estimate_token quando válido,
          e só aí chama o Google (Fase 2)
111  produtos válidos, ativos, DESTE restaurante  400
113  valida opções selecionadas ................. 400
117  calcula subtotal
118  valor mínimo do pedido ..................... 400
119  taxa de serviço
120  taxa de entrega
─────────────────────────────────────────────────────────────
129  ►►► INÍCIO DO try/except ◄◄◄
131  trava e valida o cupom (SELECT FOR UPDATE) . 400 / 401 / 404
141  total = subtotal + serviço + entrega − descontos
142  comissão da plataforma (congelada no pedido)      ← Fase 2
153  normaliza o telefone do snapshot
157  INSERT orders (com tracking_token, payment_status e comissão)
     INSERT coupon_redemptions
     INSERT order_items
     INSERT order_item_options
     INSERT order_status_history ("pending")
     UPDATE idempotency_keys → completed + resposta
     ►►► COMMIT ◄◄◄
     except: ROLLBACK e propaga
```

As três validações da Fase 2 ficam **antes** da reserva de idempotência: são
consultas baratas, e recusar cedo evita queimar chave e chamada paga do Google
num pedido que a loja não poderia receber de jeito nenhum.

Detalhe por etapa:

| # | O que faz | Onde | Erro |
|---|---|---|---|
| 1 | Restaurante ativo pelo slug | `order_service.py:73` → `restaurant_service.py:42-49` | 404 |
| 2 | `order_type` válido | `order_service.py:74` → `:311`; lista em `core/constants.py` | 400 |
| 3 | Filial ativa **e** do restaurante | `order_service.py:76` → `branch_repository.py:15-21` | 400 |
| 4 | Resolve endereço | `order_service.py:80` → `:399` | 401/404 |
| 5 | Cliente identificado | `order_service.py:81` → `:385` | 401 |
| 6 | Settings do restaurante | `order_service.py:86` → `menu_repository.py:20-22` | — |
| 7 | **Loja aberta / aceita o tipo** | `order_service.py:87` → `:315` | 400 |
| 8 | **Filial dentro do horário** | `order_service.py:88` → `branch_hours_service.py:55` | 400 |
| 9 | **Forma de pagamento** | `order_service.py:89` → `:344` | 400 |
| 10 | **Idempotência** | `order_service.py:95` | 409/422 |
| 11 | Endereço completo p/ entrega | `order_service.py:103` → `:390` | 400 |
| 12 | Estimativa de entrega (reaproveita token) | `order_service.py:104` → `:409`, `:463` | 400 |
| 13 | Produtos válidos | `order_service.py:111` → `:535` | 400 |
| 14 | Opções por item | `order_service.py:113` → `:543` | 400 |
| 15 | Subtotal | `order_service.py:117` → `:598` | — |
| 16 | Valor mínimo | `order_service.py:118` → `:662` | 400 |
| 17 | Taxa de serviço | `order_service.py:119` → `:657` | — |
| 18 | Cupom (travado) | `order_service.py:131` → `coupon_service.py:240-268` | 400/401/404 |
| 19 | **Comissão da plataforma** | `order_service.py:142` → `:614` | — |
| 20 | Gravação | `order_service.py:157-228` | — |
| 21 | Commit | `order_service.py:258` | — |

**Passo 4 (endereço) em detalhe** — `order_service.py:399`:
- `customer_address_id` sem login → **401**. Um convidado não pode apontar para endereço de conta.
- Com login + `customer_address_id` → busca em `customer_addresses` filtrando **também** por
  `customer_id` (`customer_repository.py:121-126`). Não é seu, é 404.
- Sem `customer_address_id` → usa o `address` inline do corpo.

**Passo 13 (produtos)** — `order_service.py:535` chama
`product_repository.py:55-66`, que filtra por `restaurant_id`, `is_active` e `is_available`.
Se o número de produtos encontrados não bater com o número de ids únicos pedidos, é **400**
genérico ("Produto inválido ou indisponível"). Não diz qual — de propósito, para não virar
oráculo de quais UUIDs existem.

**Passo 11 (opções)** — `order_service.py:325-378`. Valida, nessa ordem: grupo pertence ao
produto e está ativo (`:332-336`), opção pertence ao grupo e está ativa (`:337-345`), opção
não repetida (`:346-350`); depois, por grupo: obrigatório foi preenchido (`:358-362`), mínimo
(`:363-367`) e máximo (`:368-372`).

### 3.3 Onde entra a idempotência

`src/services/order_service.py:95`, e a posição é intencional.

Ela fica **antes de qualquer escrita e antes da chamada paga ao Google Maps**, mas **depois**
das validações baratas (restaurante, filial, endereço, loja aberta, horário da filial e
forma de pagamento). Assim um reenvio devolve a resposta
gravada sem gastar rota do Maps nem criar um segundo pedido, e um payload obviamente inválido
não queima chave.

A reserva é um `INSERT ... ON CONFLICT DO NOTHING` — `src/repositories/idempotency_repository.py:36-48`:

- Pegou a reserva (voltou um id) → segue e executa.
- Não pegou → lê a linha existente (`idempotency_service.py:104`) e decide:
  - fingerprint diferente → **422** (`:114-124`): mesma chave, corpo diferente, é reciclagem.
  - `in_progress` → **409** (`:126-135`).
  - `completed` com resposta → devolve a resposta gravada (`:137-143`), e o
    `order_service.py:101` a converte de volta em `CreateOrderResponse`.

O escopo da chave é `restaurant_id | rota | solicitante` — `idempotency_service.py:48-64`.
O solicitante é `customer:<id>` quando há login, senão `phone:<dígitos>`
(`order_service.py:266`). Sem o solicitante no escopo, um cliente adivinhando a chave de
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
| Preço unitário | `products.price` do banco + soma de `product_options.additional_price` | `order_service.py:671` |
| Subtotal | Σ (preço + opções) × quantidade | `order_service.py:598` |
| Taxa de entrega | base + km × valor/km, com piso e teto da filial | `delivery_estimate_service.py:607` |
| Taxa de serviço | `restaurant_settings.service_fee_amount`, se habilitada | `order_service.py:657` |
| Desconto do cupom | tipo e valor do cupom no banco | `coupon_service.py:47-64` |
| Total | `subtotal + serviço + entrega − descontos` | `order_service.py:141` |
| Comissão da plataforma | `(subtotal − cupom − cashback) × percentual do restaurante` | `order_service.py:614` |

Tudo em `Decimal`, arredondado com `ROUND_HALF_UP` para 2 casas em `src/utils/money.py:15-16`.
Nunca `float` no cálculo — `float` só aparece na serialização da resposta
(`money_to_float`, `src/utils/money.py:19-20`).

Os preços também são **congelados em snapshot** dentro do pedido: `product_name_snapshot`,
`unit_price_snapshot`, `option_name_snapshot`, `additional_price_snapshot`
(`order_service.py:671-702`). Mudar o preço do produto amanhã não altera pedido de ontem.

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

O bloco `try` vai de `order_service.py:129` a `:262`. Dentro dele, tudo em uma transação:

```
orders                 1 linha    order_service.py:157-205
coupon_redemptions     0 ou 1     order_service.py:207
order_items            N linhas   order_service.py:209-219
order_item_options     0..N       order_service.py:220-226
order_status_history   1 linha    order_service.py:227  (status "pending", changed_by "system")
idempotency_keys       1 UPDATE   order_service.py:254  (completed + response_body em JSONB)
                       ─────────
                       COMMIT     order_service.py:258
```

A resposta é montada em `:230` e gravada em `idempotency_keys.response_body`
**antes** do commit (`:254`), com o motivo explicado no comentário logo acima: assim a resposta
armazenada e o pedido entram no banco atomicamente. Se o commit falhar, não sobra chave
apontando para pedido inexistente.

O `order_number` é uma sequence do Postgres (`src/models/order_model.py:17`), preenchida no
`flush()` do `create_order` e já legível na resposta. O `tracking_token`, ao contrário, é
sorteado em Python antes do INSERT (`order_service.py:158`).

### 3.7 Se algo falhar no meio

**Dentro do `try` (linhas 129–259)**: `except Exception: self.db.rollback(); raise`
(`order_service.py:260`). Volta tudo — pedido, itens, opções, histórico, resgate de cupom
e a reserva da chave de idempotência. O lock `FOR UPDATE` do cupom é liberado pelo rollback.
O erro sobe: `HTTPException` vira a resposta HTTP correspondente; qualquer outra exceção vira
500.

**Entre o `begin()` da idempotência (95) e o `try` (129)** — endereço inválido, fora de área,
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

`POST /restaurants/{slug}/delivery/estimate` → `src/api/endpoints/delivery.py` →
`DeliveryEstimateService.estimate_and_store` (`src/services/delivery_estimate_service.py:187`),
que chama `estimate` (`:237`) e guarda o resultado.

```
restaurante ativo
restaurant_settings.accepts_delivery == false → não atende
filial (a informada, ou a is_main) ......................... :524
   └─ o `getattr(branch, "accepts_delivery", True)` foi REMOVIDO na Fase 2:
      a coluna nunca existiu em `branches`, então era código morto que
      sempre liberava
endereço (address_id do cliente, ou inline) ................ :548
faixa de funcionamento que contém o agora .................. :589 → branch_hours_service.py:35
   └─ nenhuma faixa contém o agora → NÃO ATENDE ("branch_closed")
   └─ faixa sem tempo de preparo   → NÃO ATENDE ("prep_time_unavailable")
coordenadas: usa lat/lng se vierem, senão geocodifica (PAGO)
cache (Redis, ou memória) .................................. :326
   └─ hit → devolve sem chamar o Google
Google Routes: compute_route (PAGO) ........................ :363
taxa de entrega = base + km × valor/km, com piso e teto .... :607
   └─ filial sem base/por-km configurados → não atende
distância > delivery_max_distance_km → fora da área ........ :629
eta_min = prep_min + tempo de viagem
eta_max = prep_max + tempo de viagem
grava no cache
─────────────────────────────────────────────────────────────
GRAVA em delivery_estimates e devolve `estimate_token` ...... :187   ← Fase 2
   └─ só quando serviceable=true. É esse token que evita a segunda
      rodada de geocode + rota na criação do pedido (ver 4.7 e 9.17).
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

### 4.5 Pagamento do pedido (Fase 2)

Um pedido nasce com dois estados: `status` (o ciclo operacional) e
`payment_status` (o dinheiro). Os estados de pagamento estão documentados em
`src/core/constants.py` — `on_delivery`, `pending`, `paid`, `failed`,
`refunded`.

Qual dos dois caminhos o pedido segue é decidido na criação, por
`OrderService._resolve_payment_flow` (`src/services/order_service.py:344`), a
partir do `payment_flow` da forma de pagamento **habilitada na filial**:

```
pagamento na entrega (payment_flow="delivery")
    pedido nasce status=pending, payment_status=on_delivery
    o lojista já pode aceitar

pagamento online (payment_flow="online")
    pedido nasce status=pending, payment_status=pending
    POST /restaurants/{slug}/orders/{tracking_token}/payment  → cria a cobrança
    gateway confirma → POST /payments/webhooks/{provider}     → payment_status=paid
    só então o lojista consegue aceitar
```

A regra central está em `order_state_machine.ensure_payment_allows_order_status`
(`src/services/order_state_machine.py:110`): nenhum pedido entra em
`accepted`/`preparing`/`ready`/`out_for_delivery`/`completed` com pagamento
online não confirmado. Sem isso bastava clicar em "aceitar" para o pedido ir
para a cozinha com o pix em aberto.

**Pagar não aceita o pedido automaticamente.** O webhook grava
`payment_status=paid` e registra no histórico; aceitar continua sendo decisão do
lojista — pagar não pode obrigar o restaurante a produzir.

O webhook (`src/services/payment_service.py:165`) faz, nesta ordem: confere a
assinatura (401 se inválida), traduz o corpo, acha o pedido por
`(payment_provider, provider_payment_id)`, reserva a chave de idempotência
usando o **id do evento** e só então aplica a transição. Corpo malformado,
pagamento desconhecido e transição impossível respondem **2xx** com warning no
log — 5xx colocaria o gateway em retentativa por horas sem consertar nada.

#### Os dois providers

`src/integrations/payment_gateway.py` implementa **dois** providers atrás das
mesmas três funções (`create_payment`, `verify_webhook_signature`,
`parse_webhook_event`) — nada fora desse arquivo sabe qual dos dois está ativo:

- `sandbox`: implementação **de verdade** (sem chamada externa) — cria a
  cobrança localmente e valida o webhook por HMAC-SHA256 do corpo cru com
  `PAYMENT_WEBHOOK_SECRET`. Permite exercitar o fluxo inteiro sem depender do
  Mercado Pago responder.
- `mercadopago`: chama a API de Pagamentos (v1) de verdade, Checkout
  Transparente, **hoje só pix** (cartão exige token gerado no frontend com o
  SDK deles — outra integração, fica para depois). Detalhes na 4.5.2.

Troca de um para o outro é só `PAYMENT_PROVIDER` no `.env`.

#### 4.5.2 Integração real com o Mercado Pago (pix)

**Criar a cobrança** (`create_payment`, provider `mercadopago`) —
`POST /v1/payments` com:

```
Authorization: Bearer {access_token}      credencial do restaurante do pedido
                                           (PaymentCredentialService, nunca global)
X-Idempotency-Key: {order_id}:{hash}      uma chave por TENTATIVA, ver abaixo
transaction_amount, description
payment_method_id: "pix"
payer.email                               ver payer_email abaixo
application_fee                           SO no corpo se != None (split, ver 4.5.1)
```

**A chave de idempotência é por tentativa, não por pedido.** O Mercado Pago
devolve a **mesma** cobrança quando a mesma chave chega de novo — é o que
protege um retry (timeout na ida, cliente clicando duas vezes em "pagar") de
virar duas cobranças pix abertas para o mesmo pedido. A chave era
`str(order_id)`, constante para sempre, e isso tratava como "a mesma
cobrança" duas coisas que não são:

- **retry depois de uma cobrança recusada.** A chave antiga devolve a
  própria cobrança recusada, e o pedido nunca mais teria como ser pago.
- **tentativa com o corpo diferente** — o total do pedido mudou, ou o
  cliente fez login entre uma tentativa e outra e o `payer.email` deixou de
  ser o sintético de convidado. Mesma chave com corpo diferente é
  **conflito** de idempotência, não retry, e conflito eles respondem com
  erro.

A chave passou a ser `{order_id}:{sha256(corpo + cobrança substituída)[:16]}`
(`payment_gateway._mercadopago_idempotency_key`). O `order_id` fica no começo
para a tentativa continuar achável no painel de "Atividade" deles a partir
do pedido. Quem informa a cobrança substituída é
`PaymentService.start_online_payment`, e só quando o pagamento está
`failed`:

```
payment_status = "pending"   previous_payment_id = None       chave se REPETE
                                                                (2º clique devolve o mesmo pix)
payment_status = "failed"    previous_payment_id = o recusado  chave MUDA
                                                                (cobrança nova, a recusada não volta)
```

A garantia original continua: retry por timeout na ida não gravou nada no
pedido, o corpo é o mesmo, a chave se repete e eles devolvem a cobrança que
já tinham criado. O que a mudança **não** faz é cancelar a cobrança antiga
lá quando o corpo muda (total editado, cliente logou) — nasce uma cobrança
nova e o QR antigo fica em aberto do lado deles até expirar.

`payer.email` é exigido pela API deles e o pedido **não guarda e-mail
nenhum** (só nome e telefone — `Order.customer_name_snapshot`/
`customer_phone_snapshot`). `PaymentService._resolve_payer_email` resolve
assim: cliente logado → `customers.email`; convidado → um e-mail sintético
próprio (`pedido-{order_number}@pederapidex.com`) — o Mercado Pago só usa
esse campo para validar o payer da cobrança pix, não manda nada para ele.

Da resposta, `PaymentIntent.qr_code` vem de
`point_of_interaction.transaction_data.qr_code` (o "copia e cola") e
`checkout_url` de `.ticket_url` (página hospedada pelo Mercado Pago com o QR
em imagem — não implementamos `qr_code_base64` para renderizar o QR inline;
hoje o front redireciona para `ticket_url`).

**Receber o webhook** (`verify_webhook_signature` + `parse_webhook_event`,
provider `mercadopago`) — duas coisas não óbvias, as duas resolvidas pela
mesma ordem de operações em `PaymentService.handle_webhook`:

- **o corpo do webhook não traz o status**, só avisa "o pagamento X mudou"
  (`data.id`). O status confiável exige `GET /v1/payments/{data.id}`,
  autenticado com a credencial do restaurante DONO do pagamento — e o
  webhook não diz de qual restaurante é.
- **a assinatura também é por restaurante** (`webhook_secret` em
  `restaurant_payment_credentials`, ver 4.5.1) — não existe mais uma
  `MERCADOPAGO_WEBHOOK_SECRET` global para verificar contra qualquer
  notificação. Só dá para saber qual segredo usar depois de saber de qual
  restaurante é o pagamento — o mesmo dado que falta para o `GET` acima.

```
1. extract_provider_payment_id                       so le data.id, nenhuma chamada externa nem ao banco
2. OrderRepository.get_order_by_provider_payment      SELECT indexado e local; acha o pedido (e o restaurante)
                                                       sem pedido correspondente -> ignored/unknown_payment, PARA AQUI
3. PaymentCredentialService.get_active_credential(order.restaurant_id)
4. verify_webhook_signature(secret=credential.webhook_secret)   SO AGORA confere a assinatura (401 se invalida)
5. parse_webhook_event(access_token=credential.access_token)    SO com assinatura valida faz o GET com o token certo
```

**Por que essa ordem não abre brecha.** A ordem antiga (assinatura antes de
tudo) existia para não gastar uma consulta FORJADA na API do Mercado Pago —
o passo 5, a única chamada de rede de verdade e paga. Essa garantia
continua: o passo 5 só roda depois da assinatura verificada, exatamente
como antes. O que mudou foi só re-ordenar os passos 1 e 2 para ANTES da
assinatura, e os dois são leitura local e barata (parse de JSON e um
`SELECT` por índice único, sem chamada externa nem efeito colateral) — o
pior que um corpo forjado com um `data.id` chutado consegue provocar é um
`SELECT` que não acha nada, e a resposta nesse caso (`ignored`,
`reason=unknown_payment`) já era pública antes desta mudança. Um provider
desconhecido (nem `sandbox` nem `mercadopago`) já é rejeitado no passo 1
(`PaymentProviderUnknownError` → 404), antes de qualquer leitura.

Assinatura (`x-signature: ts=...,v1=...` + `x-request-id`): manifest
`f"id:{data_id};request-id:{x_request_id};ts:{ts};"` (id em minúsculas),
`hmac_sha256` com o `webhook_secret` do RESTAURANTE (nunca uma constante
global), comparado com `hmac.compare_digest`. `ts` mais velho que 5
minutos é recusado — sem isso uma notificação capturada hoje serviria para
replay amanhã. Corpo malformado durante a verificação vira `False` (401),
nunca uma exceção. Restaurante sem `webhook_secret` cadastrado (ou sem
credencial nenhuma) responde 503, o mesmo comportamento de uma credencial
ausente em qualquer outro ponto do fluxo.

Tradução de status: `approved→paid`, `rejected`/`cancelled→failed`,
`refunded`/`charged_back→refunded`. `pending`/`in_process`/`authorized` (e
qualquer status novo que o Mercado Pago venha a inventar) não aplicam
mudança nenhuma — o pagamento ainda não tem veredito, e é o próprio gateway
quem manda um novo webhook quando isso mudar.

**Erros do gateway são todos explícitos**, nunca uma resposta plausível
silenciosa:

| Situação | Exceção | Como o PaymentService reage |
|---|---|---|
| timeout / falha de rede / 5xx | `PaymentGatewayUnavailableError` | 503 `gateway_unavailable` (create_payment) / 503 + gateway reenvia (webhook) |
| 401/403 (token inválido/revogado) | `PaymentGatewayCredentialError` | 503 `payment_unavailable` |
| 404 (payment_id não existe lá) | `PaymentNotFoundError` | 502 `payment_rejected` (create_payment) / `ignored, reason=payment_not_found` (webhook — não retentável) |
| 400/422 (requisição nossa recusada) | `PaymentGatewayError` | 502 `payment_rejected` |
| corpo da resposta não é JSON | `PaymentGatewayUnavailableError` | 503 `gateway_unavailable` |

#### O que o cliente recebe quando a cobrança não sai

Os códigos da coluna da direita são o `detail` da rota de criar cobrança, no
formato de `PaymentErrorDetail` (`src/schemas/payment_schema.py`) — um
**objeto**, não a string dos outros erros da API. Antes tudo saía como 503
com a mensagem interna (`"Mercado Pago com erro interno (status 500)"`) e o
frontend mostrava "erro interno" para qualquer coisa, sem ter como
distinguir "espere um minuto" de "não adianta insistir":

```json
{"detail": {"code": "gateway_unavailable",
            "message": "Não foi possível gerar o pagamento agora. Tente de novo em alguns instantes.",
            "retryable": true,
            "provider_error_code": null}}
```

| `code` | HTTP | `retryable` | Quando |
|---|---|---|---|
| `gateway_unavailable` | 503 | `true` | instabilidade passageira deles — a mesma chamada tem chance de funcionar daqui a pouco |
| `payment_unavailable` | 503 | `false` | pagamento online indisponível para **este restaurante**: sem credencial cadastrada, credencial recusada, método não suportado. Quem resolve é o lojista |
| `payment_rejected` | 502 | `false` | eles entenderam e **recusaram** a cobrança (dado inválido, conflito de idempotência) |

502 no recusado é proposital: 503 diz "volte depois", e voltar depois com a
mesma cobrança dá no mesmo. `provider_error_code` é o código do catálogo
deles (`"bad_request"`, `"2062"`) para citar num chamado de suporte — nunca
a mensagem crua, que pode ecoar o e-mail de quem pagou.

#### O log de uma chamada que deu errado

Chamada que deu certo rende uma linha só, sem o corpo da resposta (que traz
o dado de quem pagou):

```
[Pagamento][mercadopago] method=POST path=/v1/payments status=200 latency_ms=915.18
```

Chamada com status >= 400 rende **uma segunda linha** com o que eles
contam no corpo. Sem ela o log tinha o status HTTP e mais nada, e
`status=500` sozinho não distingue instabilidade deles de `payer.email`
recusado de chave de idempotência em conflito:

```
[Pagamento][mercadopago] erro method=POST path=/v1/payments status=500 \
    error=internal_error code=2062 message=... cause=code=2062 description=...
```

`code` é o primeiro `cause[].code` deles, ou o `error` genérico quando não
vem causa nenhuma. O access_token nunca esteve nessas linhas; o e-mail do
pagador sai mascarado como `[email]` (é o único dado dele que mandamos, e
volta ecoado na mensagem quando é recusado). Números **não** são mascarados
de propósito — levariam junto o id do pagamento e o código do erro, que são
exatamente o que se lê. A mensagem crua deles também não entra na exceção,
só o código: a exceção chega ao cliente.

Testes: `tests/test_mercadopago_gateway.py` cobre as três funções com o
gateway mocado (`httpx.Client` substituído) — formato da requisição,
tradução da resposta, todos os erros da tabela acima, o que a linha de erro
leva para o log (`ErrorLoggingTests`: código, causa, e-mail mascarado, id do
pagamento **não** mascarado), quando a chave de idempotência se repete e
quando muda (`IdempotencyKeyTests`), e o formato da assinatura (HMAC
calculado a mão no teste, corpo adulterado, timestamp velho).
`tests/test_payments.py` cobre a resolução de `payer_email`, qual cobrança
o service informa como substituída em cada `payment_status`, o `retryable`
de cada erro que chega ao cliente (`StartPaymentErrorTests`), a
ordem pedido→credencial→GET no webhook, e a ordem
extração→pedido→credencial→assinatura descrita acima — inclusive que o
segredo usado é sempre o do restaurante DO PEDIDO (o segredo de um
restaurante não valida o webhook de outro) e que um pagamento sem pedido
correspondente nunca chega a chamar `verify_webhook_signature`. Nenhum
teste chama o Mercado Pago de verdade — isso só se confirma testando de
ponta a ponta com a credencial de teste (ver relatório da integração para
o passo a passo).

#### 4.5.1 Credencial de pagamento por restaurante (BLOCO G)

Não existe uma credencial global do Mercado Pago. Cada restaurante tem a
própria conta lá, e a cobrança do pedido dele tem que sair em nome dessa
conta — daí `restaurant_payment_credentials`
(`alembic/versions/20260801_0008_credenciais_pagamento_por_restaurante.py`,
`webhook_secret_encrypted` acrescentada em
`20260801_0009_segredo_webhook_por_restaurante.py`), uma linha por
`(restaurant_id, environment)`:

```
public_key               texto puro    — o próprio Mercado Pago manda expor no frontend
access_token_encrypted   cifrado       — credencial da CONTA DO RESTAURANTE, nunca em log
webhook_secret_encrypted cifrado, NULLABLE — "Assinatura secreta" do painel, idem
environment               'test' | 'production'
```

`access_token` e `webhook_secret` são cifrados com Fernet
(`src/utils/crypto.py`), chave em `PAYMENT_CREDENTIALS_ENCRYPTION_KEY` (só no
`.env`, nunca no banco — cifrar a coluna não protege nada se a chave mora ao
lado dela). `startup_checks.py` derruba o boot se `PAYMENT_PROVIDER=mercadopago`
e a chave estiver vazia. `webhook_secret_encrypted` é `NULL` até o
restaurante cadastrar a Notification URL no painel do Mercado Pago e receber
a Assinatura secreta — um passo que pode acontecer depois do `access_token`;
enquanto estiver `NULL`, o webhook deste restaurante responde 503 (ver
4.5.2), do mesmo jeito que uma credencial ausente.

Teste e produção **coexistem** no banco. Qual das duas vale para todo mundo é
a variável global `MERCADOPAGO_ENVIRONMENT` (`test` por padrão) — trocar para
produção é mudar essa variável no `.env`, sem tocar em código nem apagar a
credencial de teste.

`PaymentCredentialService.get_active_credential` (`src/services/payment_credential_service.py`)
busca a linha do `(restaurant_id, MERCADOPAGO_ENVIRONMENT)` e devolve
`access_token` e `webhook_secret` já decifrados (`webhook_secret` vem `None`
quando a coluna está `NULL`). `PaymentService.start_online_payment` chama
isso **antes** de falar com o gateway e passa o `access_token` para
`create_payment(..., access_token=...)` — não existe caminho em que a
cobrança saia sem a credencial do restaurante do pedido. Sem credencial
cadastrada para o ambiente ativo, `create_payment` responde
`PaymentProviderNotConfiguredError` → **503**, o mesmo comportamento de
antes quando a credencial global estava vazia. `PaymentService.handle_webhook`
usa o `webhook_secret` da mesma forma — ver a ordem em 4.5.2.

`create_payment` também aceita `application_fee` (o corte da plataforma no
split de pagamento do Mercado Pago), **opcional e hoje sempre `None`**: não
há contrato de marketplace assinado com nenhum restaurante ainda. O campo
existe para não exigir mexer na assinatura da função no dia em que existir.

Sem tela de cadastro, quem registra a credencial é
`scripts/register_restaurant_payment_credential.py` — mesma forma de
`scripts/create_admin_user.py` (os segredos são pedidos em prompt oculto,
nunca argumento de linha de comando):

```
python scripts/register_restaurant_payment_credential.py \
    --restaurant-slug junior-da-picanha \
    --environment test \
    --public-key TEST-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Pede `access_token` (obrigatório) e, em seguida, `webhook_secret`
(opcional — Enter em branco pula e **não mexe** no que já estava cadastrado,
se houver). Rodar de novo para o mesmo `(restaurant, environment)`
substitui a credencial (`ON CONFLICT DO UPDATE` em
`RestaurantPaymentCredentialRepository.upsert`) — é como se troca um token
vazado; `webhook_secret_encrypted` só entra nesse `UPDATE` quando um valor
novo é informado, para rodar de novo (rotacionar `access_token`, por
exemplo) não apagar um segredo de webhook já cadastrado.

Para atualizar **só** o segredo do webhook de uma credencial que já existe
— o caso de uma credencial cadastrada antes deste campo existir —, sem
re-informar `access_token` nem `public_key`:

```
python scripts/register_restaurant_payment_credential.py \
    --restaurant-slug junior-da-picanha \
    --environment test \
    --webhook-secret-only
```

Isso chama `RestaurantPaymentCredentialRepository.update_webhook_secret`, um
`UPDATE` parcial que recusa (mensagem clara, sem criar linha) se a
credencial ainda não existir.

### 4.6 Comissão da plataforma (Fase 2)

Gravada **no pedido, na criação** (`order_service.py:614`), não apurada no
fechamento do mês:

```
base    = subtotal − desconto de cupom − cashback usado     (nunca negativa)
percent = restaurant_settings.platform_commission_percent   (por restaurante)
valor   = base × percent / 100                              (2 casas, HALF_UP)
```

Ficam **fora** da base: taxa de entrega, taxa de serviço e taxa do gateway — as
duas primeiras são repasse, a terceira é custo de quem recebe.

São três colunas em `orders` (`commission_percent`, `commission_base_amount`,
`commission_amount`) porque o valor sozinho não é conferível: com base e
percentual gravados, o lojista refaz a conta de cada linha do extrato.

Congelar é o ponto: se fosse calculado no fim do mês, mudar o percentual do
restaurante hoje reescreveria a comissão de pedidos de três semanas atrás.

`GET /admin/reports/commission?start_date&end_date`
(`src/services/admin_report_service.py:33`) só **soma** o que está gravado.
Cancelados, recusados e estornados não entram, e quantos ficaram de fora vem em
`excluded_orders_count` — sem esse número, a diferença para o painel parece bug.
As datas são lidas em America/Fortaleza e o fim é exclusivo no dia seguinte,
para não perder pedido das 23:59.

### 4.7 Consulta de pedido pelo cliente (Fase 2)

A rota `GET /orders/{order_number}?phone=...` **foi removida**. Ela casava um
`order_number` vindo de sequence **global** com o telefone: com um telefone em
mãos, dava para varrer os números vizinhos e ler endereço residencial, itens e
histórico de outras pessoas.

Duas rotas no lugar:

| Quem | Rota | Como autoriza |
|---|---|---|
| Convidado | `GET /restaurants/{slug}/orders/track/{tracking_token}` | token opaco sorteado na criação, devolvido **uma vez** a quem criou |
| Cliente logado | `GET /customers/me/orders/{order_id}` | `orders.customer_id`, sem token nenhum |

O token é `secrets.token_urlsafe(32)` (`src/utils/security.py`), gravado em
`orders.tracking_token` com UNIQUE. Pedidos anteriores à migração receberam
token sorteado no banco: como ninguém os tem em mãos, eles deixaram de ser
consultáveis pela rota pública — que era o objetivo.

## 5. Onde ficam as regras de negócio

| Regra | Arquivo:linha |
|---|---|
| **Taxa de entrega** = base + (km × valor/km), com piso e teto | `src/services/delivery_estimate_service.py:607` |
| Filial sem `delivery_base_fee`/`delivery_fee_per_km` → não atende | `src/services/delivery_estimate_service.py:607` |
| **Raio máximo de entrega** | `src/services/delivery_estimate_service.py:629` |
| **Tempo de preparo** (por filial, dia da semana e faixa de horário) | `src/services/delivery_estimate_service.py:589` |
| Escolha da faixa de horário (a que contém o agora, ou fechado) | `src/services/branch_hours_service.py:35` |
| ETA = preparo + tempo de viagem | `src/services/delivery_estimate_service.py:425` |
| Fuso horário da operação (America/Fortaleza) | `src/core/constants.py` |
| TTL do cache de estimativa (600s / 120s negativo) | `src/core/config.py:49-50` |
| **Valor mínimo do pedido** (compara com o **subtotal**) | `src/services/order_service.py:662` |
| **Taxa de serviço** (liga/desliga + valor, por restaurante) | `src/services/order_service.py:657` |
| Subtotal = Σ (preço do produto + opções) × quantidade | `src/services/order_service.py:598` |
| Total = subtotal + serviço + entrega − descontos | `src/services/order_service.py:141` |
| Arredondamento de dinheiro (2 casas, HALF_UP) | `src/utils/money.py:15-16` |
| **Elegibilidade de cupom** (ordem completa das checagens) | `src/services/coupon_service.py:66-140` |
| Cálculo do desconto (fixo / percentual / frete grátis) | `src/services/coupon_service.py:47-64` |
| Teto do desconto percentual (`max_discount_amount`) | `src/services/coupon_service.py:57-58` |
| Cupom exige cliente autenticado | `src/services/coupon_service.py:250-251` |
| Trava do cupom (`SELECT FOR UPDATE`) | `src/repositories/coupon_repository.py:47-58` |
| Cooldown em dias entre usos | `src/services/coupon_service.py:115-127` |
| "Só no primeiro pedido" | `src/services/coupon_service.py:128-129`, `coupon_repository.py:124-130` |
| Estorno do cupom ao cancelar/rejeitar | `src/services/admin_order_service.py:139` → `coupon_service.py:282-285` |
| Opções obrigatórias, mínimo e máximo por grupo | `src/services/order_service.py:543` |
| Endereço obrigatório para entrega | `src/services/order_service.py:390` |
| Telefone gravado e comparado só em dígitos | `src/schemas/order_schema.py:20-31`, `order_service.py:153`, `:266` |
| Tipos de pedido, status, formas e estados de pagamento | `src/core/constants.py` |
| **Transições válidas de status** (grafo, terminais) | `src/services/order_state_machine.py:30` |
| **Pedido online só entra na cozinha depois de pago** | `src/services/order_state_machine.py:110` |
| Transições válidas de `payment_status` | `src/services/order_state_machine.py:52` |
| Loja aberta / aceita entrega / aceita retirada | `src/services/order_service.py:315` |
| Horário de funcionamento da filial (inclui virada de noite) | `src/services/branch_hours_service.py:35` |
| Forma de pagamento válida e habilitada na filial | `src/services/order_service.py:344` |
| **Comissão da plataforma** (base, percentual, valor) | `src/services/order_service.py:614` |
| Percentual de comissão por restaurante | `restaurant_settings.platform_commission_percent` |
| Recorte e exclusões do relatório de comissão | `src/services/admin_report_service.py:33` |
| Reaproveitamento da estimativa de entrega | `src/services/order_service.py:463` |
| Identidade do endereço estimado (fingerprint) | `src/services/delivery_estimate_service.py:38` |
| Assinatura e tradução do webhook do gateway | `src/integrations/payment_gateway.py` |
| Revogação de JWT na troca de senha | `src/services/auth_service.py` |
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


   delivery_estimates  ◄──── estimativa já calculada, reaproveitada na criação do
                             pedido por token (TTL curto). Aponta para restaurante,
                             filial e, quando há login, cliente.

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
`/customers/me/*` (perfil, endereços, histórico, detalhe de pedido, cashback) e
`POST /restaurants/{slug}/coupons/preview`. Sem token → 401; conta inativa → 403.

Desde a Fase 2 o token também é conferido contra `customers.password_changed_at`:
JWT emitido antes da última troca de senha é recusado, mesmo dentro da validade.
É o que permite ao cliente expulsar quem entrou na conta dele — antes, um token
roubado sobrevivia à troca de senha por até 7 dias.

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
- `GET /admin/reports/commission` — comissão do período com extrato
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
py -m pytest tests -q          # 245 testes, ~6s
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
| `[Pagamento][mercadopago]` | latência/status de cada chamada e, no erro, o código e a causa que eles devolveram | `payment_gateway.py:_call_mercadopago` (ver 4.5.2) |
| `[Pagamento]` | cobrança que não saiu, webhook aplicado ou ignorado | `payment_service.py` |
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

O primeiro apaga chaves de idempotência **e estimativas de entrega** vencidas —
seguro, passado o TTL nenhuma das duas protege ou vale mais nada
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
| ~~Consulta pública do pedido~~ | removida na Fase 2 — ver 4.7 |

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

### 9.2 `is_open` e `accepts_pickup` (corrigido na Fase 2)

**Era um bug, foi corrigido.** `restaurant_settings.is_open`, `accepts_delivery`
e `accepts_pickup` existiam, apareciam no cardápio e **não bloqueavam nada**:
quem impedia pedido com a loja fechada era o frontend, então bastava chamar a
API direto para furar.

Agora os três são lidos em `OrderService._validate_store_accepts_order`
(`src/services/order_service.py:315`), chamado antes da reserva de
idempotência. `accepts_delivery` e `accepts_pickup` são conferidos **por tipo
de pedido** — desligar retirada não pode derrubar a entrega junto.

Restaurante **sem linha** em `restaurant_settings` continua aceitando pedido: é
o comportamento que sempre valeu, e mudá-lo agora derrubaria quem nunca
configurou nada.

### 9.3 `payment_method` (corrigido na Fase 2)

**Era um bug, foi corrigido.** O campo era string livre de 50 caracteres que ia
direto para `orders.payment_method`; `PAYMENT_METHODS` estava definido e não era
usado em lugar nenhum. Dava para fechar pedido com `payment_method="banana"`, e
a filial recebia pedido numa forma que não aceita.

Agora `OrderService._resolve_payment_flow` (`src/services/order_service.py:344`)
confere **duas listas**: `PAYMENT_METHODS` (o que a plataforma conhece,
`src/core/constants.py`) e `branch_payment_methods` habilitados **daquela
filial**. Sem forma de pagamento, ou com uma que a filial não oferece, é 400.

A constante foi ampliada para espelhar o CHECK de
`branch_payment_methods.method_type` (`cash`, `voucher`, `meal_voucher`,
`other` entraram). **Os dois lados têm que mudar juntos**: método que exista só
no banco faz a filial oferecer algo que o pedido recusa.

Efeito colateral desta função: ela também devolve o `payment_flow`
(`online` ou `delivery`), que é o que decide se o pedido nasce devendo. Quando a
filial oferece o mesmo método nos dois fluxos (pix pelo gateway **e** pix na
entrega), vale `online` — o caminho restritivo, que exige pagamento antes da
cozinha.

### 9.4 Horário da filial (corrigido na Fase 2)

**Eram dois bugs, foram corrigidos.**

O primeiro: quando a hora atual não caía em nenhuma faixa do dia,
`_select_business_hour_for_prep_time` usava a **primeira faixa cadastrada**. Às
3h da manhã o pedido passava com o tempo de preparo do almoço — e passava com a
loja fechada.

O segundo: `is_closed` era ignorado. Uma linha marcada como fechada mas com
horários preenchidos (o jeito comum de registrar "domingo fechado") abria a
filial.

A regra agora está em `src/services/branch_hours_service.py`: ou existe faixa
que contém o agora, ou está fechado. Não existe faixa "mais parecida". Faixa que
vira a noite (18:00–02:00) pertence ao dia em que **começa**, então
`find_current_period` (`:35`) consulta hoje **e ontem** — sem isso, a lanchonete
noturna recusaria pedido à 1h da manhã.

`ensure_branch_is_open` (`:55`) é chamado na criação do pedido para **os dois
tipos**. Pedido de retirada não passa pela estimativa de entrega, então antes
disso nunca chegava perto de uma checagem de horário.

Sintoma que continua valendo: sem faixa cadastrada para o dia da semana atual, a
filial está fechada e nenhum pedido entra. Confira `branch_business_hours` para
o `weekday` de hoje (0 = segunda), com `prep_time_min` e `prep_time_max`
preenchidos.

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

Entre `begin()` (`order_service.py:95`) e o `try` (`:129`) há validações que podem
levantar `HTTPException`, e **não** há `rollback()` nesse trecho. Está correto — a sessão é
fechada sem commit em `src/db/session.py:24-26` e o Postgres descarta a transação — mas é
implícito.

O que isso significa para você: **se algum dia alguém colocar um `db.commit()` entre as linhas
95 e 129**, a reserva passa a ser persistida sem o pedido, e a chave fica queimada por 24h
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

### 9.11 Máquina de estados de pedido (corrigido na Fase 2)

**Era um bug, foi corrigido.** `update_order_status` validava apenas que o
status **existia** em `ORDER_STATUSES`. `completed → pending` era aceito, e
`cancelled → completed` também — o pedido cancelado (cujo cupom já tinha sido
estornado) era faturado, e o cliente levava o desconto duas vezes.

O grafo agora é explícito em `src/services/order_state_machine.py:30`, e
`completed`, `cancelled` e `rejected` são **terminais**. `rejected` entrou na
lista junto com os dois obrigatórios pelo mesmo motivo: "desrecusar" traz de
volta o problema do cupom estornado.

Duas armadilhas de ordem que valem conhecer:

- A validação da transição roda **depois** do replay de idempotência
  (`admin_order_service.py:123`). Validando antes, um retry legítimo da mesma
  chave morreria com "o pedido já está em accepted" em vez de devolver a
  resposta gravada.
- `ensure_payment_allows_order_status` bloqueia a entrada na cozinha enquanto o
  pagamento online não confirma, mas **nunca** bloqueia `cancelled`/`rejected`:
  são justamente a saída para o pagamento que não chegou.

Continua valendo o aviso sobre o estorno de cupom: ele acontece em `cancelled` e
`rejected` e `reverse_redemption` é idempotente, então o resgate fica `reversed`
para sempre. Como agora não se sai de estado terminal, o cenário
"cancelar e descancelar" deixou de existir.

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

### 9.15 Pagamento: o que a Fase 2 deixou de fora, de propósito

Três buracos conhecidos, todos com o lugar de resolver já escolhido:

1. **O Mercado Pago não está implementado.** As três funções de
   `src/integrations/payment_gateway.py` levantam
   `PaymentProviderNotConfiguredError` e a API responde **503**. Falhar alto é
   proposital — um retorno plausível deixaria pedidos pendentes para sempre.
   Com `PAYMENT_PROVIDER=sandbox` (padrão) o fluxo roda inteiro sem gateway.
2. **Cancelar um pedido pago NÃO estorna.** O estorno só acontece quando o
   gateway avisa (webhook com `refunded`) ou quando alguém o faz no painel do
   próprio gateway. Cancelamento de pedido `paid` sai no log como
   `[Pagamento] pedido pago foi cancelled sem estorno automatico`
   (`admin_order_service.py:169`) — é o único rastro de dinheiro de cliente
   parado. Bota esse grep no radar.
3. **Pagamento recusado não cancela o pedido.** Ele fica `status=pending` com
   `payment_status=failed`, e a máquina de estados impede que seja aceito. Fica
   visível para o lojista cancelar, e o cliente pode gerar uma nova cobrança
   para o mesmo pedido (`failed → pending`).

Ainda: um pedido **estornado** não consegue avançar de status, porque `refunded`
não está em `PAYMENT_STATUSES_THAT_RELEASE_ORDER`. É intencional — pedido cujo
dinheiro voltou não deve continuar na cozinha.

### 9.16 O `tracking_token` só sai da API uma vez

`CreateOrderResponse.tracking_token` é a única resposta que carrega o token. Não
existe rota para reemitir: o segredo **é** a chave de acesso, e uma rota de
"me manda meu token de novo" precisaria autenticar por... telefone e número de
pedido, que é exatamente o buraco que a Fase 2 fechou.

Consequências práticas:

- O front **tem** que guardar o token (localStorage) ao criar o pedido de
  convidado. Perdeu, o convidado não acompanha mais aquele pedido.
- Cliente logado não depende disso: `GET /customers/me/orders/{order_id}`
  deriva o acesso de `customer_id`.
- O token fica gravado também em `idempotency_keys.response_body`, porque a
  resposta inteira é armazenada lá. Não é vazamento novo (a tabela já guardava
  a resposta do pedido), mas conta na hora de decidir quem lê aquela tabela.

### 9.17 Estimativa reaproveitada: quando ela NÃO é usada

`delivery_estimate_token` é uma otimização de custo, nunca uma fonte de valor.
`OrderService._reuse_stored_estimate` (`order_service.py:463`) descarta a
estimativa guardada — e chama o Google de novo — quando a linha venceu, é de
outro restaurante, de outra filial, de outro cliente ou de outro endereço.

O `grep` para quando a conta do Maps subir:
`[Delivery estimate] estimativa nao reaproveitada motivo=`.

Um motivo legítimo e frequente: o cliente pediu a estimativa e demorou mais de
`DELIVERY_ESTIMATE_REUSE_TTL_SECONDS` (15 min) para fechar o pedido. Aumentar
esse TTL reduz custo e aumenta a chance de cobrar uma taxa calculada com o
trânsito de meia hora atrás.
