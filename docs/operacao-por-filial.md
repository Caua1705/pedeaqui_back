# A operação passa a ser da filial

Documento para quem implementa o **app do cliente** e o **painel do lojista**.
Tudo o que é preciso saber está aqui; não é necessário acesso ao código do
backend.

**Estado em 18/08/2026.** Revisão de banco `20260818_0025`. Base da API: a
mesma do resto do app (`https://api.pederapidex.com` em produção,
`http://localhost:8000` em desenvolvimento).

É o **passo 2** de três. O passo 1 (`91a68a4`) criou a tela de escolha de
filial, documentada em [`contrato-filiais-frontend.md`](contrato-filiais-frontend.md).

> **⚠️ O passo 3 foi feito em 20/08/2026** (revisões `20260820_0026`/`0027`).
> Onde este documento diz que os produtos continuam sendo do restaurante,
> **ele está desatualizado**: cardápio, preço e disponibilidade passaram a ser
> de cada filial, e `payment_methods` saiu de `settings`. Vale
> [`cardapio-por-filial.md`](cardapio-por-filial.md). O que este documento
> descreve sobre OPERAÇÃO — os dois regimes, `is_open`, herança dos números
> comerciais — continua correto e não foi tocado.

---

## 1. O que mudou, em um parágrafo

O botão **"fechar agora"** era do restaurante inteiro. Fechar a loja do Centro
fechava também a da Aldeota, e não havia como fechar só uma. O mesmo valia para
"aceita entrega" e "aceita retirada": o quiosque de shopping que só faz
retirada desligava a entrega da rede toda. Agora esses três são **de cada
filial**, e o pedido é recusado pela filial que o cliente escolheu — não por um
valor compartilhado.

Junto com eles foram, em outro regime, os números comerciais: valor mínimo,
taxa de serviço, prazo estimado e taxa de contingência. Esses **continuam
tendo um padrão do restaurante**, e a filial só o sobrescreve quando precisa
divergir.

---

## 2. Os dois regimes, e por que não é um só

Esta é a decisão de desenho do passo, e ela explica por que alguns campos têm
`null` e outros não.

### Estado do dia — só da filial, nunca herda

| Campo | O que é |
|---|---|
| `is_open` | o "fechar agora": acabou o gás, a fila cresceu |
| `accepts_delivery` | esta loja está entregando hoje |
| `accepts_pickup` | esta loja está atendendo retirada |
| `delivery_paused_until` | a pausa temporária da entrega (revisão `0030`) |

São o que **alguém no balcão aperta durante o expediente**. Um padrão do
restaurante para eles não responderia pergunta nenhuma: "o restaurante está
fechado mas esta filial está aberta" não é um estado que a operação consiga
ler. Não têm `null`, não herdam, e a única resposta possível é a da própria
loja.

### Termo comercial — padrão do restaurante, sobrescrita por filial

| Campo | O que é |
|---|---|
| `min_order_value` | pedido mínimo |
| `service_fee_enabled` / `service_fee_amount` | taxa de serviço |
| `estimated_delivery_time_min` / `_max` | prazo mostrado na vitrine |
| `default_delivery_fee` | taxa de contingência quando o Google cai |
| `free_delivery_enabled` / `free_delivery_min_order_value` | frete grátis acima de X (revisão `0030`) |
| `receipt_footer_message` | mensagem no rodapé da comanda (revisão `0029`) |

São **preço negociado da marca**. Aqui `null` na filial significa **"herda o
padrão do restaurante"**, e é o estado em que toda filial existente nasceu.

**Por que não "tudo por filial".** Com nove campos por loja e nenhuma herança,
abrir a quinta loja custaria 45 valores digitados, e mudar a taxa de serviço da
marca de R$ 0,99 para R$ 1,49 viraria cinco edições — sem nenhuma tela capaz de
mostrar se as cinco concordam. Com o split, a quinta loja custa **três chaves
para ligar** e os seis preços já chegam certos.

**Por que não "tudo herdado".** Porque `is_open` compartilhado é exatamente o
defeito que este passo fecha.

### Dois campos herdados precisam de um SEGUNDO estado para "desligado"

`NULL` significa "herda". Para a filial poder **recusar** o que a marca
configurou, é preciso um valor que signifique "desligado nesta loja" — e nem
todo tipo tem um:

| Campo | Como a filial recusa |
|---|---|
| `service_fee_enabled` | `false` (booleano ao lado do valor) |
| `free_delivery_enabled` | `false` — idem, e é por isso que a campanha são **dois** campos: não existe número que diga "desligado", já que `0` seria "grátis sempre" |
| `receipt_footer_message` | `""` — a string vazia é "esta loja não imprime rodapé" |

