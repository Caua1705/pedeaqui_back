# Fase 3 — Painel do lojista (backend)

Relatório do que a Fase 3 entregou. Complementa `arquitetura.md`, que continua sendo o
documento de navegação e depuração do projeto inteiro.

Branch: `feat/fase-3-painel-backend`.

---

## 1. O que existia, o que mudou

| Rota / assunto | Situação | O que foi feito |
|---|---|---|
| `POST /admin/auth/login`, `GET /admin/auth/me` | existia | **mantido** |
| `GET /admin/restaurants/{slug}/orders` | existia, com slug na URL | **corrigido**: virou `GET /admin/orders`, restaurante sai do token |
| `GET /admin/orders/{id}`, `PATCH /admin/orders/{id}/status` | existia | **estendido**: escopo por filial |
| Cupons admin com `restaurant_id` no path | existia | **corrigido**: `/admin/coupons`, restaurante do token |
| `GET /admin/reports/commission` | existia | **mantido** |
| Filtros, contadores e SSE de pedidos | não existia | **criado** (Bloco A) |
| Cardápio no painel | não existia | **criado** (Bloco B) |
| Configurações de loja e filial | não existia | **criado** (Bloco C) |
| Clientes do restaurante | não existia | **criado** (Bloco D) |
| Escopo por filial | coluna existia, não era aplicada | **criado**: uma dependência (`get_admin_scope`) |

**Nenhuma rota `/admin` recebe restaurante.** Ele sai sempre do JWT. O `restaurant_id` no
path não autorizava nada (era confrontado com o token), mas manter na API a *forma* de um
parâmetro que a rota não pode obedecer é o que cria a chance de a próxima rota obedecê-lo.

A filial aparece no path das rotas de configuração (`/admin/branches/{branch_id}/...`)
porque um restaurante tem várias e o lojista precisa dizer qual está configurando — e é
conferida contra o escopo antes de qualquer leitura ou escrita.

---

## 2. Endpoints

Todos exigem `Authorization: Bearer <token de lojista>`.

Coluna **Papel**: `todos` = owner, manager e attendant, cada um dentro do escopo de filial
dele. Não há restrição por papel além da filial — ver pendências.

### Pedidos (Bloco A)

| Método | Rota | O que faz | Papel |
|---|---|---|---|
| GET | `/admin/orders` | Lista paginada. Filtros: `branch_id`, `status`, `start_date`, `end_date`, `search`, `limit`, `offset` | todos |
| GET | `/admin/orders/status-counts` | Contadores por status para os badges (mesmos filtros, menos `status`) | todos |
| GET | `/admin/orders/{order_id}` | Detalhe do pedido | todos |
| PATCH | `/admin/orders/{order_id}/status` | Muda o status (aceita `Idempotency-Key`) | todos |
| POST | `/admin/orders/stream-ticket` | Credencial de 30s para abrir o stream | todos |
| GET | `/admin/orders/stream` | SSE: pedido novo e mudança de status | todos |

### Cardápio (Bloco B)

| Método | Rota | O que faz | Papel |
|---|---|---|---|
| GET | `/admin/categories` | Lista categorias, ativas e inativas | todos |
| POST | `/admin/categories` | Cria categoria (slug derivado do nome) | todos |
| PATCH | `/admin/categories/reorder` | Grava a nova ordem (lista completa) | todos |
| PATCH | `/admin/categories/{category_id}` | Renomeia / reordena / ativa / desativa | todos |
| GET | `/admin/products` | Lista paginada. Filtros: `category_id`, `search`, `is_active` | todos |
| POST | `/admin/products` | Cria produto | todos |
| GET | `/admin/products/{product_id}` | Detalhe com grupos de opções | todos |
| PATCH | `/admin/products/{product_id}` | Edita nome, descrição, preço, categoria, ordem, ativo | todos |
| PATCH | `/admin/products/{product_id}/availability` | **Esgotado / disponível** (B4) | todos |
| POST | `/admin/products/{product_id}/image` | Upload da foto (multipart, JPEG/PNG/WEBP) | todos |
| GET | `/admin/products/{product_id}/option-groups` | Grupos do produto, com opções | todos |
| POST | `/admin/products/{product_id}/option-groups` | Cria grupo | todos |
| PATCH | `/admin/option-groups/{group_id}` | Edita grupo (mín./máx./obrigatório/ativo) | todos |
| POST | `/admin/option-groups/{group_id}/options` | Cria opção | todos |
| PATCH | `/admin/options/{option_id}` | Edita opção (preço adicional, ordem, ativo) | todos |

