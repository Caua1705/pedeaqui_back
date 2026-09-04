# Mapa do backend

Porta de entrada da documentação. Este arquivo responde "onde eu mexo?".
O detalhe de cada assunto está nos arquivos linkados no fim.

Citações são `arquivo.py` + **nome da função**, não `arquivo:linha`. Número de
linha derrapa a cada commit e o documento vira mentira em silêncio; nome de
função você acha com Ctrl+F e continua achando daqui a seis meses.

---

## 1. O que é

API de pedidos de restaurante, multi-restaurante desde a base: cada restaurante
é um `slug` na URL pública e um `restaurant_id` em toda consulta. Serve o
cardápio, autentica cliente e lojista, calcula taxa de entrega pelo Google
Routes, aplica cupom, grava o pedido, cobra pelo Mercado Pago, expõe o painel do
lojista e monta as comandas que o agente de impressão manda para as impressoras
da loja.

FastAPI + SQLAlchemy sobre PostgreSQL (Supabase). O schema existia antes do
Alembic entrar — daí a revisão baseline.

---

## 2. Mapa de pastas

```
main.py                    monta o app: middlewares, exception handlers, routers
src/
  api/
    endpoints/             camada HTTP. Uma função por rota. Sem regra de negócio.
    dependencies/          o que o FastAPI injeta: sessão, cliente logado, lojista, escopo
    middleware/            middleware ASGI puro (hoje só o limite de tamanho de corpo)
    rate_limit.py          limites por rota e extração do IP real
    validation_errors.py   log extra quando o Pydantic recusa corpo em rota crítica
    chat.py                rotas do chat (fora de endpoints/ por histórico)
  services/                REGRA DE NEGÓCIO. Orquestra, valida, decide, commita.
  repositories/            SÓ consulta SQL. Não decide, não levanta erro de regra.
  schemas/                 DTOs Pydantic de entrada e saída (o contrato HTTP)
  models/                  modelos SQLAlchemy espelhando as tabelas
  integrations/            serviço externo: Google Routes, Mercado Pago, Supabase Storage
  ai/                      prompts, embeddings, busca vetorial, chamada ao LLM
  core/                    config, constantes, validação de config no boot
  db/                      engine, sessionmaker, get_db
  utils/                   dinheiro (Decimal), normalização, senha/token, cripto, imagens
alembic/versions/          migrações versionadas — a fonte da verdade do schema
migrations/                arquivo histórico congelado dos 12 .sql aplicados a mão. NÃO rode.
scripts/                   operação: criar lojista, credencial de pagamento, limpeza,
                           e o worker que mantém o índice do Rapi em dia
print-agent/               projeto Python SEPARADO, roda no PC da loja. Ver impressao.md
tests/                     614 testes da API, sem banco real (fakes em memória)
```

### Quem chama quem

```
endpoint  →  service  →  repository  →  banco
   ↓           ↓
dependency   integration / ai
```

- **endpoint → service**: sempre. O endpoint recebe, injeta e devolve.
- **service → repository**: sempre.
- **service → service**: permitido e usado. `OrderService` chama `CouponService`,
  `DeliveryEstimateService`, `IdempotencyService`, `RestaurantService`.
- **service → integration / ai**: permitido. Só o service fala com o mundo externo.
- **repository → qualquer coisa fora do banco**: **proibido**. Repositório não
  decide, não levanta erro de regra, não commita.
- **endpoint → repository**: **proibido**. Não existe hoje; não crie.

Quem commita é **o service**, sempre. O repositório faz `flush()` para pegar o id
gerado; o `commit()` fica no service para várias escritas caírem na mesma
transação. Não há `controllers/` — os endpoints são essa camada.

---

## 3. O caminho de um pedido, do clique ao prato

Esta é a espinha do sistema. Sete etapas, cada uma num arquivo diferente.

### Etapa 1 — o cliente fecha o pedido

`POST /restaurants/{slug}/orders`

