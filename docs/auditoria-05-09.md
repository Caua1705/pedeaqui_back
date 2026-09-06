# Auditoria do backend — 05/09/2026

Rodada de **levantamento**. Nada aqui foi consertado: cada achado traz tamanho
(P/M/G), risco (alto/médio/baixo) e o que quebra se ficar como está. A decisão
do que entra é de quem lê, depois do dia 15.

**Estado em que a auditoria rodou**, porque nenhuma medição vale sem isto:

| | |
|---|---|
| Produção (`/health`) | `f5b6a93` |
| `main` local | `f5b6a93` — **os dois batem**, o que está medido aqui é o que está no ar |
| Suíte rápida | `3019 passed, 254 subtests passed` em 77 s |
| Suíte total | 3928 casos coletados (909 são `db`) |
| Schema medido | baseline + as 56 revisões, aplicado no Postgres 17 de teste |
| Tabelas | 49 (+ `alembic_version`) |
| Rotas no `openapi.json` | 131 caminhos, 162 operações, 260 schemas — documento **em dia** com o código |

**O que NÃO deu para medir daqui, e por quê.** Consulta ao banco de produção é
recusada pelo classificador de permissão desta máquina. Isso deixa quatro
perguntas do enunciado sem resposta minha: **quantas linhas cada tabela tem**,
**quais colunas são sempre NULL em produção**, **quais índices nunca são
usados** e **as dez consultas mais caras**. As quatro estão respondidas em SQL
pronto no §8 — cola e roda, e cada bloco diz o que fazer com o resultado. Não
inventei número nenhum no lugar delas.

---

## As cinco que eu faria primeiro

1. **O lock que falta em `OrderStatusChangeService.apply`** (§1.1). É o único
   achado desta rodada que **custa dinheiro sozinho**, e ele já foi levantado na
   rodada anterior sem virar código. Um pedido em `ready` que receba "concluir"
   do lojista e "cancelar" ao mesmo tempo roda **os dois** conjuntos de efeito:
   credita cashback **e** estorna o cupom. Tamanho P (um arquivo).
2. **Dimensionar o pool do banco** (§1.2). Medido: 5 + 10 = **15 conexões**, 40
   threads servindo, 30 s de espera antes de morrer. E o `create_order` segura
   uma dessas conexões durante a chamada ao Google (5 s de teto). Tamanho P
   (uma linha em `src/db/session.py`), risco alto no dia 15.
3. **Fechar as 6 cópias do recorte de filial** (§3.1). `_get_branch` está
   copiado byte a byte em cinco services, e a sexta cópia **já divergiu** — usa
   `get_by_id_and_restaurant` em vez de `get_active_...`. É a checagem que
   impede o restaurante A de alcançar a filial do B. Tamanho M.
4. **Rodar o SQL do §8** com a loja aberta, para as quatro perguntas que eu não
   consegui responder daqui. Tamanho P (é colar e rodar), e é o que transforma
   metade desta auditoria em decisão.
5. **A auditoria do VPS e a rotação da chave do Google** (§6.4 e §6.5). A chave
   **não está no histórico deste repositório** — varri os 2.666 blobs, §6.5 —, e
   isso muda o procedimento: procure no repositório certo antes de rotacionar.

---

## 1. Dívida que morde no dia 15

Só entra aqui o que precisa de **duas coisas ao mesmo tempo** para aparecer.
Nada disto se reproduz com um pedido por vez.

### 1.1 `apply` lê, valida e escreve o status SEM lock — G/alto

**Onde:** `src/services/order_status_change_service.py`. Não há
`with_for_update` sobre `orders` em lugar nenhum de `src/` — conferido: os três
`with_for_update` do repositório são de **cupom** (`coupon_repository:72,90`) e
de **cliente** (`customer_repository:38`).

**O cenário, e ele não é teórico numa loja com entregador:**

```
pedido em `ready`
  lojista clica "concluir"           lê ready -> valida ready->completed        OK
  entregador clica "saí p/ entrega"  lê ready -> valida ready->out_for_delivery OK
  os dois escrevem. o último ganha.
```

O status errado é o menor problema. **O que custa dinheiro é o efeito
colateral**, porque `apply` faz mais do que gravar:

| Destino | Efeito colateral |
|---|---|
| `completed` | credita cashback |
| `cancelled`/`rejected` | estorna cupom **e** devolve o cashback resgatado |

Uma corrida `completed` × `cancelled` a partir de `ready` — as duas transições
são válidas — roda **os dois** conjuntos: o cliente recebe o crédito **e** o
cupom volta, num pedido que terminou cancelado e portanto fora do faturamento.

**O que já protege, e até onde:** o crédito é idempotente por
`cashback:earn:{order.id}`, então não sai em dobro. Isso **limita** o estrago;
não o elimina — o par crédito-mais-estorno continua acontecendo.

**Quatro portas chegam nesse escritor** (armadilha 25.1), e a quinta é a
varredura de pagamento não concluído. Ela relê o pedido dentro de `_cancel_one`
antes de cancelar, e o docstring diz que é a releitura que barra o cliente que
voltou e pagou. **Ela estreita a janela; não a fecha** — sem lock, o pagamento
entra entre a releitura e a escrita.

**O que quebra se ficar:** no dia 15, com entregador e lojista mexendo no mesmo
pedido, cashback creditado em pedido cancelado e cupom devolvido a quem não
comprou. Ninguém reclama de desconto que recebeu.

**Já levantado em `scratchpad/rodada-back-9.md`, item 7, e não virou código.**
Este é o segundo registro.

### 1.2 O pool do banco é o padrão do SQLAlchemy, e ele é pequeno — P/alto

**Medido**, não estimado, contra o engine real
(`create_engine(settings.DATABASE_URL, pool_pre_ping=True)`, `src/db/session.py`):

| | |
|---|---|
| classe do pool | `QueuePool` |
| `pool_size` | **5** |
| `max_overflow` | **10** |
| teto de conexões | **15 por processo** |
| `pool_timeout` | **30 s** — depois disso a requisição morre com `QueuePool limit ... timed out` |
| `pool_recycle` | −1 (nunca recicla; `pool_pre_ping` cobre a conexão morta) |
| threads servindo | **40** (endpoints são `def`, vão para o threadpool do AnyIO) |
| workers | **1** (`CMD` do Dockerfile, sem `--workers`) |

São **40 threads disputando 15 conexões**. E o `create_order` não é um caminho
curto: a reserva de idempotência faz o `INSERT` (armadilha 8 — mesma transação,
sem commit), e **só depois** vem a chamada paga ao Google Maps. Ou seja, a
conexão fica retida durante a chamada externa:

```
begin() -> INSERT idempotency_keys        <- conexao presa a partir daqui
  _estimate_delivery -> Google Maps        5 s de teto
  _lock_customer_for_cashback              FOR UPDATE em customers
  _resolve_coupon                          FOR UPDATE em restaurant_coupons
commit()                                   <- so aqui ela volta
```