Cardápio é do **restaurante**, não da filial: `categories` e `products` só têm
`restaurant_id`. Um manager preso a uma filial edita o cardápio inteiro — igual ao que já
valia para cupons.

**Não existe DELETE.** `order_items` e `order_item_options` apontam para produto, grupo e
opção por FK; apagar quebraria o histórico que o cliente ainda consulta. "Excluir" no painel
é `is_active = false`.

### Configurações (Bloco C)

| Método | Rota | O que faz | Papel |
|---|---|---|---|
| GET | `/admin/settings` | Configurações do restaurante | todos |
| PATCH | `/admin/settings` | Valor mínimo, taxa de serviço, aceita entrega/retirada, prazos | todos |
| PATCH | `/admin/settings/store-status` | **Abre / fecha a loja** (C1) | todos |
| GET | `/admin/branches` | Filiais que o lojista enxerga | todos |
| GET | `/admin/branches/{branch_id}` | Dados de uma filial | escopo |
| PATCH | `/admin/branches/{branch_id}` | Endereço, contato e regras de entrega | escopo |
| GET | `/admin/branches/{branch_id}/business-hours` | Horário da semana | escopo |
| PUT | `/admin/branches/{branch_id}/business-hours` | Substitui a semana inteira | escopo |
| GET | `/admin/branches/{branch_id}/payment-methods` | Formas de pagamento, habilitadas e não | escopo |
| POST | `/admin/branches/{branch_id}/payment-methods` | Habilita uma forma na filial | escopo |
| PATCH | `/admin/payment-methods/{method_id}` | Liga/desliga, rótulo, ordem | escopo |
| DELETE | `/admin/payment-methods/{method_id}` | Remove a forma da filial | escopo |

`platform_commission_percent` **não aparece em nenhum schema**, nem para leitura: é o
percentual que a plataforma cobra, negociado por contrato. Publicá-lo no OpenAPI do painel
o transformaria em campo de tela.

`GET /admin/branches` devolve **uma** filial para quem está preso a uma — o seletor de filial
do painel já vem resolvido sem a tela precisar conhecer a regra de escopo.

### Clientes (Bloco D)

| Método | Rota | O que faz | Papel |
|---|---|---|---|
| GET | `/admin/customers` | Clientes que já pediram neste restaurante. Filtros: `branch_id`, `search` | todos |

Deriva de `orders`, agrupando por `customer_phone_snapshot`. **Nunca lê `customers`**: a conta
do cliente é global da plataforma, e um SELECT no cadastro entregaria ao lojista também quem
nunca pediu na loja dele — inclusive cliente de concorrente na mesma plataforma. Derivando de
`orders`, o isolamento é consequência da própria consulta.

Agrupa por telefone e não por `customer_id` porque pedido de visitante não tem id, e agrupar
por id descartaria justamente esses. O contrato não traz e-mail, CPF nem o id de cadastro.

---

## 3. SSE ou WebSocket

**SSE.** Três razões, nesta ordem:

1. **O painel só precisa receber.** Tudo o que ele envia (aceitar, recusar, mudar status) já
   tem rota REST com idempotência, máquina de estados e histórico. Um canal bidirecional
   seria uma segunda porta de escrita para as mesmas operações, com autenticação e validação
   próprias — mais superfície de ataque para o mesmo resultado.