| Ordem | O que acontece | Onde |
|---|---|---|
| 1 | Limite de corpo (413 antes de carregar o JSON) | `api/middleware/body_size.py` |
| 2 | CORS (camada mais externa, para 413/429 saírem com header) | `main.py` |
| 3 | Rate limit por IP, `10/minute;60/hour` | `api/rate_limit.py` |
| 4 | Validação de forma (422) | `schemas/order_schema.py` |
| 5 | Cliente logado **opcional** — token ruim devolve `None`, não erro | `api/dependencies/customer_auth.py` |
| 6 | `Idempotency-Key` normalizada | `services/idempotency_service.py` |

Dentro de `OrderService.create_order` (`services/order_service.py`), nesta ordem:

```
restaurante ativo pelo slug ................. 404
order_type ∈ {delivery, pickup} ............. 400
filial ativa E deste restaurante ............ 400
resolve o endereço .......................... 401 / 404
exige cliente (logado ou no corpo) .......... 401
carrega restaurant_settings
loja aberta / aceita este tipo .............. 400   _validate_store_accepts_order
filial dentro do horário .................... 400   BranchHoursService.ensure_branch_is_open
forma de pagamento válida e habilitada ...... 400   _resolve_payment_flow  → define payment_flow
──────────────────────────────────────────────────
►►► RESERVA A CHAVE DE IDEMPOTÊNCIA ◄◄◄            IdempotencyService.begin
──────────────────────────────────────────────────
endereço completo se for delivery ........... 400
estimativa de entrega ....................... 400 se fora de área
   └─ reaproveita delivery_estimate_token quando válido; senão chama o Google (PAGO)
produtos válidos, ativos, DESTE restaurante . 400
valida opções selecionadas .................. 400
subtotal .................................... _calculate_subtotal
valor mínimo do pedido ...................... 400
taxa de serviço / taxa de entrega
──────────────────────────────────────────────────
►►► try ◄◄◄
resolve o cupom: o escolhido, ou o automático  _resolve_coupon
trava e valida o cupom (SELECT FOR UPDATE) .. 400/401/404
total = subtotal + serviço + entrega − descontos
comissão da plataforma (congelada no pedido)  _calculate_commission
INSERT orders  (tracking_token_hash, payment_status, comissão)
INSERT coupon_redemptions / order_items / order_item_options
INSERT order_status_history ("pending", changed_by "system")
UPDATE idempotency_keys → completed + resposta gravada
►►► COMMIT ◄◄◄
except: ROLLBACK e propaga
```

As três checagens de loja (aberta, horário, forma de pagamento) ficam **antes**
da reserva de idempotência de propósito: são consultas baratas e recusam cedo o
pedido que a loja não poderia receber, sem queimar chave nem chamada paga.

O pedido nasce `status="pending"`. O `payment_status` depende do fluxo:
`on_delivery` (paga na entrega, estado final) ou `pending` (online, esperando o
gateway). Ver [pagamentos-e-comissao.md](pagamentos-e-comissao.md).

### Etapa 2 — o pagamento, se for online

`POST /restaurants/{slug}/orders/{tracking_token}/payment` cria a cobrança;
`POST /payments/webhooks/{provider}` recebe a confirmação e grava
`payment_status="paid"`. Pagar **não** aceita o pedido — só libera o botão.

### Etapa 3 — o painel do lojista vê o pedido

`GET /admin/orders` lista, e `GET /admin/orders/stream` é um SSE que empurra
`order.created` e `order.status_changed` (`services/admin_order_stream_service.py`).

O cursor do stream mora no **banco**, não em memória: cada poll pergunta "o que
aconteceu depois de X" em `orders` e `order_status_history`. É o que faz o stream
sobreviver a deploy e a mais de um worker. Cada poll olha `OVERLAP_SECONDS`
para trás, então evento repetido é normal — todo evento carrega `event_key`
estável e o consumidor descarta o que já aplicou.

O `EventSource` do navegador não manda header, então o stream autentica por
ticket de 30s obtido em `POST /admin/orders/stream-ticket`.

### Etapa 4 — o lojista aceita