O `_ou_herdado` testa `is not None` justamente por isso. Um `or` no lugar dele
colapsaria os três: a taxa que o lojista desligou voltaria a ser cobrada, a
campanha voltaria a valer na loja que a recusou, e o rodapé da marca voltaria
a sair — os três sem uma linha no log.

### A pausa da entrega é estado do dia, mas com uma diferença

`accepts_delivery` espera alguém religar; `delivery_paused_until` vence
sozinho. É a única chave da operação que se desfaz sem intervenção, e ela
existe por isso: o dia em que se pausa a entrega é o dia em que ninguém lembra
de despausá-la. As duas se combinam em **`accepts_delivery_now`** — o campo
que decide pedido. Detalhes em
[entrega-e-horarios.md](entrega-e-horarios.md), §1.1.

### `default_delivery_fee` entrou por estar na mesma situação

Não estava no pedido original. Ele é a taxa fixa usada quando a regra por km da
filial não pode ser aplicada — e **essa regra por km já era da filial**. Uma
contingência única deixava a loja do outro lado da cidade com o mesmo número da
loja da esquina. A regra de que ele **só vale se for maior que zero** não
mudou: zero desliga a contingência, não significa frete grátis.

---

## 3. O que muda para o app do cliente

### 3.1 `is_open_now` agora combina duas coisas

`POST /restaurants/{slug}/branches/availability` (a rota do passo 1) continua
respondendo `is_open_now` como booleano, e continua sendo **a única resposta
para "posso pedir nesta loja?"**. O que mudou é o que entra na conta: antes era
só a agenda da semana; agora é a agenda **e** a pausa manual daquela filial.

**Campo novo na resposta: `closed_reason`.**

| Valor | O que aconteceu | O que a tela escreve |
|---|---|---|
| `null` | a filial está atendendo | nada |
| `"outside_business_hours"` | fora do horário cadastrado | "abre às 18:00" (use `current_period` das outras, ou `/info`) |
| `"branch_paused"` | o balcão apertou "fechar agora" | "fechada no momento" — **não** prometa horário |

A distinção não é decorativa: `outside_business_hours` passa sozinho quando o
relógio virar, `branch_paused` só passa quando alguém reabrir a loja. Escrever
"abre às 18:00" para uma loja pausada é prometer o que ninguém garantiu.

### 3.2 `current_period` pode vir preenchido com `is_open_now: false`

**Isto não é bug, e é a mudança que mais tem chance de virar chamado.**

O §4.2 do contrato do passo 1 dizia que `current_period` é nulo com a loja
fechada. Continua verdade quando quem fecha é a **agenda**. Quando quem fecha é
a **pausa manual**, a agenda está em ordem e `current_period` vem preenchido.

São duas coisas diferentes: a agenda é cadastro, a pausa é o dia de hoje.
`current_period` fala da agenda; **quem decide se a filial atende é
`is_open_now`, sempre.** A regra prática não mudou desde o passo 1: não
recalcule `is_open_now` no app.

Quando as duas coisas valem ao mesmo tempo (fora do horário **e** pausada),
`closed_reason` reporta a **agenda** — porque `current_period` já sai nulo
nesse caso, e dizer "pausada" faria a tela afirmar que a agenda está em ordem
enquanto o campo dela vem vazio.

### 3.3 `delivery.reason` ficou por filial

O vocabulário não mudou; o escopo sim.

- `delivery_disabled` era "o restaurante não aceita entrega". Agora é **"esta
  filial não aceita entrega"** — e as outras filiais da mesma lista podem estar
  entregando normalmente. Se a tela esconde a seção de entrega inteira ao ver
  esse código, ela passa a esconder demais: esconda só naquela filial.
- `branch_closed` continua sendo "a filial está fechada agora" e agora cobre os
  **dois** motivos, agenda e pausa. Para saber qual foi, olhe `closed_reason`
  no mesmo item da lista.

### 3.4 `GET /restaurants/{slug}/menu` aceita `branch_id`

```
GET /restaurants/{restaurant_slug}/menu?branch_id=<uuid da filial>
```

O parâmetro é **opcional** e **não filtra produtos** — o cardápio continua
sendo do restaurante. Ele resolve o bloco `settings` da resposta, que passou a
descrever **uma filial**.

