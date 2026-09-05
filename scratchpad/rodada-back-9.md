# Rodada 9 — 05/09/2026

O que fechou, o que virou PR e o que ficou esperando decisão. Os itens 6, 7 e 9
não geraram código: o levantamento deles é este documento.

**A regra de segurança foi respeitada:** nada nesta rodada chegou perto do hash
do `tracking_token`. As revisões `0016` e `0017` não foram tocadas, lidas para
alteração nem citadas em migração nenhuma. Nenhum item levou até elas.

---

## 6. `float` × `Decimal` — DECIDIDO: não converter

### O estado não era o que parecia

A premissa da tarefa era "congelado esperando o front". **Não está.** A decisão
foi tomada em 05/09/2026 e está escrita em dois lugares: a armadilha 34 da skill
("mantido, com trava. Reavaliar só se aparecer defeito real") e o cabeçalho de
`tests/test_formato_do_dinheiro.py`. O front deixou de ser o bloqueio; o que
segura hoje é a decisão, e a condição de reabertura é estreita de propósito.

Confirmado com o dono nesta rodada: **manter**. Zero arquivo tocado.

### O levantamento, medido e não lembrado

`scripts/formato_do_dinheiro.py`, contra o `/openapi.json` **gerado**:

```
68 campo(s) de dinheiro como NUMERO, 55 como STRING
3 schema(s) misturam os dois no mesmo objeto
```

Os três são `CreateOrderResponse`, `CustomerOrderHistoryItem` e
`OrderDetailResponse`, e a divisão é **idêntica nos três** — é isso que mantém a
conversão sendo uma decisão só, e não três:

| | Campos |
|---|---|
| número | `subtotal`, `delivery_fee`, `service_fee`, `total` |
| string | `coupon_discount_amount`, `cashback_redeemed_amount`, `discount_total` |

O total de string subiu de 47 para 55 desde a medição da skill: são as quatro
rotas de desempenho (`9f89f81`), que nasceram com dinheiro em `Decimal` — do
lado certo da regra, porque relatório de painel é string sempre e nenhuma delas
mistura.

### Tamanho e risco das duas direções

Nenhuma exige migração. As duas mexem no contrato que o app do cliente lê.

| Direção | Tamanho | Risco |
|---|---|---|
| **tudo `float`** | 3 campos × 3 schemas em `order_schema.py` + `customer_schema.py`; apagar `ESQUEMAS_QUE_MISTURAM`; regerar `openapi.json` | `"2.50"` vira `2.5`. Quem faz `parseFloat` continua; quem exibe direto perde as casas |
| **tudo `Decimal`** | 4 campos × 3 schemas, mesmos arquivos | `52.9` vira `"52.90"`. **Quebra qualquer aritmética** que o app faça sobre `total`/`subtotal`/`delivery_fee`/`service_fee` |

Não há terceira via: **JSON não tem número com casa decimal fixa.** Duas casas
só existem como string.

E não dá para converter um schema só — já foi tentado e revertido (`bffca0e`):
o mesmo campo ficou número numa rota e string na outra.

---

## 7. Máquina de estados do pedido — proposta, NÃO implementada

### O que já existe (e é mais do que a frente supunha)

A máquina **está construída**, em `src/services/order_state_machine.py`:

- **grafo explícito** de `orders.status` (8 estados) e de `orders.payment_status`
  (6), com `completed`/`cancelled`/`rejected` terminais;
- **cinco subconjuntos nomeados** — `TERMINAL_ORDER_STATUSES`,
  `KITCHEN_ORDER_STATUSES`, `PREPARED_ORDER_STATUSES`,
  `CUSTOMER_CANCELLABLE_STATUSES`, `COURIER_TRANSITIONS`;
- **cinco guardas** — a transição, o pagamento que libera a cozinha, a
  confirmação do cancelamento, o que o cliente pode e o que o entregador pode;
- **um único escritor de `orders.status`**: `OrderStatusChangeService.apply`, com
  **cinco** portas chegando nele (painel status, painel cancelar, cliente
  cancelar, entregador, e a varredura de pagamento não concluído — esta última
  não está na lista de quatro da armadilha 25.1, e deveria estar).