`PATCH /admin/orders/{id}/status` → `AdminOrderService._apply_status_change`.

Aqui rodam as duas guardas, nesta ordem: `ensure_order_transition_allowed` (o
grafo) e `ensure_payment_allows_order_status` (o dinheiro). Ver a seção 5.

`PATCH /admin/orders/{id}/cancel` **não** é uma segunda escrita de status, e
desde 25/08/2026 nem a terceira porta é: as quatro — o PATCH de status, o cancelamento
pelo painel, o **cancelamento pelo cliente** e, desde 03/09/2026, o
**entregador** marcando "saiu" e "entregue" — desembocam em
`OrderStatusChangeService.apply`, que é o único lugar do sistema que grava
`orders.status`. Escritas independentes seriam a chance de a máquina de estados
valer numa e não na outra; com dinheiro em cima (cupom, cashback e estorno saem
todos dali), seriam quatro bugs por um copiar e colar. Ver a seção 5.1.

### Etapa 5 — o agente de impressão escuta

O `print-agent` (projeto separado, roda no PC da loja) mantém o mesmo stream SSE
aberto. Quando vê um pedido no status configurado (`trigger_status`), chama
`GET /admin/orders/{id}/print-jobs`.

### Etapa 6 — a API monta as vias

`AdminPrintingService.build_print_jobs` devolve as comandas **já prontas em
texto**: uma via do cliente e uma via de produção por setor que tenha item.
O desenho de cada via está em `services/print_layout.py`, sem banco.

Pedido com pagamento online não confirmado **não gera via de produção** — comanda
impressa é ordem de produção. A via do cliente continua saindo.

### Etapa 7 — sai o papel

O agente manda cada via para a impressora daquele setor via ESC/POS. Ele é burro
de propósito: não sabe o que é adicional nem o que é pedido não pago. Regra que
mora nele só se corrige indo até a loja.

Detalhe completo em [impressao.md](impressao.md).

---

## 4. Onde mora o dinheiro

**Não encoste nestes pontos sem cuidado.** Erro aqui não aparece como erro:
aparece como valor errado semanas depois.

| O quê | Arquivo | Função |
|---|---|---|
| **Preço unitário do item** (produto + adicionais) | `services/order_service.py` | `_build_order_item` |
| **Subtotal** Σ (preço + opções) × quantidade | `services/order_service.py` | `_calculate_subtotal` |
| **Taxa de serviço** | `services/order_service.py` | `_calculate_service_fee` |
| **Valor mínimo do pedido** (compara com o subtotal) | `services/order_service.py` | `_validate_minimum_order_value` |
| **Taxa de entrega** = base + km × valor/km, com piso e teto | `services/delivery_estimate_service.py` | ver entrega-e-horarios.md |
| **Desconto do cupom** (fixo / percentual / frete grátis) | `services/coupon_service.py` | `calculate_discount` |
| **Cabe ou não cabe** (visibilidade, janela, tetos, mínimo) — listagem, preview e pedido | `services/coupon_service.py` | `evaluate`; ver [cupons.md](cupons.md) |
| **Total** = subtotal + serviço + entrega − descontos | `services/order_service.py` | dentro de `create_order` |
| **Comissão da plataforma** | `services/order_service.py` | `_calculate_commission` |
| **Cobrança criada no gateway** | `integrations/payment_gateway.py` | `create_payment` |
| **Pagamento confirmado** | `services/payment_service.py` | `handle_webhook` |
| **Arredondamento** (2 casas, ROUND_HALF_UP) | `utils/money.py` | `quantize_money` |

Regras que valem para tudo acima:

- **Nenhum preço vem do cliente.** O corpo manda `product_id`, `quantity` e
  `selected_options`. Todo valor é recalculado no servidor.
- **Tudo em `Decimal`.** `float` só aparece na serialização da resposta
  (`money_to_float`). Nunca no cálculo.
- **Snapshots congelam o pedido.** `product_name_snapshot`, `unit_price_snapshot`,
  `additional_price_snapshot`, `coupon_code_snapshot`. Mudar o preço amanhã não
  altera o pedido de ontem.