| Situação | O que acontece |
|---|---|
| `branch_id` informado | o bloco descreve aquela filial |
| `branch_id` omitido | vale a filial padrão (principal se houver, senão a primeira ativa em ordem alfabética) — a mesma de `/info` e de `/delivery/estimate` sem filial |
| `branch_id` de outro restaurante | **404** |
| restaurante sem filial ativa | `settings: null` e `settings_branch_id: null`; o resto do cardápio sai normalmente |

**Campo novo na raiz da resposta: `settings_branch_id`** (uuid | null). Diz de
qual filial o bloco está falando. Sem ele o app não teria como saber se o valor
mínimo na tela é o da loja que o cliente escolheu.

**Os nomes dos campos dentro de `settings` não mudaram** — `min_order_value`,
`is_open`, `accepts_delivery`, `accepts_pickup`, `service_fee_*`,
`estimated_delivery_time_*`, `default_delivery_fee`, `payment_methods`. O que
mudou é de quem eles falam.

Uma consequência que vale conferir no app: **`settings` deixou de ser nulo para
restaurante sem linha de configuração.** Antes ele vinha nulo nesse caso; hoje
vem preenchido com os padrões da plataforma, porque zerá-lo esconderia o
`is_open` de uma filial que pode estar pausada. `settings` só é nulo quando não
há filial ativa nenhuma.

`payment_methods` dentro de `settings` era o único campo que continuava sendo
do restaurante, e era **dado morto**. **Ele saiu da resposta e da tabela na
revisão `20260820_0027`.** A fonte da verdade das formas de pagamento é
`GET /restaurants/{slug}/info?branch_id=...`, por filial.

### 3.5 O que o app deve fazer, em ordem

1. `POST .../branches/availability` → o cliente escolhe a filial, guarde o `id`.
2. `GET .../menu?branch_id=<id escolhido>` → cardápio **e** os números daquela
   loja. Chamar sem `branch_id` depois de o cliente ter escolhido mostra o
   mínimo e a taxa de outra loja.
3. `POST .../delivery/estimate` e `POST /orders` como antes.

---

## 4. O que muda para o painel do lojista

### 4.1 As rotas que saíram

| Antes | Agora |
|---|---|
| `PATCH /admin/settings/store-status` | `PATCH /admin/branches/{branch_id}/store-status` |
| `PATCH /admin/settings` com `accepts_delivery`/`accepts_pickup` | `PATCH /admin/branches/{branch_id}/order-types` |

`GET`/`PATCH /admin/settings` continuam existindo e continuam sendo do
restaurante, mas **perderam três campos**: `is_open`, `accepts_delivery` e
`accepts_pickup` não estão mais em `AdminRestaurantSettingsResponse` nem em
`AdminRestaurantSettingsUpdate`. Mandá-los no corpo do `PATCH` responde **422**.

O que sobrou lá são **os padrões**: `min_order_value`,
`estimated_delivery_time_min/max`, `default_delivery_fee`,
`service_fee_enabled/amount`. Nenhum pedido lê esses valores direto — a filial
os herda nos campos que deixou nulos.

### 4.2 As rotas novas

#### `GET /admin/branches/operation`

A tela de operação inteira em uma chamada. Aceita `?branch_id=` opcional, que
**só restringe**: quem está preso a uma filial e pedir outra recebe 404; quem
não pedir nada recebe a dele.

```json
[
  {
    "branch_id": "6a1f...",
    "branch_name": "Centro",
    "is_open": true,
    "is_open_now": false,
    "accepts_delivery": true,
    "accepts_pickup": true,
    "overrides": {
      "min_order_value": 45.0,
      "estimated_delivery_time_min": null,
      "estimated_delivery_time_max": null,
      "default_delivery_fee": null,
      "service_fee_enabled": null,
      "service_fee_amount": null
    },
    "effective": {
      "min_order_value": 45.0,
      "estimated_delivery_time_min": 30,
      "estimated_delivery_time_max": 60,
      "default_delivery_fee": 9.0,
      "service_fee_enabled": true,
      "service_fee_amount": 0.99
    }
  }
]
```

Três coisas que a tela precisa ler direito:

- **`overrides` é o que está gravado na filial.** `null` = herdando. É o que
  vai no campo de edição: campo vazio significa "usando o padrão".
- **`effective` é o que o próximo pedido vai usar.** É o que vai no texto ao
  lado do campo.
- **`is_open` e `is_open_now` são coisas diferentes, e as duas aparecem de
  propósito.** `is_open` é a chave que o lojista controla; `is_open_now` é essa
  chave combinada com a agenda de hoje. `is_open: true` com `is_open_now:
  false` significa "você deixou aberta, mas o horário de hoje já fechou" — e é
  a resposta para o chamado mais comum ("não está entrando pedido").