2. **O backend é síncrono e tem poucos workers.** Endpoint `def` do FastAPI roda no
   threadpool: uma conexão WebSocket segurada por horas prende uma thread durante todo o
   tempo ocioso, e com poucos workers isso derruba a API antes de derrubar o painel. Por isso
   `GET /admin/orders/stream` é `async def` por decisão de arquitetura (a de upload de imagem
   é async só porque `UploadFile.read()` exige `await`); o trabalho de banco
   vai para o threadpool só durante a consulta (`run_in_threadpool`) e a thread fica livre
   entre um poll e outro.
3. **Reconexão vem pronta e a conexão atravessa o proxy como HTTP.** O `EventSource`
   reconecta sozinho e reenvia `Last-Event-ID`; com WebSocket, reconexão e backoff seriam
   código no frontend, e o Traefik precisaria de upgrade configurado.

### Como o cliente que ficou offline não perde pedido

O estado do stream é um **cursor de tempo**, não uma fila em memória. Cada poll pergunta ao
banco "o que aconteceu depois de X" em `orders` e em `order_status_history`. Uma fila em
memória quebraria por dois motivos já conhecidos do projeto (armadilha 9.9): com mais de um
worker, o pedido criado no worker A nunca chegaria ao painel conectado no worker B; e um
deploy no meio do almoço perderia o que estivesse na fila.

- Reconectou → o navegador reenvia `Last-Event-ID` (o instante do último evento) e o servidor
  repete tudo o que aconteceu depois dele.
- Ficou fora **mais de uma hora** (`MAX_REPLAY_SECONDS`) → recebe um evento `sync_required` e
  recarrega a lista. Mais barato para os dois lados que 400 eventos de uma vez.
- Cada poll olha 5s para trás do cursor, porque `created_at` é o instante em que a transação
  **começou**: um pedido de transação demorada pode ficar visível depois de outro com
  `created_at` maior e cairia num intervalo já varrido. O preço dessa folga é evento
  repetido — daí a entrega ser **ao menos uma vez**.
- A conexão se fecha sozinha aos 15 min para não acumular conexão zumbi no worker. O
  `EventSource` reabre com o cursor; para o painel é invisível.

### Como o frontend consome

```js
// 1. Ticket: o EventSource não envia cabeçalho, então o token de 12h não pode
//    ir na URL (log de proxy, Referer, histórico). O ticket vale 30s e só abre stream.
const { ticket } = await api.post('/admin/orders/stream-ticket');

// 2. Abre o stream. O navegador reconecta sozinho e reenvia o Last-Event-ID.
const stream = new EventSource(`${API}/admin/orders/stream?ticket=${ticket}`);

stream.addEventListener('order.created', (e) => applyEvent(JSON.parse(e.data)));
stream.addEventListener('order.status_changed', (e) => applyEvent(JSON.parse(e.data)));
stream.addEventListener('sync_required', () => reloadOrderList());

// 3. Descartar repetido é obrigação do cliente: a entrega é ao menos uma vez.
const applied = new Set();
function applyEvent(event) {
  if (applied.has(event.event_key)) return;
  applied.add(event.event_key);
  upsertOrderRow(event.order);      // mesmo formato de GET /admin/orders
  refreshStatusCounts();            // os badges não vêm no evento
}
```

Três detalhes que economizam depuração:

- **O ticket expira em 30s**, mas só é usado para *abrir* a conexão. Se o `EventSource`
  reconectar depois disso, o navegador reusa a mesma URL e o ticket já venceu: trate `onerror`
  fechando o stream, pedindo um ticket novo e reabrindo.
- **`event.order` tem exatamente o formato de um item de `GET /admin/orders`** — a mesma linha
  da tabela que o painel já desenha.
- **A tela abre com `GET /admin/orders`**, não com o stream: sem `Last-Event-ID`, o stream só
  entrega o que acontecer dali para a frente.