Nada disso precisa ser feito de novo. O que segue são **três buracos**, em ordem
de custo.

### Buraco 1 — a corrida entre ler o status e escrevê-lo (o único que custa dinheiro)

`apply` faz **ler → validar → escrever sem lock**. Não há `SELECT ... FOR
UPDATE` em `orders` em lugar nenhum do repositório (conferido: os três
`with_for_update` do `src/` são de cupom e de cliente).

O cenário concreto, e ele não é teórico numa loja com entregador:

```
pedido em `ready`
  lojista clica "concluir"          le ready -> valida ready->completed  OK
  entregador clica "saí p/ entrega" le ready -> valida ready->out_for_delivery OK
  os dois escrevem. o último ganha.
```

O status ficar errado já é ruim. **O que custa dinheiro é o efeito colateral**,
porque `apply` faz mais do que gravar status:

| Destino | Efeito colateral |
|---|---|
| `completed` | credita cashback |
| `cancelled`/`rejected` | estorna cupom **e** devolve o cashback resgatado |

Uma corrida `completed` × `cancelled` a partir de `ready` — as duas transições
são válidas — roda **os dois** conjuntos de efeitos: o cliente recebe o crédito
**e** o cupom volta, num pedido que terminou cancelado e portanto fora do
faturamento.

**O que já protege, e até onde:** o crédito é idempotente por
`cashback:earn:{order.id}`, então ele não sai em dobro. Isso limita o estrago —
não o elimina, porque o par crédito-mais-estorno continua acontecendo.

**A varredura de pagamento não concluído tem o mesmo desenho.** Ela relê o
pedido dentro de `_cancel_one` antes de cancelar, e o docstring diz que é a
releitura que barra o cliente que voltou e pagou. **Ela estreita a janela; não a
fecha** — sem lock, o pagamento pode entrar entre a releitura e a escrita.

### Buraco 2 — `payment_status` tem TRÊS escritores, e a regra mora nos chamadores

`orders.status` tem um escritor e a regra dentro dele. `payment_status` não:

| Escritor | Valida? | Como |
|---|---|---|
| `attach_payment_intent` | **não** | — |
| `update_payment_status` ← `payment_service.handle_webhook` | sim | `ensure_payment_transition_allowed` (levanta) |
| `update_payment_status` ← `payment_refund_service` | sim | `payment_transition_is_allowed` (predicado) |

É a família das armadilhas 46 e 47: **enquanto a regra depende de alguém lembrar
de aplicá-la, ela é uma recomendação.** Uma quarta porta de pagamento nasce sem
validação e nada acusa. E as duas que validam usam funções diferentes da mesma
regra — o que hoje é só ruído, e é como duas respostas para a mesma pergunta
começam.

**Hoje nenhum estado inválido é alcançável**, e vale saber por quê: as fontes de
`attach_payment_intent` são `PAYABLE_STATUSES = ("pending", "failed")`, e todos
os destinos possíveis a partir dos dois estão no grafo.

**A armadilha para quem for "fechar o buraco":** `attach_payment_intent` faz
`pending → pending` de propósito, como no-op documentado (é o segundo clique em
"pagar" devolvendo o mesmo pix, armadilha 6). O grafo **recusa** essa transição.
Pôr `ensure_payment_transition_allowed` ali sem tratar o caso **quebra o retry
do pix**. Isto precisa estar escrito antes de alguém tentar.

### Buraco 3 — o nascimento não é conferido contra o grafo

`OrderService.create_order` grava `status="pending"` e
`payment_status="pending"|"on_delivery"` direto. Isso está **certo** —
nascimento não é transição —, mas nada garante que o valor inicial seja um
estado que o grafo conhece. Um `payment_flow` novo poderia gravar um
`payment_status` fora de `PAYMENT_STATUS_TRANSITIONS`, e o pedido nasceria num
estado do qual não se sai.

### A proposta, com tamanho

Em ordem de valor. Cada uma é independente.