#### `PATCH /admin/branches/{branch_id}/store-status`

Corpo idêntico ao da rota antiga: `{"is_open": false}`. Devolve
`AdminBranchOperationResponse`. Papel: **PESSOAS** (owner, manager, attendant)
— não mudou, porque "vamos parar de aceitar pedido" às 21h continua sendo
decisão de quem está no balcão.

#### `PATCH /admin/branches/{branch_id}/order-types`

```json
{ "accepts_delivery": false }
```

Edição parcial: mandar só um não mexe no outro. Corpo vazio (os dois `null`) é
**422**. Desligar os dois é permitido e equivale a fechar a loja. Papel:
**GERENCIA** (owner, manager) — desligar a entrega muda o que o cliente vê por
tempo indeterminado, enquanto fechar a loja é um gesto do dia.

#### `PATCH /admin/branches/{branch_id}/settings`

As sobrescritas comerciais. Papel: **SOMENTE_DONO** — são os mesmos números com
que a loja negocia, e deixá-los na gerência daria por filial a permissão que se
recusou dar no restaurante.

**Três estados por campo, e o terceiro é o que importa:**

| No corpo | O que acontece |
|---|---|
| campo **ausente** | não mexe |
| campo **com valor** | esta filial passa a usar esse valor |
| campo com **`null` explícito** | esta filial **volta a herdar** o padrão do restaurante |

```json
{ "min_order_value": 45.00 }     // esta loja cobra 45
{ "min_order_value": null }      // volta a herdar
{ }                              // não muda nada
```

Sem o terceiro estado não haveria como desfazer uma divergência: a filial
ficaria com a cópia congelada para sempre, e mudar o padrão no
`PATCH /admin/settings` não chegaria mais nela.

Validação de par (`estimated_delivery_time_min` × `_max`) roda sobre a **mescla
com o que já está gravado na filial** e responde 422 — mandar só o máximo não
pode deixar a filial com teto abaixo do piso.

### 4.3 A tela que o painel precisa ganhar

Enquanto `is_open` era um só, a tela de configuração tinha um switch. Agora ela
tem **uma linha por filial**, e as duas informações que faltavam passam a
existir:

- o dono com cinco lojas vê as cinco chaves lado a lado — a conferência que não
  era possível antes;
- cada linha diz se a loja está de fato no ar (`is_open_now`), e não só se a
  chave está ligada.

Quem está preso a uma filial recebe uma lista de um item, então a mesma tela
serve para os dois casos sem a interface conhecer a regra de escopo.

---

## 5. O banco

### 5.1 O que saiu e o que entrou

```
restaurant_settings              branches
──────────────────────────       ─────────────────────────────────
is_open            REMOVIDA  →   is_open            NOT NULL DEFAULT true
accepts_delivery   REMOVIDA  →   accepts_delivery   NOT NULL DEFAULT true
accepts_pickup     REMOVIDA  →   accepts_pickup     NOT NULL DEFAULT true

min_order_value        fica  ←   min_order_value             NULL = herda
service_fee_enabled    fica  ←   service_fee_enabled         NULL = herda
service_fee_amount     fica  ←   service_fee_amount          NULL = herda
estimated_delivery_*   fica  ←   estimated_delivery_*        NULL = herda
default_delivery_fee   fica  ←   default_delivery_fee        NULL = herda
```

Não se moveu, e não vai se mover: `platform_commission_percent` é da
**plataforma**, não do lojista, e não aparece em schema nenhum do painel. Ao
lado dele ficava `voice_enabled`, pelo mesmo motivo; ela saiu do ORM em
06/09/2026, com o assistente de voz, e sai do banco quando a revisão preparada
`20260906_0060` for aplicada. `payment_methods` (jsonb) ficou onde estava por já ser dado morto — removê-lo
muda contrato público, e foi feito no passo 3 (revisão `20260820_0027`).

### 5.2 Onde a regra mora, uma vez só

`src/services/branch_operation.py` → `resolve_branch_operation(branch,
restaurant_settings)`. É uma função pura, e é o **único** lugar que combina
filial e padrão. Pedido, estimativa de entrega, cardápio e painel leem a
resposta dela.