---

## 4. Detalhes que valem saber antes de mexer

- **Upload de imagem**: o tipo sai dos **bytes** (`src/utils/images.py`), nunca do
  `content-type` do multipart — o bucket serve os objetos publicamente, e um HTML aceito como
  `image/png` viraria script no domínio do Storage. SVG é recusado por ser XML com `<script>`
  dentro. O objeto vai para o Storage **antes** do commit: gravando `image_path` primeiro,
  uma falha do Storage deixaria o produto apontando para um arquivo inexistente.
- **Limite de corpo por rota**: 256 KB continua valendo para JSON; rotas terminadas em
  `/image` têm teto próprio (`MAX_IMAGE_UPLOAD_BYTES`, 3 MB). A escolha é pelo **caminho** e
  não pelo content-type, que é escolhido pelo cliente.
- **Variável nova**: `SUPABASE_SERVICE_ROLE_KEY`. Sem ela só o upload para (503) — o bucket é
  público para leitura, então imagem já enviada continua aparecendo. O boot avisa.
- **Dependência nova**: `python-multipart` (exigida pelo FastAPI para ler o formulário).
- **Sem migração nova**: a Fase 3 escreve em tabelas que já existiam. Os índices que as
  consultas do painel pedem entraram em `20260806_0010_indices_do_painel_do_lojista.py`.
- **Reordenar categorias exige a lista completa**: renumerar só uma parte deixaria as de fora
  com `sort_order` repetido e a ordem final passaria a depender do desempate por nome. Um 400
  aqui significa "alguém criou categoria em outra aba, recarregue".
- **Horário é PUT, não PATCH**: a tela é uma grade de sete dias salva de uma vez, e o
  delete+insert acontece na mesma transação. Dia ausente = dia fechado. Faixas sobrepostas são
  recusadas porque `BranchHoursService.find_current_period` devolve a **primeira** que contém
  o agora — com sobreposição, o tempo de preparo do pedido vira sorteio.

---

## 5. O que ficou pendente

| Pendência | Por quê | Impacto hoje |
|---|---|---|
| `restaurant_settings.payment_methods` (JSONB) sem dono | As formas de verdade são `branch_payment_methods`, que é o que o pedido lê. Editar os dois lados criaria duas fontes de verdade | O cardápio público mostra o JSONB antigo; o checkout valida pela filial. Sai quando o cardápio público passar a ler a filial |
| Imagem antiga fica no bucket | Cada upload usa caminho novo por causa do cache do CDN, e apagar na hora deixaria o cardápio sem imagem por minutos | Lixo acumulando; falta uma limpeza por varredura fora do ciclo HTTP |
| Sem rate limit nas rotas `/admin` | O login de lojista já é limitado, que é a porta de força bruta | Um token válido pode martelar o painel |
| Sem auditoria além do status do pedido | `order_status_history` só cobre pedido | Mudança de preço, horário ou forma de pagamento não registra autor |
| Sem gestão de usuários do painel | Fora do escopo desta fase | Criar lojista continua sendo `scripts/create_admin_user.py` |
| Sem restrição por **papel** | O prompt definiu escopo por filial, não capacidade por papel | Attendant escreve o mesmo que owner dentro da filial dele |
| Cardápio e configurações do restaurante não têm dimensão de filial | `categories`, `products` e `restaurant_settings` só têm `restaurant_id` | Manager preso a uma filial edita o cardápio inteiro. Restringir exige coluna de filial nessas tabelas |
| Ordem dos produtos sem rota de arrastar-e-soltar | Só categorias ganharam reordenação em lote | Produto se reordena um a um pelo `sort_order` no PATCH |
| Testes sem banco real | Toda a suíte usa fakes | O `WHERE` de fato filtrar no Postgres, o `DISTINCT ON` e o delete+insert do horário só serão exercitados em teste de integração |