| # | O quê | Tamanho | Fecha |
|---|---|---|---|
| **A** | `get_order_detail` (o caminho de `apply`) passa a travar a linha: `with_for_update()`. Os efeitos colaterais já estão na mesma transação | **1 arquivo de `src/`** (`order_repository`) + 1 parâmetro em `apply` para escolher o caminho travado + 2 testes `db` de concorrência | Buraco 1 |
| **B** | A validação desce para o **escritor**: `update_status`/`update_payment_status` recusam a transição, e os chamadores param de validar | **3 arquivos de `src/`** + a decisão de o que fazer com o `pending → pending` de `attach_payment_intent` (a saída limpa é um parâmetro `permitir_repeticao=True` com o motivo escrito) | Buraco 2 |
| **C** | Uma partição, como `tests/test_particao_dos_status.py` já faz para faturamento: todo estado inicial gravado por `create_order` tem que estar no grafo | **1 teste**, zero `src/` | Buraco 3 |
| **D** | Atualizar a armadilha 25.1: são **cinco** portas, não quatro | 1 parágrafo | — |

**Recomendo A e C.** A é o único que fecha um caminho de dinheiro; C é um teste
e nada mais. B é correto e é o maior — e o `pending → pending` faz dele uma
decisão, não uma refatoração.

**O que eu NÃO proponho:** reescrever a máquina, trocar por uma biblioteca de
state machine, ou mover o grafo para o banco. O grafo em Python é lido de cima a
baixo, e é o que a regra 7 do CLAUDE.md pede.

---

## 9. Varredura final

### A suíte

| | |
|---|---|
| suíte inteira (`pytest -q`, com Docker de pé) | **3900 passed, 0 vermelhos**, 254 subtests |
| suíte rápida (`pytest -m "not db"`) | 3000 passed, 892 deselected |
| `ruff check .` | limpo |
| `scripts/export_openapi.py --check` | `openapi.json` em dia |

Medido no branch `refactor/branches-endereco-morto-e-tarifa`, que é o que tem
mais coisa em cima da `main`. Os outros dois branches rodaram verdes também.

### O que o código exige do `.env`

A conferência anterior foi feita contra o `.env` **local**, que tem **18
linhas** — e duas delas o código nem lê mais. O `.env` da VPS é maior; esta é a
lista para conferir lá, por consequência.

#### Derrubam o boot se faltarem (`ValidationError` da pydantic, no import)

`DATABASE_URL` · `SUPABASE_URL` · `CUSTOMER_AUTH_SECRET` ·
`ADMIN_AUTH_SECRET` · `EMAIL_CODE_SECRET` · `PASSWORD_RESET_SECRET` ·
`OPENAI_API_KEY`

As sete estão no `.env` local, então provavelmente estão na VPS — a API não
subiria sem elas.

#### Derrubam o boot pelo conteúdo (`startup_checks`)

| Condição | Variáveis |
|---|---|
| `DELIVERY_ESTIMATE_PROVIDER=google_routes` com a chave vazia | `GOOGLE_MAPS_ROUTES_API_KEY` |
| `PAYMENT_PROVIDER=mercadopago` sem a chave de cifra | `PAYMENT_CREDENTIALS_ENCRYPTION_KEY` |
| `MERCADOPAGO_ENVIRONMENT` fora de `test`/`production` | `MERCADOPAGO_ENVIRONMENT` |
| **NOVO nesta rodada** — segredo de assinatura vazio | `CUSTOMER_AUTH_SECRET`, `ADMIN_AUTH_SECRET` |
| os dois segredos com o mesmo valor | idem |

> **Confira `PAYMENT_CREDENTIALS_ENCRYPTION_KEY` antes do próximo deploy.** Ela
> não está no `.env` local, e se a VPS estiver com
> `PAYMENT_PROVIDER=mercadopago` sem ela, **a API não sobe** — e o boot já era
> assim antes desta rodada.

#### Só avisam no boot (a API sobe, alguma coisa não funciona)