**O Mercado Pago NÃO está nesta lista, e vale dizer por quê.** A cobrança é
outra rota (`POST /{slug}/orders/{token}/payment`), e
`PaymentService.start_online_payment` **commita antes** de falar com o gateway,
com o motivo escrito no lugar — *"com o pool cheio, a API inteira trava
esperando um gateway lento"*. Depois do I/O ele relê o pedido e grava. O
caminho do pagamento já é o certo; quem falta é a estimativa.

**O que quebra se ficar:** uma lentidão do Google deixa de ser "o checkout está
devagar" e vira **a API inteira travada** — o painel, o cardápio e o stream
disputam as mesmas 15 conexões. O sintoma no log é
`QueuePool limit of size 5 overflow 10 reached`, e ele aponta para o banco, que
não é onde está o problema.

**Tamanho P:** `pool_size`/`max_overflow`/`pool_timeout` explícitos numa linha.
Mas o número **depende do teto do Supabase** — dimensionar sem olhar o limite do
plano troca um erro por outro. O SQL para ver quantas conexões o banco aguenta
está no §8.5.

### 1.3 Se o Traefik não mandar `x-real-ip`, o rate limit vira UM balde global — P/alto

`src/api/rate_limit.py:247`: sem o cabeçalho `RATE_LIMIT_CLIENT_IP_HEADER`
(padrão `x-real-ip`), a chave do limite cai em `SHARED_BUCKET_KEY` — **um balde
para a plataforma inteira**. Com `CREATE_ORDER_RATE_LIMIT = "10/minute;60/hour"`,
isso é **60 pedidos por hora no total**, de todos os restaurantes juntos.

Há aviso no log (`[Rate limit] cabecalho %s ausente`), e ele é a única pista.
O Traefik não está neste repositório (a API entra pela rede externa
`n8n_default`), então **isto não é verificável daqui** — o comando de conferência
está no §6.4.

**O que quebra se ficar:** no primeiro pico, 429 em massa no checkout, com o
backend certo e a configuração do proxy errada.

### 1.4 O limite de pedido é por IP, e clientes compartilham IP — P/médio

`CREATE_ORDER_RATE_LIMIT = "10/minute;60/hour"` **por IP de cliente**. Numa
praça de alimentação, num evento, ou atrás do CGNAT de uma operadora, dezenas de
clientes chegam com o mesmo endereço. O 61º pedido daquela hora leva 429.

Não é bug — é a escolha certa contra força bruta. É uma **decisão a reconferir
antes de um dia de movimento atípico**, e a saída rápida existe
(`RATE_LIMIT_ENABLED`, ou o valor da constante).

### 1.5 O que eu procurei e NÃO achei

Vale registrar, para a próxima rodada não refazer:

- **Deadlock entre cupom e cliente: já fechado.** `order_service.py:143` fixa a
  ordem (cliente primeiro, cupom depois) em todo caminho, com o motivo escrito.
- **Migração concorrente entre réplicas: já fechado.**
  `pg_advisory_xact_lock`, `src/db/advisory_lock.py`.
- **Sessão vazando no stream SSE: não acontece.** `SessionLocal()` por poll,
  fechada no `finally` (`admin_order_stream_service.py:178-195`). Um painel
  aberto não segura conexão entre os polls de 4 s.
- **Escrita fora de transação, repositório commitando, endpoint chamando
  repositório:** `scripts/escrita_e_transacao.py` → **0 achados**.

---

## 2. Banco de dados

### 2.1 `customers.cpf` — coluna morta há 24 dias, com o índice ainda de pé — P/baixo

**Ninguém escreve e ninguém lê.** Varredura de todas as 585 colunas do schema
real contra todo o `src/`: `cpf` tem **zero** ocorrências em `src/` como
atributo, kwarg ou literal — nem no model, que a mapeia e nunca a usa.

A revisão `20260812_0019` (12/08/2026, **24 dias**) anulou os valores e disse,
com todas as letras: *"a coluna vazia deve ser derrubada numa revisão próxima,
depois de um ciclo confirmando que nada a lê."* O ciclo passou; a revisão não
foi escrita.

O que sobrou de pé: `idx_customers_cpf_unique`, um índice UNIQUE parcial
(`WHERE cpf IS NOT NULL`) — **vazio**, portanto barato, mas mantido em toda
escrita de `customers`.

**O que quebra se ficar:** nada quebra. O que fica é uma coluna de documento de
identidade no schema de produção, num sistema que fez a frente de LGPD
justamente para não tê-la. Se um dia alguém importar dado por SQL manual, ela
volta a existir sem nenhum código pedindo.

**Tamanho P** — uma revisão com `drop_column` e `drop_index`. Confirme antes com
o §8.2, que responde se ela está mesmo 100% nula em produção.

### 2.2 `coupon_claims.claimed_at` — a única coluna com ZERO leitor no repositório inteiro — P/baixo

Criada na revisão `20260828_0043` (28/08/2026, **8 dias**). É `NOT NULL` com
`server_default now()`, e a tabela **já tem** `created_at`, também `NOT NULL`
com `server_default now()`. As duas gravam o mesmo instante.

Varredura em `src/`, `scripts/`, `tools/` e `tests/`: `claimed_at` aparece
**uma vez**, na definição do model. Nenhuma consulta, nenhum schema, nenhum
relatório.

**O que quebra se ficar:** nada. É uma coluna a mais em toda escrita de resgate.
O docstring do model é explícito sobre o que a tabela deliberadamente **não**
tem ("sem coluna de status e sem coluna de valor, de propósito") — esta escapou
da mesma disciplina.

**Tamanho P.** A decisão real é qual das duas fica; `created_at` é a que o resto
do repositório usa.

### 2.3 `branches` tem 50 colunas e `orders` tem 52 — G/médio, e já tem plano

As duas maiores tabelas do schema em número de colunas. `branches` absorveu, ao
longo das revisões, operação do dia, termo comercial, entrega, tarifa de
entregador, impressão, cashback e canal de WhatsApp.

**Isto já está levantado e com migração escrita** — não é achado novo, é
pendência com dono:

| Preparada | O que faz | Estado |
|---|---|---|
| `20260905_0058` | derruba as **5 colunas de endereço mortas** de `branches` (`address_street`, `address_neighborhood`, `address_city`, `address_state`, `address_zipcode`) | **fazer** — não move dado, não muda comportamento |
| `20260905_0059` | tira as **7 colunas de tarifa** para uma tabela 1:1 opcional | decidir |
| `20260905_0057` | `ai_usage_events.surface` aceita `indexing` | decidir |

As três moram em `alembic/preparadas/`, que o Alembic não lê, e
`tests/test_revisoes_preparadas.py` cobra que continuem invisíveis para o
`upgrade`. Desenho em `docs/modelo-de-dados.md`.

**A `0058` é a que eu tiraria da gaveta primeiro**: cinco colunas mortas, sem
passo de dado, com o roteiro já revisado.

### 2.4 51 índices sem dono no Alembic — M/médio

`scripts/audit_indexes.py` contra o schema real:

| Achado | N | Leitura |
|---|---|---|
| Definição duplicada | 22 | **todos são o falso positivo conhecido** — o mesmo nome repetido uma vez por FK que o referencia. Conferi um a um: **nenhum par tem dois nomes diferentes**, que é o achado de verdade |
| Nome fora da convenção | 51 | herança do schema criado à mão |
| **Sem dono no Alembic** | **51** | nasceram no `schema_baseline.sql`, não em revisão |