A distinção que ela faz é entre **`NULL` e valor**, nunca entre verdadeiro e
falso: `service_fee_enabled = false` na filial é uma escolha ("esta loja não
cobra taxa") e não pode cair no `true` do restaurante. Um `or` no lugar do
`is not None` teria feito a filial cobrar uma taxa que o lojista desligou.

### 5.3 O que a migração faz com o Júnior

O Júnior da Picanha tem duas filiais e, hoje, um `is_open` só.

1. As três colunas nascem em `branches` com `NOT NULL DEFAULT true`.
2. **Um `UPDATE` copia o valor que o restaurante tinha para todas as filiais
   dele.** Se o Júnior estiver com a loja fechada no momento do deploy, as duas
   filiais sobem fechadas — sem esse `UPDATE`, o `DEFAULT true` reabriria as
   duas sozinho e pedido entraria numa loja sem ninguém para produzi-lo.
3. As seis colunas comerciais nascem **nulas** em todas as filiais, ou seja,
   herdando. Copiar o valor do restaurante para cada uma deixaria uma cópia
   congelada, e a próxima edição do padrão não chegaria a nenhuma.
4. As três colunas antigas somem de `restaurant_settings`.

Depois do deploy, as duas lojas do Júnior podem divergir quando ele quiser.
Antes dele não havia o que preservar, porque não havia dois valores.

Restaurante **sem** linha em `restaurant_settings` fica com `true` nas três, que
é exatamente o que valia para ele antes.

O `downgrade` existe e **perde informação**: N filiais voltam para uma coluna
só, usando o valor da filial principal. Não existe resposta melhor com uma
coluna.

---

## 6. O que rodar depois do deploy

O `docker-entrypoint.sh` já roda `alembic upgrade head` antes do Uvicorn, então
a migração se aplica sozinha. O que fica para conferir à mão:

**1. As filiais subiram com o estado certo.**

```sql
SELECT r.slug, b.name, b.is_open, b.accepts_delivery, b.accepts_pickup
  FROM branches b
  JOIN restaurants r ON r.id = b.restaurant_id
 WHERE b.is_active IS TRUE
 ORDER BY r.slug, b.name;
```

Se alguma loja aparecer fechada sem motivo, é porque o restaurante estava
fechado no minuto do deploy — abra pelo painel, filial por filial.

**2. Ninguém ficou com sobrescrita indesejada.** Deve voltar vazio: todas as
filiais nascem herdando.

```sql
SELECT name FROM branches
 WHERE min_order_value IS NOT NULL
    OR service_fee_enabled IS NOT NULL
    OR service_fee_amount IS NOT NULL
    OR default_delivery_fee IS NOT NULL;
```

**3. O check de onboarding.** Ele passou a conferir a taxa de entrega **filial
por filial**, inclusive o "aceita entrega":

```
python scripts/check_restaurant.py junior-da-picanha
```

**4. O painel.** As telas que chamavam `PATCH /admin/settings/store-status`
param de funcionar com **404** até apontarem para
`/admin/branches/{branch_id}/store-status`. Não há período de convivência: a
rota antiga não existe mais, de propósito — mantê-la exigiria escolher uma
filial por conta própria, que é o defeito que este passo fecha.

---

## 7. O que este passo NÃO faz

- **Não filtrava o cardápio por filial.** Naquele passo, `branch_id` no
  `/menu` resolvia só o bloco `settings`. **Isso mudou no passo 3** — ver
  [`cardapio-por-filial.md`](cardapio-por-filial.md).
- **Não mexe em cupons, relatórios nem no Rapi.**
- **Não move `payment_methods` do jsonb.** Ver §5.1.
- **Não cria "horário por filial"** — isso já existia
  (`branch_business_hours`). O que este passo acrescentou foi a pausa manual ao
  lado dele.

---

## 8. Coisas que vão quebrar se ninguém souber

- **`current_period` preenchido com `is_open_now: false` não é bug.** É a loja
  pausada dentro do horário. Ver §3.2.
- **`delivery_disabled` é da filial, não do restaurante.** Esconder a seção de
  entrega inteira ao ver esse código esconde as outras lojas que entregam. Ver
  §3.3.
- **`null` em `overrides` não é "não configurado, use zero".** É "herdando", e
  o valor real está em `effective`. Mostrar zero ali diria ao lojista que a
  loja não cobra taxa de serviço quando ela cobra.
- **`PATCH /admin/branches/{id}/settings` com `null` explícito apaga a
  sobrescrita.** Um painel que serializa campos vazios como `null` em vez de
  omiti-los vai devolver a filial para o padrão sem ninguém pedir.
- **`/menu` sem `branch_id` responde pela filial padrão.** Depois de o cliente
  escolher uma loja, chamar sem o parâmetro mostra o mínimo e a taxa de outra.