- **A comissão é congelada na criação**, com os três campos juntos
  (`commission_percent`, `commission_base_amount`, `commission_amount`). Base =
  subtotal − cupom − cashback, nunca negativa. Ficam **fora** da base: taxa de
  entrega, taxa de serviço e taxa do gateway.

Detalhe em [pagamentos-e-comissao.md](pagamentos-e-comissao.md).

---

## 5. A máquina de estados do pedido

Escrita em **um arquivo só**: `src/services/order_state_machine.py`.

Um pedido carrega **duas** máquinas ao mesmo tempo.

### `orders.status` — o ciclo operacional

```
pending ──┬─→ accepted ──→ preparing ──→ ready ──┬─→ out_for_delivery ──→ completed
          │                                       └─→ completed
          ├─→ rejected   (terminal)
          └─→ cancelled  (terminal)

  accepted, preparing, ready, out_for_delivery  também podem ir para cancelled
  completed, cancelled, rejected                são TERMINAIS: não saem mais
```

Não há atalho: `accepted` não pula para `ready`, `preparing` não volta para
`accepted`. Abrir um atalho é acrescentar um destino em `ORDER_STATUS_TRANSITIONS`
e mais nada.

`out_for_delivery` só existe para `order_type == "delivery"`.

### `orders.payment_status` — o dinheiro

```
on_delivery  (terminal — paga na entrega, nasce e morre assim)
pending ──→ paid ──→ refunded (terminal)
   └──→ failed ──→ pending    (cartão negado / pix expirado: pode tentar de novo)
```

### 5.1 Quem pode cancelar, e até quando

A máquina de estados diz o que é **possível**; esta regra diz quem tem
**autoridade**, e o eixo é um só: *antes do preparo cancelar é barato, depois
alguém come o prejuízo.*

| Quem | Até onde | O que o backend exige |
|---|---|---|
| **cliente** (`POST /restaurants/{slug}/orders/track/{token}/cancel`) | `pending`, `accepted` | nada além do `tracking_token`; motivo é opcional |
| **lojista** (`PATCH .../cancel` e `PATCH .../status`) | qualquer estado não-terminal | motivo obrigatório; **e confirmação explícita a partir de `preparing`** |
| **entregador** (`POST /courier/{link}/orders/...`) | só `ready → out_for_delivery → completed` | a atribuição aberta com o `courier.id` dele; nem cancela nem volta. Ver [entregadores.md](entregadores.md) |

**`preparing` entra na faixa que exige confirmação**, e a escolha é
deliberada: "a comida já foi feita" começa quando ela começa a ser feita, não
quando fica pronta. Em `accepted` o lojista só aceitou, e o pedido pode nem ter
chegado à praça.

**A confirmação responde 428, não 409**, e o código é contrato. Os 409 dessas
rotas são conflitos de estado de verdade ("pedido já entregue não muda mais") e
saem com `detail` de texto; o 428 não é erro nenhum — é o backend pedindo uma
precondição que o painel satisfaz na hora, com um corpo **tipado**
(`CancelOrderErrorResponse`, código `confirmation_required`) que diz qual
diálogo abrir e em que status o pedido está. Sobrepor os dois no mesmo código
obrigaria o painel a distinguir pelo texto da mensagem.

**A confirmação vale nas DUAS rotas do painel.** `PATCH /status` aceita
`status="cancelled"` e seria exatamente a porta pela qual o painel pularia o
diálogo — `confirm_prepared_order` existe nos dois corpos de requisição, opcional
com default `false` para não quebrar painel já instalado no minuto do deploy.

**Recusar (`rejected`) nunca pede confirmação:** o grafo só o alcança a partir
de `pending`, onde nada foi preparado.

O cancelamento do cliente passa pela mesma escrita do painel, então o cupom
volta, o cashback volta e **o pagamento é estornado** — um pix pago e cancelado
em seguida volta sem ninguém ligar para o restaurante.

### A regra que amarra as duas