Os 51 sem dono são o resíduo da armadilha 33: existem em produção e no baseline,
e **nenhuma revisão os cria**. Um banco montado do zero pelas revisões teria
outro conjunto de índices. Hoje isso não morde porque a suíte `db` aplica o
baseline antes; morde no dia em que alguém precisar recriar o banco sem ele.

**O que quebra se ficar:** nada agora. O custo é que "quais índices este sistema
tem" só se responde olhando o `.sql` congelado, não o histórico.

### 2.5 Índices que faltam — não classificado, e de propósito

A varredura cruzou toda coluna que o código filtra com a coluna **líder** de
cada índice do schema real. **41 colunas** filtradas sem índice que comece por
elas. A maioria é predicado secundário numa consulta já ancorada em
`restaurant_id`/`branch_id` — não precisa de índice, e criar um seria custo de
escrita sem ganho.

**Não classifiquei nenhuma como "falta índice" sem medir**, porque a pergunta
certa é "o planner escolheu seq scan nesta tabela, neste tamanho?", e isso só o
banco de produção responde. O §8.3 e o §8.4 respondem as duas metades: quais
índices nunca foram usados (candidatos a DROP) e quais consultas estão caras
(candidatas a índice novo).

As três que eu olharia primeiro quando o resultado chegar, por serem as que
aparecem em consulta de tela com filtro de período:

- `orders.created_at` — existe em três compostos (`ix_orders_restaurant_created_at`,
  `ix_orders_restaurant_branch_created_at`, `idx_orders_customer_id_created_at`),
  nunca como líder. Provavelmente correto;
- `orders.payment_status` — 4 filtros, nenhum índice. É metade do WHERE do
  faturamento;
- `cashback_transactions.restaurant_id` — 6 filtros, e a tabela só tem índice por
  `customer_id`. O relatório de cashback do painel varre por restaurante.

### 2.6 Tabelas que cresceram desproporcionalmente — **bloqueado**, SQL no §8.1

Sem acesso ao banco de produção não há como responder. O que dá para dizer daqui
é **quais tabelas têm crescimento não limitado por retenção**, que é a pergunta
por trás:

| Tabela | Tem varredura de retenção? | Onde |
|---|---|---|
| `idempotency_keys` | sim, por `expires_at` | `cleanup_idempotency_keys.py`, container `limpeza`, 24h |
| `delivery_estimates` | sim, por `expires_at` | idem |
| `ai_feedback` | sim, por prazo | idem (`feedback_retention_cutoff`) |
| `order_reviews.comment` | sim, só a coluna | idem (`review_retention_cutoff`) |
| `orders`, `order_items`, `order_item_options` | **não** | histórico, cresce para sempre — e é o certo |
| `order_status_history` | **não** | ~4 linhas por pedido |
| `ai_usage_events` | **não** | uma linha por turno de chat/voz |
| `whatsapp_messages` | **não** | uma linha por aviso |
| `cashback_transactions` | **não** | razão contábil |

As três últimas são as que eu vigiaria: crescem com o uso, não com o número de
pedidos, e nenhuma tem prazo escrito. `ai_usage_events` em especial — o
`reindex` roda a cada minuto.

---

## 3. Duplicação real

Só o que faz o conserto pegar em um lugar e não no outro. Similaridade estética
foi descartada: a varredura compara **estrutura de AST** com nomes locais
apagados, sobre as 485 funções de `src/` com 4+ instruções.

### 3.1 `_get_branch` — o recorte de filial escrito SEIS vezes, e duas já divergiram — M/alto

É a checagem que o próprio `scripts/escopo_das_rotas.py` chama de *"o custo de
errar aqui é o maior do sistema: um restaurante lendo pedido, cliente ou
faturamento de outro"*.

Cinco cópias **estruturalmente idênticas (100%)**:

| Arquivo | Assinatura |
|---|---|
| `admin_settings_service.py:561` | `_get_branch(self, scope, branch_id)` |
| `admin_printing_service.py:585` | `_get_branch(self, scope, branch_id)` |
| `admin_courier_service.py:391` | `_get_branch(self, scope, branch_id)` |
| `admin_cashback_service.py:155` | `_get_branch(self, scope, branch_id)` |
| `admin_menu_service.py:550` | `_get_branch(self, **branch_id, scope**)` ← **ordem invertida** |

E a sexta, `admin_whatsapp_service.py:257` `_ensure_branch_in_scope`, com **85%**
— a diferença não é cosmética:

```python
# as cinco
branch = self.branch_repository.get_active_by_id_and_restaurant(branch_id, scope.restaurant_id)
# a sexta
filial = self.branch_repository.get_by_id_and_restaurant(branch_id, scope.restaurant_id)
```

`get_by_id_and_restaurant` devolve a filial **ativa ou não** — está documentado
no repositório e existe por causa da reimpressão de comanda. Então hoje as rotas
de WhatsApp aceitam uma filial desativada e as outras cinco não. Pode estar
certo; **o que não está é isso ser invisível**, decidido por qual cópia alguém
copiou.

**Os dois estragos, e são diferentes:**

- **a ordem invertida em `admin_menu_service`** é uma armadilha de chamada. Os
  dois argumentos são posicionais e nenhum type checker separa `AdminScope` de
  `UUID` num `self._get_branch(a, b)` mal copiado;
- **um conserto de segurança pega em uma cópia e não nas outras cinco.** É
  exatamente a classe que o enunciado pede, no lugar mais caro do sistema.

**O que segura hoje:** `scripts/escopo_das_rotas.py` está **verde** (0 achados
em 102 rotas `/admin` e 5 `/courier`) e segue a cadeia de chamadas por nome,
então uma sexta cópia errada apareceria. Isso é a rede, não o conserto.

**Tamanho M** — uma função só, provavelmente em `AdminScope` ou num mixin, e
seis chamadores. A decisão que não é mecânica: qual das duas leituras de
repositório vira a canônica, e o que acontece com o WhatsApp.

### 3.2 `_normalize_search` e `MAX_SEARCH_LENGTH = 120` — três cópias — P/baixo

`admin_customer_service.py:42/166`, `admin_menu_service.py:81/735` e
`admin_order_service.py:49/335` — constante e função, idênticas nas três.

O risco aqui é menor do que parece, e vale dizer por quê: a parte que **morde**
dessa regra (a normalização NFC do termo, armadilha 31) mora no repositório, em
`_build_customer_search_condition`, `_build_product_search_condition` e
`_build_search_condition` — e **as três fazem NFC**, conferido. O que está
triplicado é o `strip()` e o corte em 120.

**O que quebra se ficar:** mudar o teto de busca em um painel e não nos outros
dois. Barato de errar, barato de consertar.

### 3.3 A regra do frete grátis existe DUAS vezes, e a segunda está no app — M/alto

Esta é a duplicação mais cara desta rodada, e ela **não é visível grepando o
backend**.

`OrderService._apply_free_delivery` (`order_service.py:384`) é a regra:

```python
if not operation.free_delivery_enabled:            return delivery_fee, ZERO
if operation.free_delivery_min_order_value is None: return delivery_fee, ZERO
if delivery_fee <= ZERO:                            return delivery_fee, ZERO
if subtotal < quantize_money(...):                  return delivery_fee, ZERO
return ZERO, delivery_fee
```

Quatro decisões: compara com o **subtotal** (não o total — senão a taxa se
autoconcede), usa **`>=`** (não `>`), não faz nada na retirada, e não faz nada
com a campanha ligada sem valor.

**E a rota de estimativa não consegue aplicá-la.** `DeliveryEstimateRequest` tem
`branch_id`, `address_id` e `address` — **não tem o valor da sacola**. Então
`POST /{slug}/delivery/estimate` devolve `delivery_fee` **sem** o frete grátis,
sempre.

Quem aplica na tela é o app, com os dois campos que `/menu` publica para ele:
`free_delivery_enabled` e `free_delivery_min_order_value`. Ou seja: a regra está
escrita **uma vez em Python e uma vez em JavaScript**, e o backend não tem como
saber se as duas concordam.

**O que quebra se ficar:** um app que compare com o total em vez do subtotal, ou
que use `>` em vez de `>=`, mostra "frete grátis" e o pedido fecha cobrando a
taxa — ou o contrário. O cliente vê um valor na sacola e outro no pedido. Não há
erro, não há log, e a reclamação chega como "o app está errado".

**Não é conserto de backend.** O caminho é uma das três:

1. `DeliveryEstimateRequest` passa a aceitar o subtotal e a resposta já vem com
   a taxa perdoada — muda contrato, e é o único jeito de a regra ter um dono só;
2. `/menu` publica a regra num formato que o app não reimplemente (hoje ele
   publica os *ingredientes*);
3. aceitar as duas cópias e **escrever isso em `docs/contrato-clientes-frontend.md`**,
   com as quatro decisões acima listadas, para o app poder ser conferido contra
   elas. É o mais barato, e hoje não existe.

**A mesma forma vale para `min_order_value` e `service_fee_*`**, pelos mesmos
motivos e com estrago menor (o backend recusa o pedido abaixo do mínimo, então
a divergência vira 400 em vez de valor errado).

### 3.4 O que eu procurei e NÃO achei

- **Regra de faturamento em dois lugares:** consolidada em
  `BILLABLE_ORDER_STATUSES`/`BILLABLE_PAYMENT_STATUSES`, com
  `tests/test_particao_dos_status.py` travando a partição (armadilha 47).
- **Janela do cupom em SQL e em Python:** consolidada em
  `src/services/coupon_window.py`, com as duas formas lado a lado (armadilha 54).
- **Herança de configuração da filial:** um lugar só,
  `resolve_branch_operation` (armadilha 35).

Fora o par `update_restaurant_settings` × `update_branch_settings` (83%), que é
legítimo — são os dois regimes da armadilha 35 —, **não há outro par acima de
72% em `src/`**.

---

## 4. Correções silenciosas

### 4.1 Não há exceção engolida em `src/` — 0 achados

Varredura por AST de **todo** `except` de `src/`: 26 handlers não logam e não
relançam. Li os 26. Nenhum é engolida:

- 21 são guardas de parse (`except ValueError: return None` sobre entrada
  externa) — o `return` **é** o tratamento, e o chamador decide;
- 4 são `except IntegrityError: self.db.rollback()` em `coupon_service`, todos
  seguidos de `_raise_conflict(exc)` ou com o comentário do caso de corrida
  ("duas requisições ao mesmo tempo: a outra ganhou");
- 1 é `except KeyError: pass` em `google_identity_client.py:129`, e a linha
  seguinte explica: `kid` desconhecido é o sintoma da rotação de chave do Google
  e tem tratamento próprio abaixo.

Todo `except Exception` largo em service **loga com `logger.exception` ou
relança**, incluindo os dois que mais importam:
`order_status_change_service.py:189` (falha ao avisar o cliente por WhatsApp) e
`:214` (falha ao estornar pedido cancelado).

**Este item do enunciado está limpo.** O que sobra é o parágrafo seguinte.

### 4.2 Falha externa tem log — não tem métrica e não tem tela — M/médio

O enunciado pede "sem log, **sem métrica e sem tela**". As três falhas externas
que mais custam têm a primeira e nenhuma das outras duas:

| Falha | Log | Métrica | Tela |
|---|---|---|---|
| Mercado Pago recusa/não responde | `[Pagamento][mercadopago] erro pedido=#N` | — | o cliente vê 502 com mensagem traduzida (armadilha 49) |
| Estorno automático falhou | `sem estorno automatico` (grep preservado) | — | **nenhuma** |
| Meta recusou o aviso | `[WhatsApp] desistindo do reenvio pedido_id=... tentativas=N` | — | **nenhuma** |
| Google Maps caiu | queda para `default_delivery_fee` resolvido | — | o cliente vê a taxa padrão, ou é recusado |
| OpenAI/embedding falhou | `[AI cache] ...=true` | — | o Rapi responde pior |

**Não existe endpoint de métrica de saúde de integração.** `/internal/ai-usage` e
`/internal/whatsapp-usage` medem **custo**, não falha — e são de propósito
`include_in_schema=False`, para o público da plataforma.

**O que quebra se ficar:** as duas linhas sem tela dependem de alguém estar
lendo `docker logs` no minuto certo. `sem estorno automatico` é dinheiro de
cliente parado na conta do restaurante; a linha do WhatsApp é o cliente sem
saber que o pedido saiu. No dia 15, ninguém vai estar lendo log.

**Tamanho M** e é decisão de produto antes de código: um contador exposto em
`/internal/` (barato, e já há chave e público para ele) ou um alerta em cima do
grep que já existe. O grep `sem estorno automatico` foi mantido palavra por
palavra justamente para isso — e não há nada em cima dele.

### 4.3 `[Delivery estimate debug]` roda em INFO, em produção, no caminho quente — P/baixo

`delivery_estimate_service.py` emite **cinco** linhas `logger.info` com prefixo
`[Delivery estimate debug]` por estimativa: `event=start`,
`event=restaurant_branch_resolved`, `event=address_resolved`,
`event=prep_time_resolved` e o resultado final. É uma das rotas mais chamadas do
app (toda abertura de sacola).

