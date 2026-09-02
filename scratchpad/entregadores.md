# Entregadores — fase 1 — fonte da verdade

Branch: `rodada/entregadores`, saindo de `rodada/backend-8`. Nunca commitar na
`main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Contexto de operação (do pedido): no piloto (Júnior da Picanha, duas filiais)
os motoboys são FIXOS. O cadastro não precisa ser instantâneo; o código de
acesso é gerado uma vez e raramente trocado. **Otimizar para durar, não para
cadastrar rápido.**

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 0 | As três perguntas | **respondidas** — nenhuma muda o desenho |
| 1 | Schema: `couriers`, `courier_assignments`, taxa do entregador em `branches` | **feito** — revisão `20260903_0045` |
| 2 | Ferramenta: varredura de escopo cobre `/courier` (antes das rotas) | **feito** |
| 3 | Admin: taxa do entregador da filial | **feito** — `GET`/`PATCH /admin/branches/{id}/courier-fee` |
| 4 | Admin: cadastro (listar, criar, editar, ativar/desativar, excluir) + código | **feito** — `/admin/couriers`, `/admin/couriers/{id}`, `/admin/couriers/{id}/access` |
| 5 | Admin: atribuir e desatribuir pedidos | **feito** — `POST`/`GET /admin/couriers/{id}/assignments`, `GET`/`DELETE /admin/orders/{id}/courier` |
| 6 | Entregador: autenticação (link + código), lista, saiu/entregue, histórico | **feito** — cinco rotas em `/courier/{link_token}/...` |
| 7 | Docs, contrato para o painel e para o app, fase 2 | pendente |

---

## 0. As três perguntas

### 0.1 A taxa do entregador — onde mora

**O que existe.** A taxa que o CLIENTE paga não é por região: é por raio, com
cinco colunas da FILIAL (`branches.delivery_base_fee`, `delivery_fee_per_km`,
`delivery_min_fee`, `delivery_max_fee`, `delivery_max_distance_km`), sem
herança do restaurante. A fórmula mora em
`DeliveryEstimateService._calculate_delivery_fee`:

```
raw = base + km × por_km ;  taxa = min(max(raw, piso), teto)
```

Não existe tabela de "área de entrega". O que mais se parece com "região" é
`branch_delivery_time_bands` (tetos de km com PRAZO), mas ela é opcional, é
apagada inteira por `PUT {"bands": []}`, e endereço além do último teto é um
estado válido ("vale o tempo do Google"). Pendurar dinheiro nela faria a taxa
do motoboy sumir junto com uma edição de prazo, e faria "além do último teto"
significar "sem taxa" em silêncio.

**Decisão: a taxa do entregador espelha a do cliente, na mesma tabela.** Duas
colunas novas em `branches`, mesmo regime das cinco (só da filial, sem
herança, `NULL` = não configurado):

| Coluna | O que é |
|---|---|
| `courier_fee_base` | valor fixo por entrega |
| `courier_fee_per_km` | adicional por km |

Motoboy fixo pago por corrida = `base` preenchida e `per_km = 0`. Motoboy pago
por distância = os dois. Não há piso nem teto: com dois números o dono já
descreve os dois acordos que existem no piloto, e cada coluna a mais é uma que
o painel precisa desenhar e explicar.

**Onde ela entra em quem calcula frete: em lugar nenhum.** `DeliveryEstimateService`
não é tocado. A estimativa acontece antes de existir pedido, é cacheada e
publicada para o cliente — nada disso diz respeito ao motoboy. A taxa do
entregador é calculada **na atribuição** (`AdminCourierService.assign`), a
partir do `orders.delivery_distance_km` que o pedido já congelou, e gravada
como **snapshot** em `courier_assignments.courier_fee` — mesma disciplina de
`unit_price_snapshot`: mudar a taxa amanhã não muda a corrida de ontem.
Reatribuir é uma linha nova com a taxa daquele momento.

Regras do cálculo (`src/services/courier_fee.py`, função pura):

- as duas colunas nulas → snapshot **nulo** ("sem taxa configurada"), nunca
  zero — zero é um número que soma, e faria a corrida aparecer como grátis no
  histórico que o dono usa para pagar (armadilha 11, o mesmo raciocínio);
- `delivery_distance_km` nulo (Google caiu, `configured_fallback`) → só a
  `base` conta; o histórico mostra a distância nula ao lado para o dono acertar
  à mão;
- `Decimal`, `quantize_money`, e `money_to_float` só na resposta.

**Nenhum número de dinheiro do cliente muda:** não entra em `cartTotals`, em
`orders.total`, na comissão nem na estimativa. É registro.

**Quem escreve:** rota própria `PATCH /admin/branches/{id}/courier-fee`,
SOMENTE_DONO — é o que a loja paga, e a regra do repositório é que quem
escreve dinheiro é o dono. Rota separada e não campo no `PATCH /branches/{id}`
(GERENCIA) para não repetir o `ensure_pode_definir_preco` por corpo, que
recusa o gerente que mandou o campo sem tocar nele.

**Alternativa registrada, não escolhida:** taxa por faixa de km em
`branch_delivery_time_bands`. Fica para a fase 2 se o dono quiser "até 3 km
R$ 5, até 6 km R$ 8" — aí a coluna entra na tabela de faixas com o cuidado de
não morrer no `PUT` de prazos.

### 0.2 A máquina de estados

**Como o status é escrito hoje.** Há UMA escrita de `orders.status` no
sistema: `OrderStatusChangeService.apply`. Chegam nela três portas — o PATCH
de status do painel, o cancelamento do painel e o cancelamento pelo cliente
(duas rotas, mesma porta). O grafo mora em `order_state_machine.py`:

```
ready ──→ out_for_delivery ──→ completed
```

As duas arestas que o entregador precisa **já existem** e `out_for_delivery`
já é restrito a `order_type == "delivery"`. `completed` é terminal e é onde o
cashback é creditado — dentro do `apply`, de graça para qualquer porta.

**Quem pode escrever hoje:** só o painel (PESSOAS — dono, gerente, atendente),
pelo escopo do token.

**Dá para o entregador escrever sem abrir a frente inteira: dá.** O desenho
já previu isso: o docstring do writer diz "*este service NÃO autoriza — as
portas autorizam de formas incomparáveis*" e "*quem pode X em que estado é
regra da PORTA*" (`ensure_customer_can_cancel` é o exemplo). O entregador é a
**quarta porta**, e o mínimo que precisa existir é:

1. `CourierDeliveryService`, que carrega o pedido **pela atribuição** (o
   pedido é dele, da filial dele) e chama `apply` com
   `changed_by="entregador:{nome}"` e `route`/`requester` próprios — o mesmo
   que `CustomerOrderCancelService` faz;
2. `ensure_courier_can_set(current, new)` em `order_state_machine.py`, a regra
   da porta: o entregador só anda `ready → out_for_delivery` e
   `out_for_delivery → completed`. Nada mais — nem cancelar, nem voltar.

Nada muda no grafo, no writer, nem nas outras três portas. A armadilha 25.1
da skill passa a dizer "quatro portas".

O que o writer já faz e o entregador ganha sem pedir: `ensure_payment_allows_order_status`
(pedido online não pago não sai para entrega — não chega a `ready` de qualquer
jeito), histórico com autor, crédito de cashback no `completed`, evento
`order.status_changed` no stream do painel (ele lê `order_status_history`).

**"Vários de uma vez"**: cada pedido passa pelo `apply` sozinho (uma
transação por pedido). A resposta é **por item** (`ok` / `error`), e não
tudo-ou-nada: com o terceiro pedido em estado errado, os dois primeiros já
saíram e o motoboy já está na rua — desfazer isso seria mentir para ele.

### 0.3 Autorização do entregador

**O que existe.** `AdminScope` e `exigir_papel` moram em
`api/dependencies/admin_scope.py` e valem para TODA rota `/admin`, via
`get_current_admin` (Bearer de `admin_users`). `print_agent` é um
`admin_user` de máquina, preso a uma filial, com quatro rotas.
`tests/test_papeis_das_rotas.py` cobra papel de toda rota `/admin`, e
`scripts/escopo_das_rotas.py` cobra escopo de toda rota `/admin`.

**Decisão: o entregador não é `admin_user`, não tem Bearer, não tem JWT.**
Ele é uma linha em `couriers` com duas credenciais, as duas em hash:

| Credencial | Onde viaja | Como é guardada | Por quê |
|---|---|---|---|
| link (`secrets.token_urlsafe(32)`, 256 bits) | no caminho: `/courier/{link_token}/...` | `sha256` sem chave, igual ao `tracking_token` | entrada de 256 bits não precisa de chave; e não existe variável de ambiente cuja perda mate todo link |
| código (6 dígitos) | cabeçalho `X-Courier-Code`, nunca na URL | `HMAC(chave = link_token, msg = código)` | 6 dígitos precisam de chave contra força bruta num dump; a chave é o próprio link, que o dump não tem — sem env var nova |

O par (link, código) vai em **toda requisição**. Sem sessão, sem token
derivado: é isso que faz "**o link velho morre na hora**" ser verdade
literal — regenerar troca os dois hashes, e a próxima requisição do link
antigo é 404. Um JWT emitido a partir do par sobreviveria à regeneração até
expirar, ou exigiria consultar o banco a cada requisição — que é o que já
fazemos, sem o JWT.

**Segredo se compara com `compare_digest`** (armadilha 18): o link pelo
`WHERE` no hash mais a reconferência, como `get_order_by_tracking_token`; o
código só por `compare_digest`.

**Como convive com `AdminScope`, papéis e `print_agent`:**

- **rota nenhuma de `/admin` aceita o par**, e rota nenhuma de `/courier`
  aceita Bearer. São dependências diferentes (`get_current_courier` ×
  `get_current_admin`), routers diferentes, prefixos diferentes;
- **`print_agent` não alcança nada novo**: as rotas `/admin/couriers` são
  PESSOAS/GERENCIA/SOMENTE_DONO, e `test_o_agente_de_impressao_alcanca_exatamente...`
  continua com as mesmas quatro;
- **escopo do entregador é a atribuição**: toda consulta filtra por
  `courier_id` (e o pedido é da `branch_id` do cadastro). Ele não enxerga
  pedido não atribuído nem da loja vizinha, e não há parâmetro de filial ou
  restaurante em rota nenhuma dele — não há o que conferir porque não há o que
  passar;
- **varredura de escopo**: `scripts/escopo_das_rotas.py` ganha a dimensão
  `/courier` — toda rota recebe `Courier` por dependência, nenhuma aceita
  `restaurant_id`/`branch_id`/`courier_id`, e a cadeia de chamadas passa
  `courier.id` para a consulta. Iscas plantadas em
  `tests/test_escopo_das_rotas.py`. Ferramenta ANTES das rotas.

**Regenerar**: `POST /admin/couriers/{id}/access` sorteia link e código
novos, grava os hashes e devolve os dois em claro **uma vez** (como a senha
temporária de lojista). Não há rota que os devolva de novo.

**Resíduo conhecido (fase 2)**: com o link vazado, o código de 6 dígitos só
tem o rate limit por IP contra força bruta online (10/min, 60/h). Um contador
de falhas por entregador com trava temporária é a próxima camada — no banco,
nunca em memória (armadilha 20). Não construído agora porque no piloto o link
vai por WhatsApp para três pessoas conhecidas.

### O que essas respostas mudam no desenho pedido: nada

Três decisões que não estavam escritas no pedido e ficam registradas:

- **exclusão é `deleted_at`, não `DELETE`**: `courier_assignments` referencia
  `couriers`, e o histórico (que é o que o dono usa para PAGAR) tem que
  sobreviver ao motoboy que saiu. Excluído some de toda lista e perde o acesso
  na hora; inativo continua listado (férias) e perde o acesso também;
- **um motoboy que serve duas filiais tem dois cadastros** (dois links);
- **o par viaja em toda requisição**, sem sessão.

---

## 1.1 Restaurante ou filial: FILIAL

O critério do sistema é "de quem é o objeto físico". Setor de impressão,
agente, forma de pagamento, horário e as cinco colunas do frete são da filial;
cardápio virou da filial em `0026`. O motoboy sai de UMA cozinha, a taxa dele
é configuração daquela loja, e o gerente preso a uma filial precisa ver só os
motoboys dela — o que `AdminScope.resolve_branch_filter` já resolve se a
tabela tiver `branch_id`.

`restaurant_id` vai junto (como em `orders`), para o `WHERE` do repositório
barrar a filial de outro restaurante sem junção.

---

## 2. Fora da fase 1 — o que cada um exigiria

| Item | O que exigiria |
|---|---|
| Roteirização no mapa | coordenadas do pedido já existem (`delivery_latitude/longitude`); falta chamada ao Google Routes com waypoints (paga, por rota), ordem sugerida gravada na atribuição, e tela de mapa. Custo por corrida, não por pedido |
| Despacho automático | regra de escolha (menos pedidos abertos? mais perto?), o que exige localização do motoboy — ver abaixo — ou fila simples por ordem de cadastro. E um botão de "desfazer" no painel, porque a regra erra |
| Agrupamento de pedidos | conceito de "corrida" (várias atribuições sob um id, com ordem). Hoje a atribuição é por pedido; o motoboy já leva vários, só não há objeto que os junte |
| Localização do entregador | **navegador não rastreia com a tela apagada.** Qualquer coisa nessa direção é "última posição conhecida" quando o app estava aberto — nunca tempo real, nunca promessa de "onde ele está agora". Exigiria `POST /courier/.../position` (lat/lng, timestamp), coluna na atribuição, retenção curta (dado pessoal, armadilha 38) e a tela dizendo "visto às 19h42" |
| Notificação push | não existe notificação de status para o CLIENTE em canal nenhum hoje (nem push, nem SMS, nem WhatsApp). O app lê o status por `GET .../track/{token}` (polling). "Saiu para entrega" **já aparece** nesse GET e no `status_history`. Push exigiria service worker no app, tabela de subscrições por cliente, chave VAPID e um disparo no `OrderStatusChangeService.apply` |
| Auto-atribuição por QR Code | QR na comanda com um token de atribuição por pedido, rota `POST /courier/{link}/claim/{token}`; e decidir o que acontece quando dois leem o mesmo |
| Mapa de calor | agregação de `orders.delivery_latitude/longitude` por período, com grade; é relatório, não mudança de modelo |
| Métricas de tempo médio | `order_status_history` já tem os instantes (`ready`, `out_for_delivery`, `completed`); é uma consulta de relatório por entregador. Fácil, mas fica para quando houver dado |
| Tentativas fora da área | estado "não entregue" com motivo (cliente não atendeu, endereço errado) — é aresta NOVA na máquina de estados (`out_for_delivery → delivery_failed → ?`) e é aí que a frente da máquina de estados abre de verdade |

---

## 1. O schema

Revisão `20260903_0045`: `couriers`, `courier_assignments` e as duas colunas
em `branches`. Decisões que não estão no docstring da revisão:

- **telefone só dígitos e único por filial entre os não excluídos** (índice
  parcial). O motoboy que saiu e voltou é cadastro novo; o antigo fica com o
  histórico;
- **`ux_courier_assignments_order_ativa`** é o que faz "um pedido, um
  motoboy": parcial em `unassigned_at IS NULL`, e há teste exigindo que a
  reatribuição continue possível — sem essa metade, um UNIQUE sem `WHERE`
  passaria no teste de recusa;
- **41 divergências ORM×schema, como antes.** As tabelas novas nasceram com
  o `nullable=` do model igual ao DDL, coluna a coluna.

Visto vermelho antes (`column "courier_fee_base" does not exist`), 11 verdes
depois em `tests/test_migracao_entregadores_db.py`. Diagrama ER ganhou a
seção "O entregador, que é da filial".

## 2. A ferramenta, antes das rotas

`scripts/escopo_das_rotas.py` ganhou a quarta pergunta: toda rota `/courier`
recebe `Courier` por dependência, não aceita `restaurant_id`/`branch_id`/
`courier_id`, e a cadeia passa `courier.id` a alguma consulta. Exceção com
motivo em `SEM_CONSULTA_DO_ENTREGADOR_ESPERADA` (só o `/me`).

**A isca que importa** é a rota que recorta por `courier.branch_id`: parece
escopo e é a loja inteira. O varredor a acusa. Mutação (cegar a expressão)
deixou `test_o_padrao_legitimo_do_entregador_nao_e_acusado` vermelho — a
metade que prova que ele enxerga.

O que ainda falta e entra no item 6: a anti-vacuidade sobre o app real
(`len(rotas /courier) > 0`), que hoje seria vermelha porque a ferramenta veio
antes das rotas.

**Lição desta rodada:** `git checkout <arquivo>` para desfazer uma mutação
reverte o patch inteiro não commitado. Mutação se desfaz com o `sed` inverso,
ou se faz depois do commit.

## 3. A taxa da filial

`GET` (GERENCIA) e `PATCH` (SOMENTE_DONO) em `/admin/branches/{id}/courier-fee`.
A fórmula é `calculate_courier_fee` em `src/services/courier_fee.py`, pura:
nulo+nulo → nulo; só base → base; sem distância → só a base; só por-km sem
distância → nulo. `AdminCourierService._get_branch` copia as duas
conferências de `AdminSettingsService._get_branch` (a varredura exige as
duas).

Vermelho visto: coleta interrompida (módulos inexistentes) e, com os módulos
de pé, `test_as_rotas_estao_registradas` — que é o vermelho que vale para
"a rota existe". 17 verdes depois. `openapi.json` regenerado.

## 4. O cadastro e o código

Seis rotas em `/admin/couriers`. Lista e leitura PESSOAS (o atendente atribui
e precisa da lista); criar, editar, excluir e gerar acesso GERENCIA — o
código é credencial e não sai da senha que mais circula.

Decisões que só aparecem no código:

- **desativar fecha as corridas abertas** (`mark_open_assignments_unassigned`)
  e o painel reatribui. Inativo não consegue marcar nada, e um pedido preso
  com ele só apareceria como "atribuído". Reativar não reabre nada e **não
  recria o acesso** — o par gerado antes continua valendo;
- **excluir** = `deleted_at` + `is_active=false` + hashes nulos + corridas
  fechadas. O telefone fica livre (índice parcial);
- **o HMAC do código usa o link como chave** (`hash_courier_access_code`). Há
  teste provando que o mesmo código com outro link não confere — é o que
  faz um dump sem o link não servir para força bruta. Nenhuma env var nova;
- **inativo não ganha acesso** (409): seria um par que a dependência
  recusaria, e o dono veria "funcionou" numa tela e "não entra" na outra;
- nomes de método do repositório seguem os prefixos que
  `escrita_e_transacao.py` classifica (`exists_`, `mark_`, `create`,
  `list_`, `get_`) — nome fora da lista vira achado.

Vermelho visto: `ImportError` na coleta (o vermelho de "não existe") e, com o
módulo de pé, as rotas não registradas. 37 verdes rápidos + 9 de banco
(`test_entregadores_repository_db.py`, o WHERE de verdade).

## 5. Atribuir e desatribuir

Quatro rotas, todas PESSOAS (é trabalho do balcão, como mover o pedido):

- `POST /admin/couriers/{id}/assignments` `{order_ids: [...]}` — **resposta
  por item, escrita uma só**. Códigos: `not_found` (fora do escopo, os três
  casos no mesmo código), `not_delivery`, `order_closed` (terminal),
  `other_branch` (o motoboy do Centro não sai com o pedido da Aldeota — o
  dono enxerga os dois, por isso não é 404). Reatribuir = fecha a anterior e
  abre outra com a taxa de agora; ao mesmo motoboy = no-op; `out_for_delivery`
  ainda troca de motoboy (a moto quebrou);
- `GET /admin/couriers/{id}/assignments` — as abertas dele;
- `GET /admin/orders/{id}/courier` — quem está com o pedido; os dois campos
  nulos = ninguém ainda (estado normal, não 404);
- `DELETE /admin/orders/{id}/courier` — 204; 409 se ninguém está com ele.

O snapshot da taxa nasce aqui: `calculate_courier_fee(base, per_km,
orders.delivery_distance_km)`. Teste de banco prova que a taxa alterada
depois não muda a corrida, e que o índice parcial convive com a
reatribuição (fecha antes de abrir, na mesma transação).

**O que ficou para o painel decidir:** o motoboy NÃO aparece em
`AdminOrderListItem` (seria uma junção na lista e no evento SSE). Por pedido
há o `GET .../courier`; por motoboy há a lista de abertas. Se a tela precisar
do nome na listagem, é fase 2 (junção em `list_orders_by_restaurant`).

Vermelho visto: `ImportError` e rotas não registradas. 22 rápidos + 2 de
banco verdes.

## 6. A porta do entregador

Cinco rotas em `/courier/{link_token}/...`, todas com `X-Courier-Code` no
cabeçalho e rate limit `30/minute;600/hour` por IP (a barreira contra força
bruta dos seis dígitos: um milhão de combinações a 600/h são mais de dois
meses por IP):

| Rota | O quê |
|---|---|
| `GET .../me` | quem é, de que loja — a rota que o app usa para conferir o código antes de guardá-lo |
| `GET .../orders` | os abertos dele (não terminais), com endereço, telefone, pagamento, `is_paid`, `amount_to_collect`, `can_leave`/`can_deliver` |
| `POST .../orders/out-for-delivery` `{order_ids}` | por item: `not_found` / `wrong_status` (+`message`); os `ok` já saíram |
| `POST .../orders/{id}/delivered` | 409 se não saiu; 404 se não é dele (inclusive já entregue) |
| `GET .../history?start_date&end_date` | entregas concluídas e soma das taxas; sem datas = hoje no fuso da operação; até 92 dias; `deliveries_without_fee` separado |

**A quarta porta do writer.** `CourierDeliveryService._apply` chama
`OrderStatusChangeService.apply` com `changed_by="entregador:<nome>"`,
`requester="entregador:<id>"` e rota própria. A regra da porta é
`ensure_courier_can_set` em `order_state_machine.py`, sobre a tabela
`COURIER_TRANSITIONS = {ready: out_for_delivery, out_for_delivery: completed}`
— só diz quais arestas EXISTENTES a porta usa; o grafo não mudou. O
cashback é creditado no `completed` de graça (há teste com o dublê do
`cashback_service` registrando a chamada, e o e2e mostra os dois eventos
assinados `entregador:Zé` no histórico que o painel lê).

**Códigos da credencial:** 404 para link desconhecido, regenerado, inativo
ou excluído (mesma frase — o link "morre" sem dizer por quê); 401 para
código ausente ou errado (o app distingue "peça outro link" de "digite de
novo"). Bearer de lojista não abre `/courier` e o par não abre `/admin`
(teste e2e `test_as_duas_credenciais_nao_se_cruzam`).

**Duas mudanças no repositório que a porta exigiu:**

- `mark_open_assignments_unassigned` recebe `except_order_statuses`
  (os terminais, passados pelo service — "quem sabe o que é terminal é a
  máquina de estados"). Sem isso, desativar um motoboy fecharia as
  atribuições dos pedidos JÁ ENTREGUES e apagaria as entregas do histórico
  que o dono usa para pagar;
- `list_deliveries_by_courier` lê o instante da linha `completed` em
  `order_status_history` (não `orders.updated_at`, que muda por qualquer
  escrita) e só conta a atribuição ABERTA — a de quem estava com o pedido
  quando ele foi entregue.

**O varredor de escopo precisou de um critério a mais:** a cadeia das duas
rotas de escrita passa pelo writer e ultrapassa `PROFUNDIDADE_MAXIMA`. Para
a dimensão do entregador a pergunta termina na consulta recortada por
`courier.id`; o que vem depois é a cadeia que as três portas do painel já
percorrem. `completo = completo or entregador`, com o motivo escrito no
script. Anti-vacuidade sobre o app real: `>= 5` rotas `/courier`.

Vermelho visto: `ImportError` na coleta, depois as rotas não registradas.
Verdes: 40 rápidos + 5 e2e de banco. **Portão: 3126 verdes** (2475 rápidos
+ 651 `db`), ruff limpo, `openapi.json` regenerado, 41 divergências
ORM×schema (inalterado), varreduras em 0.

## Portão

```
venv/Scripts/python -m pytest -q -m "not db"
docker compose -f docker-compose.test.yml up -d
venv/Scripts/python -m pytest -q -m db
venv/Scripts/ruff check .
venv/Scripts/python scripts/export_openapi.py
venv/Scripts/python scripts/escopo_das_rotas.py
venv/Scripts/python scripts/escrita_e_transacao.py
venv/Scripts/python scripts/dubles_de_dado.py
```