`ensure_payment_allows_order_status`: um pedido com pagamento **online** só entra
no fluxo operacional (`accepted`, `preparing`, `ready`, `out_for_delivery`,
`completed`) depois que o gateway confirmou. Só `paid` e `on_delivery` liberam.

`cancelled` e `rejected` ficam liberados em qualquer estado de pagamento — são
justamente a saída para o pagamento que nunca chegou.

Mudança de pagamento entra em `order_status_history` com o prefixo `payment:`
para não se confundir com status operacional na mesma tabela.

---

## 6. Integrações externas e o que acontece quando caem

| Integração | Para quê | Se cair |
|---|---|---|
| **Google Routes** (`integrations/google_maps_routes_client.py`) | geocodifica o endereço e calcula distância/tempo | Sem taxa calculada. Cai para o `default_delivery_fee` resolvido da filial (ver `operacao-por-filial.md`) se houver (`provider="configured_fallback"`); **sem ela, todo pedido de entrega é recusado** com `route_unavailable`. Retirada continua funcionando. A chave é validada no boot e **derruba a API** se faltar. |
| **Mercado Pago** (`integrations/payment_gateway.py`) | cobrança pix e confirmação por webhook | Cobrança não sai: 503 `gateway_unavailable` (retentável) ou 503 `payment_unavailable` / 502 `payment_rejected` (não retentáveis). O pedido **continua de pé** em `payment_status="pending"` — não é cancelado. Nenhum pedido online consegue ser aceito enquanto durar. |
| **OpenAI** (`ai/`) | chat do Rapi: embedding + LLM | Só o `/chat` quebra. Pedido, cardápio, painel e impressão seguem intactos. Os embeddings são gerados fora do ciclo HTTP pelo container `reindex`; com a OpenAI fora, a fila de produtos atrasados cresce, o painel não sente nada e o chat serve o cardápio anterior. |
| **Resend** (`services/email_service.py`) | código de verificação e de reset de senha | Cadastro novo e recuperação de senha param. Login de quem já tem conta verificada continua. Sem `RESEND_API_KEY` a rota responde 500. |
| **Supabase Storage** (`integrations/supabase_storage_client.py`) | upload de imagem de produto no painel | Só o upload responde 503. Leitura das imagens já enviadas continua — o bucket é público. |
| **Redis** (opcional) | cache de estimativa e contador de rate limit | Sem `REDIS_URL` os dois caem para memória do processo. Funciona com 1 worker; com N, o rate limit efetivo vira N × o configurado e o cache do Maps é perdido a cada deploy. Avisado no boot. |

---

## 7. O chat do Rapi, em resumo

`POST /chat` → `services/chat_service.py`. Valida o restaurante **e a
filial** antes de gastar token, gera o embedding da pergunta, faz busca
vetorial no pgvector filtrada por `branch_id` e produto ativo, chama o LLM com
structured output.

`branch_id` é obrigatório no corpo desde a revisão `20260820_0026`, e não cai
para a filial padrão: o cardápio é por loja, e recomendar **com preço** um
produto que aquela loja não vende é o defeito que ele existe para fechar. Ver
`docs/cardapio-por-filial.md`.

Duas defesas que importam: ids que o LLM inventou são descartados
(`_validate_selected_product_ids`), e os produtos são recarregados do banco pelo
id (`_hydrate_products`) — **preço e nome que chegam ao cliente vêm do banco,
nunca do que o LLM escreveu.**

**O `restaurant_context` do prompt** sai da linha de `restaurants` que o
`chat()` já carregou para validar: nome e, quando preenchida, a `description`.
Era `restaurant_id=<uuid>`, e o assistente não tinha como saber onde trabalhava.
Não existe campo de tipo de cozinha no cadastro — `description` é o mais
próximo, e é texto do lojista, então entra cortado e com as quebras de linha
achatadas (`_build_restaurant_context`).