| Variável | O que para |
|---|---|
| `REDIS_URL` | rate limit vira N× o configurado; cache de entrega e de embedding morrem a cada deploy (três avisos separados, um por consequência) |
| `SUPABASE_SERVICE_ROLE_KEY` | upload de imagem do painel responde 503 |
| `GOOGLE_OAUTH_CLIENT_IDS` | entrar com Google responde 503 — **o resto da API fica perfeita**, então o sintoma não aponta para cá |
| `PAYMENT_WEBHOOK_SECRET` | webhook do sandbox responde 503 |
| `WHATSAPP_TOKEN_ENCRYPTION_KEY` · `WHATSAPP_APP_SECRET` · `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | os três avisos do WhatsApp, cada um com o seu sintoma — e só saem se a frente estiver em uso |
| `RATE_LIMIT_CLIENT_IP_HEADER` | vazio, todo cliente cai no mesmo balde do proxy |

#### Não avisam em lugar nenhum (o sintoma é o chamado)

| Variável | O que para |
|---|---|
| `RESEND_API_KEY` | cadastro responde **500** e a recuperação de senha falha **em silêncio** — `forgot_password` engole a exceção de propósito. **É a única sem aviso de boot** |
| `EMAIL_FROM` | remetente do e-mail |
| `PLATFORM_METRICS_KEY` | `GET /internal/ai-usage` e `GET /internal/whatsapp-usage` respondem 503 |
| `VOICE_ENABLED` | falsa, o router de voz não é registrado |

#### Podem SAIR do `.env` da VPS

`INTERNAL_API_KEY` e `EXPERIMENTO_VOZ_ENABLED`. As duas saíram do código nesta
rodada e `extra="ignore"` faz o boot ignorá-las enquanto estiverem lá — remover
é limpeza, não urgência.

### O que este levantamento NÃO consegue ver

**O `.env` da VPS.** A memória desta máquina registra que consulta ao banco de
produção é bloqueada, e o mesmo vale para o arquivo. A lista acima é o que o
CÓDIGO exige; casar com o que está lá é conferência manual — e o jeito barato de
fazer a maior parte dela é ler o boot:

```bash
docker logs pedeaqui-api 2>&1 | grep '\[Startup\]'
```

Toda variável ausente que tenha consequência conhecida sai ali, com o sintoma
junto. As quatro da última tabela são as que **não** saem, e por isso estão
escritas aqui.

---

## Os três PRs, e por que eles não foram abertos daqui

**Não existe `gh` nesta máquina** — nem no PATH nem em `Program Files`. Os três
branches estão empurrados; os links do `pull/new` são estes:

| Branch | Link | Por que é PR e não `main` |
|---|---|---|
| `fix/customer-jwt-secret-e-trava-dos-segredos` | [pull/new](https://github.com/Caua1705/pedeaqui_back/pull/new/fix/customer-jwt-secret-e-trava-dos-segredos) | mexe em **autenticação** |
| `feat/custo-de-indexacao-e-avisos-de-whatsapp` | [pull/new](https://github.com/Caua1705/pedeaqui_back/pull/new/feat/custo-de-indexacao-e-avisos-de-whatsapp) | traz **migração** (preparada, não aplicada) |
| `refactor/branches-endereco-morto-e-tarifa` | [pull/new](https://github.com/Caua1705/pedeaqui_back/pull/new/refactor/branches-endereco-morto-e-tarifa) | traz **duas migrações** (preparadas, não aplicadas) |

Cada um é **um commit só**, então o GitHub já preenche a descrição do PR com a
mensagem dele — que foi escrita para isso. Não há nada a colar.

### Nenhum dos três aplica migração em produção

As três revisões desta rodada moram em `alembic/preparadas/`, que o Alembic não
lê. Mesclar os três e fazer deploy **não** roda nenhuma delas — o
`alembic upgrade head` do entrypoint continua parando em `20260905_0056`.

`tests/test_alinhamento_orm_schema.py::test_nada_em_preparadas_esta_na_cadeia`
cobra exatamente isso, e `tests/test_revisao_preparada_da_indexacao.py` e
`tests/test_revisoes_preparadas_de_branches.py` cobram o resto da armadilha 53.