O conteúdo está **certo**, e vale registrar porque é bom: a coordenada do
cliente é deliberadamente omitida, com o motivo escrito na linha de cima (*"quem
tivesse o log do container reconstruiria a casa de todo mundo que pediu
entrega"*), e o que sai é bairro/cidade/estado.

**O que quebra se ficar:** nada quebra. O nome diz "debug" e o nível é `info` —
alguém vai desligar isso um dia sem saber que era intencional, ou vai deixar
para sempre. Uma decisão de duas linhas: ou vira `logger.debug` e o prefixo sai,
ou o prefixo deixa de dizer "debug".

### 4.4 O que a voz loga em claro, e por quê — P/baixo

`src/ai/voice/search_service.py:359` grava a **consulta falada do cliente** em
claro (`consulta=%.120s`). Isso é deliberado e necessário — armadilhas 44 e 45:
o argumento da tool call é o que separa "o modelo inventou" de "o dado veio
errado", e sem ele os dois casos são indistinguíveis.

O que vale anotar: é **texto de pessoa**, da mesma natureza de
`ai_feedback.user_message` — que tem prazo de retenção porque é o mecanismo de
exclusão dela (armadilha 38). **O log do container não tem prazo nenhum.** Não é
achado de bug; é uma linha que falta no inventário de `docs/lgpd-proposta.md`,
que hoje também não menciona o Redis.

---

## 5. Peso

### 5.1 Não há N+1 nas 26 rotas de leitura medidas — 0 achados

**Medido, não estimado.** App inteiro de pé (`TestClient`) contra o Postgres 17
com o schema real, contando o SQL emitido por um listener de
`before_cursor_execute`. Duas execuções com volumes diferentes — 5 produtos/3
pedidos e 20 produtos/12 pedidos. **Consulta que cresce com o volume é N+1.**

| Rota | Consultas (5p/3ped) | Consultas (20p/12ped) |
|---|---|---|
| `GET /{slug}/menu` | 13 | **13** |
| `GET /{slug}/info` | 4 | **4** |
| `GET /{slug}/coupons` | 3 | **3** |
| `GET /admin/products` | 3 | **3** |
| `GET /admin/orders` | 3 | **3** |
| `GET /admin/orders/{id}` | 4 | **4** |
| `GET /admin/orders/status-counts` | 1 | **1** |
| `GET /admin/branches` | 1 | **1** |
| `GET /admin/branches/operation` | 5 | **5** |
| `GET /admin/customers` | 3 | **3** |
| `GET /admin/reviews` | 3 | **3** |
| `GET /admin/couriers` / `coupons` / `users` | 1 | **1** |
| `GET /customers/me/orders` | 3 | **3** |
| **os 11 `/admin/reports/*`** | 1 a 7 | **iguais** |

**Nenhuma cresceu.** O carregamento é `selectinload` em todo lugar que importa:
`/admin/orders/{id}` faz 4 consultas fixas (pedido, histórico, itens, opções dos
itens) para qualquer número de itens, e `/menu` faz 3 (categorias/produtos,
grupos, opções) para qualquer cardápio.

`/admin/reports/customers` é a mais pesada em consultas (**7**), e as 7 são
fixas.

### 5.2 Duas consultas repetidas dentro do próprio `/menu` — P/baixo

Das 13 do cardápio, **duas são repetição literal na mesma requisição**:

- `branches` lido 2×: `menu_repository.get_active_branches(restaurant.id)` (a
  lista) e `branch_repository.get_active_by_id_and_restaurant(...)` (a filial
  escolhida, **que já está na lista**);
- `restaurant_banners` lido 2×: `get_banners_by_type(id, "hero")` e
  `get_banners_by_type(id, "highlight")` — um `WHERE banner_type IN (...)`
  resolve as duas.

**O que quebra se ficar:** nada. São duas idas ao Supabase por abertura de
cardápio, na rota pública mais chamada do sistema. **Tamanho P**, ganho pequeno
e certo.

### 5.3 As dez consultas mais caras e as rotas mais lentas — **bloqueado**

Não dá para responder daqui, e não vou estimar. Os tempos que medi (17–101 ms)
são contra um banco local vazio e **não dizem nada** sobre produção: o custo por
consulta cresce com o dado, e é justamente isso que a contagem constante de
consultas não captura.

`pg_stat_statements` responde a primeira metade (§8.4) e o log de acesso do
Traefik responde a segunda (§8.6). As duas juntas dizem se alguma das 41 colunas
filtradas sem índice (§2.5) precisa de um.

---

## 6. Segurança

### 6.1 Recorte de papel e de escopo — 0 achados

`scripts/escopo_das_rotas.py`, contra o app real:

```
102 rotas /admin  |  45 recebem branch_id do cliente  |  5 rotas /courier  |  0 achados
```

As sete perguntas dele estão todas em zero, incluindo as duas que custam mais:

- nenhuma rota `/admin` aceita `restaurant_id` do cliente;
- nenhuma rota com `branch_id` do cliente deixa de conferir **as duas
  dimensões** (`ensure_branch_allowed` **e** o filtro de restaurante no
  repositório) — a que falta é a que deixa o dono de A alcançar a filial de B,
  porque para o dono `ensure_branch_allowed` retorna na primeira linha;
- nenhuma rota `/courier` deixa de recortar por `courier.id`;
- **nenhuma cadeia de chamadas ficou por seguir** — o script reporta como achado
  o que ele não consegue resolver, e a lista está vazia.

O risco residual é o do §3.1: a checagem está certa em seis lugares e é a mesma
seis vezes.

### 6.2 Dado pessoal em log — limpo, com uma anotação

Varredura por AST de todo argumento interpolado em chamada de `logger.*` de
`src/`, contra um dicionário de nomes de campo pessoal, descontando as defesas
conhecidas (digest, hash, `[email]`, `len()`, id).

**19 sinalizados, nenhum é vazamento.** As três que mereciam olhar:

- a coordenada do cliente **não** é logada na estimativa — está escrito por que
  não, e a decisão é a certa;
- `neighborhood`/`city`/`state` são logados, e o comentário justifica: é o
  recorte que a regra usa;
- a consulta falada da voz é logada em claro, de propósito (§4.4).

`scripts/dados_pessoais_em_chave.py`: **0 chaves de Redis com dado pessoal em
claro** (9 montagens de string revisadas). O `v2` da chave da estimativa fechou
o caso de 03/09.

### 6.3 As outras varreduras, todas verdes

Rodadas nesta auditoria contra o código em produção:

| Varredura | Resultado |
|---|---|
| `escrita_e_transacao.py` | 0 achados |
| `filtros_por_exclusao.py` | 0 achados, 34 sítios revisados, 86 conjuntos fechados |
| `dubles_de_dado.py` | 0 — todo `SimpleNamespace` da suíte dubla colaborador |
| `dados_pessoais_em_chave.py` | 0 com dado pessoal |
| `testes_mudos.py` | 0 — nenhum teste que o pytest não colete |
| `alcance_da_anonimizacao.py` | 0 — as 13 tabelas com `customer_id` estão declaradas |
| `export_openapi.py --check` | `openapi.json` em dia com o código |

### 6.4 Auditoria do VPS — **não verificável daqui**

Não tenho acesso ao servidor, e o Traefik não está neste repositório (a API entra
pela rede externa `n8n_default`). O que dá para dizer do repositório é o que
**deveria** estar valendo:

- o compose **não publica porta nenhuma** — o roteamento é por label do Traefik;
- o container roda como uid 10001, com `/app` somente-leitura;
- o Redis **não publica porta** e exige `REDIS_PASSWORD`, porque `n8n_default`
  é uma rede compartilhada (o n8n mora nela).

**A lista de conferência, para rodar no servidor.** Cada linha responde uma
pergunta, e a coluna da direita é o que você quer ver:

```bash
# 1. o que está escutando na máquina, e em qual interface
sudo ss -tulpn                          # 22, 80, 443 em 0.0.0.0; NADA de 5432/6379/8000
sudo ufw status verbose                 # ativo, default deny incoming
sudo iptables -L DOCKER-USER -n         # o Docker fura o ufw: esta cadeia é quem manda

# 2. SSH — a configuração EFETIVA, não o que está no arquivo
sudo sshd -T | grep -Ei 'permitrootlogin|passwordauthentication|pubkeyauthentication|^port|allowusers|maxauthtries'
#   permitrootlogin no  |  passwordauthentication no  |  pubkeyauthentication yes
sudo lastb | head -30                   # quantas tentativas falhas, e de onde
sudo fail2ban-client status sshd 2>/dev/null || echo "fail2ban NAO instalado"

# 3. o que os containers expõem para fora
docker ps --format '{{.Names}}\t{{.Ports}}'   # só o Traefik com 80/443

# 4. Redis — a senha vale, e a persistência está mesmo desligada
docker exec pedeaqui-redis redis-cli -a "$REDIS_PASSWORD" ping           # PONG
docker exec pedeaqui-redis redis-cli -a "$REDIS_PASSWORD" info persistence | grep -E 'rdb_bgsave_in_progress|aof_enabled|rdb_last_save_time'
docker exec pedeaqui-redis redis-cli -a "$REDIS_PASSWORD" info stats | grep evicted_keys   # tem que ser 0

# 5. o cabeçalho de que o rate limit depende (§1.3) — o achado mais provável desta lista
docker logs pedeaqui-api 2>&1 | grep -c 'cabecalho x-real-ip ausente'    # tem que ser 0

# 6. atualizações
sudo apt list --upgradable 2>/dev/null | head -20
systemctl is-enabled unattended-upgrades 2>/dev/null || echo "sem atualizacao automatica"

# 7. o que o .env do servidor tem, sem imprimir valor
docker exec pedeaqui-api sh -c 'env | cut -d= -f1 | sort' | grep -E 'REDIS_URL|ADMIN_AUTH_SECRET|PAYMENT_CREDENTIALS_ENCRYPTION_KEY|PLATFORM_METRICS_KEY'
```

O item **5** é o que eu rodaria primeiro: é o único da lista que já é uma falha
em produção se der diferente de zero, e é o §1.3.

### 6.5 A chave do Google — **ela não está no histórico DESTE repositório**

Varri o repositório inteiro por objeto, não por commit — os **2.666 blobs** de
todas as refs, incluindo arquivos apagados e branches que nunca entraram na
`main`:

```bash
git cat-file --batch-all-objects --batch-check='%(objecttype) %(objectname)' \
  | awk '$1=="blob"{print $2}' | git cat-file --batch \
  | grep -aoE 'AIza[0-9A-Za-z_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|APP_USR-[0-9a-zA-Z_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
```

**Zero resultados.** Nenhuma chave do Google, nenhuma da OpenAI, nenhuma do
Mercado Pago, nenhuma chave privada. E `.env` **nunca** foi rastreado (`git log
--diff-filter=A -- .env` é vazio); o `.gitignore` o cobre desde cedo, junto com
`.env.*` e a exceção do `.env.example`.

**Isso muda o procedimento, e é por isso que está aqui e não em "rotacione".**
Antes de rotacionar, é preciso saber **onde** ela está, porque é isso que decide
se rotacionar resolve:

- se está no repositório do **painel** ou do **app** (o caso mais provável — uma
  chave de Maps costuma vazar pelo front, onde ela é usada no navegador), a
  varredura acima roda igual lá;
- se está num repositório **público**, ela já foi indexada e a única resposta é
  **apagar a chave no Google**, não desabilitar;
- se está no histórico de um clone local que nunca subiu, o risco é outro.

**O procedimento de rotação** — para executar quando souber qual chave é:

1. **Crie uma chave NOVA.** Google Cloud Console → APIs & Services →
   Credentials → Create credentials → API key. **Nunca edite a exposta**: editar
   restrições numa chave vazada não a invalida, e você perde a capacidade de ver
   se ainda tem tráfego nela.
2. **Restrinja a nova, antes de usá-la.**
   *Application restrictions* → **IP addresses** → o IP do VPS (uma chave de
   backend nunca precisa de referrer). *API restrictions* → **Routes API** e só.
   Sem isso, a próxima cópia da chave vale para toda a conta de faturamento.
3. **Troque no servidor.** `GOOGLE_MAPS_ROUTES_API_KEY` no `.env` do VPS, e
   `docker compose up -d pedeaqui-api`. Não precisa de rebuild — é `env_file`.
4. **Confirme que a nova está servindo**, antes de mexer na velha:
   ```bash
   curl -s https://api.pederapidex.com/health     # o git_sha não muda: é só env
   docker logs --since 5m pedeaqui-api 2>&1 | grep '\[Delivery estimate debug\]'
   ```
   e faça uma estimativa de verdade pelo app. Se a chave nova estiver errada, a
   taxa cai para o `default_delivery_fee` resolvido — e **`0` não é fallback**
   (armadilha 11), então filial sem padrão passa a **recusar toda entrega**. É a
   falha que este passo existe para pegar.
5. **Observe a chave velha por 24–48h.** Console → a chave → Metrics. Enquanto
   houver tráfego, alguma coisa ainda a usa (o app? o painel? um script?), e
   apagá-la derruba essa coisa.
6. **Apague a velha** — Delete, não Disable. Desabilitada, ela pode ser
   reabilitada por quem tiver acesso ao projeto; apagada, o valor vazado deixa
   de existir. **Este é o único passo que fecha o vazamento**; os cinco
   anteriores só evitam derrubar a operação no caminho.
7. **Trave o custo**, porque rotação não impede a próxima: Billing → Budgets &
   alerts, com alerta em 50%/90%/100% do que você espera gastar por mês na Routes
   API.
8. **A chave sai do histórico?** Não sem reescrever a história (`git filter-repo`
   ou BFG) e forçar push — o que quebra todo clone existente e **não apaga nada
   dos forks nem do cache do GitHub**. Para uma chave que já pode ser apagada no
   Google, reescrever o histórico é custo alto por benefício zero. O passo 6 é a
   resposta.

---

## 7. Código morto

### 7.1 Não há função, classe ou módulo sem consumidor em `src/`

Varredura por AST de toda `def` e `class` de `src/` (excluindo rotas, que têm o
decorador como consumidor), contando menções por nome em `src/`, `scripts/`,
`tools/`, `tests/`, `alembic/` e `main.py`.

**40 sinalizados. Li os 40: nenhum é morto.** São 39 validators do Pydantic
(chamados pelo `@field_validator`/`@model_validator`, nunca pelo nome) e o
`get_col_spec` do tipo `Vector` (gancho do SQLAlchemy) e dois helpers privados
chamados por decorador.

Zero módulos órfãos, zero schemas sem uso.

### 7.2 23 rotas que nenhum teste exercita ponta a ponta — M/médio

Cruzei os 131 caminhos do `openapi.json` com a suíte inteira. **23 não são
citados por nenhum teste** — nem literalmente nem por f-string. Confirmei uma a
uma por grep.

O grupo que preocupa é o de **conta e dinheiro do cliente**:

```
POST   /auth/register                       POST   /auth/verify-email-code
POST   /auth/resend-email-code               POST   /auth/reset-password
POST   /auth/verify-reset-code               POST   /auth/forgot-password
POST   /auth/google/nonce                    POST   /auth/google/complete-signup
PATCH  /customers/me/password                POST   /customers/me/delete-code
GET    /customers/me/social                  POST   /customers/me/social/google
DELETE /customers/me/social/{provider}       PATCH  /customers/me/addresses/{id}/default
GET,POST /customers/me/cards                 DELETE /customers/me/cards/{card_id}
GET    /customers/me/export
POST   /restaurants/{slug}/coupons/claim     POST   /restaurants/{slug}/coupons/preview
GET    /restaurants/{slug}/coupons           PUT    .../orders/track/{token}/review
GET    /restaurants/{slug}/payment-config    POST   /chat/feedback
```

**Isto não quer dizer "sem teste".** Boa parte da regra dessas rotas é testada no
service, e a suíte tem 3.928 casos. O que falta é a camada de cima: o
`response_model`, o `Depends` de autenticação, o rate limit e o
`@limiter.limit` que exige `request` na assinatura.

**O que quebra se ficar:** um `response_model` que não valida (armadilha 50: sete
colunas nuláveis derrubam schema de resposta com 500), um `Depends` trocado, ou o
`request` faltando numa rota limitada — todos passam pela suíte e quebram na
primeira chamada. `/customers/me/cards` é o pior caso da lista: cartão salvo,
sem cobertura de rota.

**Tamanho M**, e não é "escrever 23 testes": é um teste de fumaça por rota, do
tipo que `test_new_route_contracts.py` já faz para outras.

### 7.3 "Rotas que o painel e o app nunca chamam" — **não respondível daqui**

Esta pergunta não tem resposta dentro deste repositório, e eu não vou fingir que
tem. Tentei o proxy que existia — cruzar as rotas com os documentos de contrato —
e ele não serve: os três `docs/contrato-*.md` cobrem **cliente**, **filiais** e
**voz**, e **não existe um contrato do painel**. Das 131 rotas, 125 não são
citadas por contrato nenhum, e 102 delas são `/admin` — o número mede a ausência
do documento, não o desuso da rota.

**Quem responde é o log de acesso**, e o comando está no §8.6. Rota com zero
chamada em 30 dias é candidata; rota com zero chamada em 7 dias pode ser
sazonal.

**Vale registrar que a ausência do contrato do painel é ela mesma um achado** —
tamanho M, risco médio. O painel consome o `/openapi.json` (armadilha 16), então
renomear campo ou rota o quebra junto; e hoje não há documento dizendo **quais**
das 102 rotas ele usa. Sem isso, nenhuma rota `/admin` pode ser removida com
segurança, nunca.

---

## 8. O SQL das quatro perguntas que não deram para responder daqui

Roda no banco de produção (pelo `!` na linha, ou
`docker exec pedeaqui-api python -c ...`, ou o editor do Supabase). **Tudo aqui é
leitura.**

### 8.1 Quantas linhas cada tabela tem, e qual cresceu — §2.6

```sql
-- Rode ANALYZE antes se o autovacuum estiver atrasado; sem ele os numeros
-- abaixo sao a ultima estimativa, nao a de agora.
SELECT relname                                        AS tabela,
       n_live_tup                                     AS linhas_aprox,
       pg_size_pretty(pg_total_relation_size(relid))  AS tamanho_total,
       pg_size_pretty(pg_indexes_size(relid))         AS so_indices,
       n_tup_ins                                      AS inserts_desde_o_reset,
       n_tup_upd                                      AS updates,
       n_tup_del                                      AS deletes,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(relid) DESC;
```

**Como ler.** `n_tup_ins = 0` numa tabela é a resposta de "ninguém escreve" —
combinado com o §2.1/§2.2, fecha o caso das duas colunas mortas. `so_indices`
maior que a tabela é sinal de índice sobrando (cruze com o §8.3).

E o ritmo de crescimento das seis que não têm retenção (§2.6):

```sql
SELECT 'orders' AS tabela, date_trunc('day', created_at)::date AS dia, count(*)
FROM orders WHERE created_at > now() - interval '30 days' GROUP BY 1,2
UNION ALL
SELECT 'ai_usage_events', date_trunc('day', created_at)::date, count(*)
FROM ai_usage_events WHERE created_at > now() - interval '30 days' GROUP BY 1,2
UNION ALL
SELECT 'whatsapp_messages', date_trunc('day', created_at)::date, count(*)
FROM whatsapp_messages WHERE created_at > now() - interval '30 days' GROUP BY 1,2
ORDER BY 1, 2 DESC;
```

### 8.2 Colunas sempre NULL em produção — §2.1

```sql
ANALYZE;   -- pg_stats e uma amostra; sem isto ela pode ser de semanas atras

SELECT tablename AS tabela, attname AS coluna,
       round(null_frac::numeric, 4) AS fracao_nula,
       n_distinct                    AS valores_distintos
FROM pg_stats
WHERE schemaname = 'public'
  AND null_frac >= 0.999
ORDER BY tablename, attname;
```

**Como ler.** `null_frac = 1` com a coluna aparecendo no §2 desta auditoria
(`customers.cpf`) confirma que a revisão de `drop_column` pode ser escrita.
`null_frac = 1` numa coluna que o código **escreve** é outro achado — significa
que o caminho de escrita nunca roda.

Para conferência exata de uma coluna específica (a amostra do `pg_stats` pode
mentir em tabela pequena):

```sql
SELECT count(*) AS linhas, count(cpf) AS com_cpf FROM customers;
```

### 8.3 Índices que nunca são usados — §2.4, §2.5

```sql
-- PRIMEIRO isto: idx_scan = 0 nao vale nada se as estatisticas foram
-- zeradas ontem. Um deploy nao as zera; um restart do Postgres, sim.
SELECT stats_reset, now() - stats_reset AS janela
FROM pg_stat_database WHERE datname = current_database();

SELECT s.relname                                    AS tabela,
       s.indexrelname                               AS indice,
       s.idx_scan                                   AS varreduras,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS tamanho,
       i.indisunique                                AS e_unique,
       i.indisprimary                               AS e_pk
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.schemaname = 'public'
ORDER BY s.idx_scan ASC, pg_relation_size(s.indexrelid) DESC;
```

**Como ler, e as duas armadilhas.**

- **`idx_scan = 0` num índice UNIQUE não autoriza `DROP`.** Ele pode existir para
  a *constraint*, não para consulta: `uq_delivery_estimates_token`,
  `coupon_redemptions_idempotency_key_unique`,
  `uq_whatsapp_messages_order_kind` e `ux_cashback_transactions_idempotency_key`
  são regras de integridade. Derrubar qualquer um deles abre duplicata — e a
  última é a que impede o cashback de ser lançado duas vezes.
- **A janela importa mais que o número.** Índice que só serve ao relatório mensal
  aparece com `idx_scan` baixo numa janela de uma semana.

O candidato que eu já espero encontrar aqui é `idx_customers_cpf_unique` (§2.1),
que indexa uma coluna 100% nula.

### 8.4 As dez consultas mais caras — §5.3

```sql
-- No Supabase a extensao ja costuma estar ligada. Se nao estiver, ela exige
-- shared_preload_libraries e um restart — nao habilite no meio do movimento.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

SELECT calls,
       round(total_exec_time)::bigint          AS ms_total,
       round(mean_exec_time::numeric, 1)       AS ms_media,
       round(max_exec_time::numeric, 1)        AS ms_pior,
       rows,
       left(regexp_replace(query, '\s+', ' ', 'g'), 200) AS consulta
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY total_exec_time DESC
LIMIT 10;
```

**Como ler.** Ordene por `total_exec_time` (o que consome o banco) e depois por
`mean_exec_time` (o que faz o cliente esperar) — **são listas diferentes**, e a
segunda é a que aparece como lentidão de tela. Uma consulta com `calls` alto e
`mean` baixo é o poll do stream de 4 s e está certa.

### 8.5 Quantas conexões o banco aguenta — §1.2

```sql
SHOW max_connections;
SELECT count(*) AS abertas_agora,
       count(*) FILTER (WHERE state = 'active')       AS ativas,
       count(*) FILTER (WHERE state = 'idle in transaction') AS presas_em_transacao
FROM pg_stat_activity
WHERE datname = current_database();
```

**Como ler.** `presas_em_transacao` > 0 de forma persistente é o §1.2
acontecendo: transação aberta esperando uma chamada externa. E `max_connections`
é o teto de que o `pool_size` novo precisa caber — no pooler do Supabase, o teto
que vale é o do plano, não o do Postgres.

### 8.6 As rotas mais lentas e as que ninguém chama — §5.3, §7.3

Não é SQL: sai do log de acesso do Traefik, que vive fora deste repositório.

```bash
# quais rotas o painel e o app REALMENTE chamam (§7.3)
docker logs pedeaqui-api --since 720h 2>&1 \
  | grep -oE '"(GET|POST|PATCH|PUT|DELETE) [^ ]+' | sort | uniq -c | sort -rn

# se o Traefik estiver com accessLog em JSON, as mais lentas saem assim:
docker logs traefik --since 24h 2>&1 \
  | jq -r 'select(.Duration) | "\(.Duration/1000000) \(.RequestMethod) \(.RequestPath)"' \
  | sort -rn | head -20
```

Cruze o primeiro com a lista de 131 rotas do `openapi.json`: o que não aparecer
em 30 dias é candidato a morto — **depois** de conferir se não é sazonal
(relatório mensal, exclusão de conta) e de o painel confirmar.

---

## 9. Correções aplicadas nesta rodada

**Nenhuma.** A exceção do enunciado (import morto, docstring errada, typo)
não foi acionada: a varredura de símbolos sem consumidor deu zero, e o único
erro de documentação que achei não cabe nela — está registrado abaixo como
achado.

### 9.1 A tabela de serviços de `docs/operacao.md` está desatualizada — P/baixo

`docs/operacao.md`, §"O compose não expõe porta", diz **"São cinco serviços"** e
lista `pedeaqui-api`, `redis`, `limpeza`, `estorno` e `reindex`.

São **seis**. Falta `whatsapp-reenvio` (container `pedeaqui-whatsapp-reenvio`,
cadência de **2 minutos**, roda `reenvia_avisos_de_whatsapp.py`).

E a linha do `estorno` está incompleta: ela diz `estorna_pedidos_cancelados.py`,
e o compose roda **dois** scripts encadeados —
`estorna_pedidos_cancelados.py && cancela_pedidos_sem_pagamento.py`. O segundo é
o da armadilha 48, o que cancela pedido sem pagamento depois de 30 minutos.

**O que quebra se ficar:** o documento diz, uma seção acima, que *"quando um
deles morre, nada na API muda de aparência — o sintoma é indireto"*. Um
container que não está na tabela é um container que ninguém confere. Se o
`whatsapp-reenvio` morrer, o sintoma é aviso de pedido que não chega ao cliente,
e a lista que a pessoa vai consultar às duas da manhã não tem a linha dele.

Não corrigi porque uma tabela de operação não é "typo em comentário", e o
enunciado foi explícito. **Tamanho P**, e eu faria junto com o primeiro item que
entrar.

---

## Resumo, por ordem de risco

| # | Achado | Tam. | Risco | O que quebra |
|---|---|---|---|---|
| 1.1 | `apply` sem lock em `orders` | P | **alto** | cashback creditado **e** cupom estornado no mesmo pedido |
| 1.2 | Pool de 15 conexões, 40 threads, HTTP externo dentro da transação | P | **alto** | lentidão do Google/MP trava a API inteira |
| 3.1 | `_get_branch` em 6 cópias, 2 já divergentes | M | **alto** | conserto de isolamento pega em uma e não nas outras |
| 1.3 | `x-real-ip` ausente ⇒ um balde de rate limit global | P | **alto** | 60 pedidos/hora na plataforma inteira |
| 3.3 | Frete grátis implementado no backend **e** no app | M | **alto** | sacola mostra um valor, pedido cobra outro |
| 7.2 | 23 rotas sem teste de ponta a ponta | M | médio | 500 em `response_model`, `Depends` trocado |
| 4.2 | Falha externa tem log, não tem métrica nem tela | M | médio | dinheiro parado e aviso não entregue sem ninguém saber |
| 7.3 | Não existe contrato do painel | M | médio | nenhuma rota `/admin` pode ser removida com segurança |
| 2.3 | `branches` com 50 colunas | G | médio | já tem 3 migrações preparadas; a `0058` é de graça |
| 1.4 | Limite de pedido por IP, clientes compartilham IP | P | médio | 429 no checkout em evento/praça |
| 2.4 | 51 índices sem dono no Alembic | M | médio | "quais índices existem" só se responde pelo `.sql` |
| 2.5 | 41 colunas filtradas sem índice líder | ? | ? | **não classificado** — depende do §8.3/§8.4 |
| 2.1 | `customers.cpf` morta há 24 dias, com índice | P | baixo | documento de identidade no schema sem motivo |
| 2.2 | `coupon_claims.claimed_at` sem leitor, há 8 dias | P | baixo | coluna a mais em toda escrita de resgate |
| 5.2 | 2 consultas repetidas dentro do `/menu` | P | baixo | 2 idas ao Supabase por abertura de cardápio |
| 3.2 | `_normalize_search` em 3 cópias | P | baixo | teto de busca mudado em um painel só |
| 4.3 | `[Delivery estimate debug]` em INFO no caminho quente | P | baixo | 5 linhas por estimativa, com "debug" no nome |
| 4.4 | Consulta falada em log sem prazo de retenção | P | baixo | texto de pessoa fora do inventário da LGPD |
| 9.1 | `docs/operacao.md` diz 5 serviços; são 6 | P | baixo | container que ninguém confere quando morre |

**Verde, e vale saber que está verde:** recorte de papel e escopo (0 em 102
rotas `/admin`), exceção engolida (0), N+1 (0 em 26 rotas medidas), índice
duplicado de verdade (0), filtro por exclusão (0), dublê de dado (0), dado
pessoal em chave de Redis (0), teste mudo (0), alcance da anonimização (0),
`openapi.json` em dia, suíte rápida 3019 verdes.