**O preço que o modelo vê** é carimbado por requisição em
`RetrievalService._with_current_prices`, da linha viva de `products`, e de
propósito **fora** do cache de busca: preço servido de um cache de 20 minutos
faria toda alteração divergir do cartão por até 20 minutos, na mesma resposta. A
mesma consulta aplica `is_active`/`is_available`, então o que chega ao modelo é
sempre um produto que a hidratação consegue transformar em cartão. Sobra a
janela da chamada ao modelo (~1s), que `_log_price_divergence` denuncia no log
em vez de corrigir — o cartão já está certo.

**Produto citado no texto vira cartão.** Se o modelo nomeia produtos e devolve
`selected_product_ids` vazio, `_rescue_products_named_in_text` casa os nomes do
texto com os da busca e preenche a seleção. Só age com a seleção vazia, e só
alcança produto que a nossa busca devolveu — é a direção oposta de
`_validate_selected_product_ids`, que continua sendo quem barra id inventado.

**O índice se mantém sozinho** desde o container `reindex`
(`scripts/reindex_worker.py`): a cada minuto ele compara
`ai_product_embeddings.updated_at` com o `GREATEST(products.updated_at,
categories.updated_at)` e põe em dia quem estiver atrás. Não há fila nem gancho
no painel — `products.updated_at` é mantido pelo TRIGGER
`trg_products_updated_at`, dentro do banco, então edição por SQL manual ou
script de importação entra na varredura do mesmo jeito. Produto novo aparece no
chat em até um minuto; antes disto, só depois de alguém lembrar de rodar
`reindex_ai.py` à mão.

Duas propriedades do worker que não são detalhe de implementação: ele grava em
`ai_product_embeddings.updated_at` **a versão do produto que indexou**, e não a
hora em que indexou (senão uma edição salva durante a geração do vetor se
perderia em silêncio); e quando o texto indexado não mudou, ele só carimba a
linha, sem chamar a OpenAI — é o que faz "acabou o X" não custar um embedding.

O histórico da sessão é um dicionário no processo (1h, 20 mensagens): com mais de
um worker o Rapi "esquece" a conversa. Não há caminho de Redis para ele.

---

## 8. Os outros documentos

| Arquivo | Assunto |
|---|---|
| [modelo-de-dados.md](modelo-de-dados.md) | tabelas, o que cada uma guarda, como se ligam, isolamento entre restaurantes |
| [pagamentos-e-comissao.md](pagamentos-e-comissao.md) | fluxo de cobrança, Mercado Pago, credencial por restaurante, comissão, o que ficou de fora |
| [entrega-e-horarios.md](entrega-e-horarios.md) | estimativa, taxa por km, horário de funcionamento, reaproveitamento de estimativa |
| [autenticacao-e-escopo.md](autenticacao-e-escopo.md) | token de cliente, token de lojista, escopo por restaurante e por filial, o par do entregador |
| [entregadores.md](entregadores.md) | cadastro, link + código, atribuição, a taxa que a loja paga por corrida, a tela do motoboy, o que ficou para a fase 2 |
| [impressao.md](impressao.md) | setores, montagem das comandas, o print-agent |
| [whatsapp.md](whatsapp.md) | o número por filial, o webhook único, os quatro avisos de status, o reenvio do que falhou, e o que é ignorado de propósito |
| [avaliacao-de-pedido.md](avaliacao-de-pedido.md) | nota do cliente pelo link de acompanhamento, aba do painel, LGPD do texto livre, e por que o QR na comanda ficou para depois |
| [custo-de-ia.md](custo-de-ia.md) | quanto o assistente custa por restaurante: o que é gravado por chamada, a rota de leitura, e por que ela não é do painel |
| [operacao.md](operacao.md) | subir local, deploy, migrações, logs, o que fazer quando não sobe |
| [onboarding-de-restaurante.md](onboarding-de-restaurante.md) | pôr um restaurante novo no ar, do zero ao primeiro pedido pago e impresso; os cinco passos que quebram em silêncio; os fluxos em linguagem de lojista |

Armadilhas de quem escreve código estão em
`.claude/skills/rapidex-backend/SKILL.md`.
