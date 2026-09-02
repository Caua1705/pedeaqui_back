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
| 7 | Docs, contrato para o painel e para o app, fase 2 | **feito** — `docs/entregadores.md` |

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

## 7. Docs e o relatório

`docs/entregadores.md` (novo), `docs/arquitetura.md` (quatro portas, tabela
de quem cancela, índice), `docs/autenticacao-e-escopo.md` (§3.1, o par),
`docs/modelo-de-dados.md` (diagrama, feito no item 1) e a armadilha 25.1 da
skill (quatro portas).

---

## RELATÓRIO

### O que ficou pronto

- **Schema** (revisão `20260903_0045`): `couriers`, `courier_assignments`,
  `branches.courier_fee_base/per_km`.
- **Painel** (`/admin`): taxa da filial (2 rotas), cadastro (6 rotas),
  atribuição (4 rotas). Papéis na tabela de `test_papeis_das_rotas.py`.
- **Entregador** (`/courier/{link_token}/...`): 5 rotas, link + código em
  toda requisição, rate limit por IP.
- **Máquina de estados:** quarta porta do writer, `ensure_courier_can_set`.
  Nenhuma aresta nova.
- **Ferramenta:** varredura de escopo cobre `/courier`, com iscas.
- **Portão:** 3126 verdes (2475 rápidos + 651 `db`), ruff limpo,
  `openapi.json` regenerado, 41 divergências ORM×schema (inalterado).
- **Notificação ao cliente ("saiu para entrega"):** NÃO existe canal de
  notificação de status para o cliente hoje. O evento aparece no `GET
  .../track/{token}` (status e histórico) e no SSE do painel. Pendência
  anotada, nada construído.

### Pronto para colar no painel

> **Entregadores — o que o painel precisa receber**
>
> Tudo com o Bearer de lojista de sempre. Nenhuma rota aceita `restaurant_id`;
> `branch_id` só onde indicado, e o escopo de filial do token vale como no
> resto.
>
> **Taxa que a loja paga por corrida (por filial)**
>
> | Rota | Papel | Corpo / resposta |
> |---|---|---|
> | `GET /admin/branches/{branch_id}/courier-fee` | GERENCIA | `{branch_id, courier_fee_base: float\|null, courier_fee_per_km: float\|null}` |
> | `PATCH /admin/branches/{branch_id}/courier-fee` | SOMENTE_DONO | `{courier_fee_base?: number\|null, courier_fee_per_km?: number\|null}` — ausente não mexe, `null` apaga |
>
> `null` nos dois = "sem taxa configurada" (as corridas passam a ter taxa
> nula, não zero). Motoboy pago por corrida: `courier_fee_base` preenchida e
> `courier_fee_per_km: 0`. **Não muda nada do que o cliente paga.**
>
> **Cadastro**
>
> | Rota | Papel | Corpo / resposta |
> |---|---|---|
> | `GET /admin/couriers?branch_id=` | PESSOAS | lista de `AdminCourierResponse`; inativos vêm, excluídos não |
> | `POST /admin/couriers` | GERENCIA | `{branch_id, name, phone}` → 201 `AdminCourierResponse`. 409 telefone repetido na filial; 404 filial fora do escopo |
> | `GET /admin/couriers/{courier_id}` | PESSOAS | `AdminCourierResponse` |
> | `PATCH /admin/couriers/{courier_id}` | GERENCIA | `{name?, phone?, is_active?}` parcial |
> | `DELETE /admin/couriers/{courier_id}` | GERENCIA | 204 |
> | `POST /admin/couriers/{courier_id}/access` | GERENCIA | → `{courier_id, link_token, access_code, access_generated_at}` |
>
> `AdminCourierResponse = {id, branch_id, name, phone, is_active, has_access,
> access_generated_at, created_at}`. `phone` volta só com dígitos.
>
> **O acesso sai UMA vez.** A resposta do `POST .../access` é a única vez
> que `link_token` e `access_code` existem em claro. Mostrem os dois na tela
> com "copiar", e montem a URL do app do entregador com o token:
> `https://<app-do-entregador>/entregador/{link_token}` (o caminho é de vocês;
> o backend só conhece o token). Chamar de novo **regenera**: novo par, e o
> antigo morre na hora — é o botão "o motoboy saiu / perdeu o celular".
> Entregador inativo responde 409.
>
> `is_active: false` tira o acesso na hora e **devolve os pedidos abertos
> dele para a fila** (as atribuições são fechadas; os já entregues ficam no
> histórico). `true` de volta não recria o acesso: o par antigo continua
> valendo. `DELETE` é exclusão lógica: some das listas, perde o acesso,
> libera o telefone, mantém o histórico.
>
> **Atribuição**
>
> | Rota | Papel | Corpo / resposta |
> |---|---|---|
> | `POST /admin/couriers/{courier_id}/assignments` | PESSOAS | `{order_ids: [uuid, ...]}` (1–50) → `{items: [{order_id, ok, error?, assignment?}]}` |
> | `GET /admin/couriers/{courier_id}/assignments` | PESSOAS | lista de `AdminAssignmentResponse` (as abertas, não terminais) |
> | `GET /admin/orders/{order_id}/courier` | PESSOAS | `{assignment: AdminAssignmentResponse\|null, courier: AdminCourierResponse\|null}` — os dois nulos = ninguém ainda (200, não 404) |
> | `DELETE /admin/orders/{order_id}/courier` | PESSOAS | 204; 409 se ninguém está com ele |
>
> `AdminAssignmentResponse = {id, order_id, order_number, order_status,
> courier_id, assigned_at, courier_fee_snapshot: float\|null,
> distance_km_snapshot: float\|null}`.
>
> **O lote responde por item, na ordem do corpo.** `error` ∈ `not_found`
> (pedido fora do escopo), `not_delivery` (retirada), `order_closed`
> (terminal), `other_branch` (o motoboy é de outra filial). Os `ok` são
> gravados juntos; os outros não. A rota só dá erro HTTP para o entregador
> em si (404 fora do escopo, 409 inativo).
>
> **Reatribuir é chamar o POST com outro entregador** — a anterior fecha e
> a nova abre com a taxa de agora. Mesmo entregador de novo: `ok`, sem
> mudar nada. Pedido em `out_for_delivery` ainda troca de motoboy.
>
> **O que NÃO está na listagem de pedidos:** `AdminOrderListItem` não ganhou
> o nome do motoboy (seria junção na lista e no evento SSE). Por pedido há o
> `GET .../courier`; por motoboy há a lista de abertas. Se a tela precisar
> do nome na listagem, é a primeira coisa da fase 2.
>
> **`openapi.json` está regenerado** com tudo isto, inclusive os enums de
> erro.

### Pronto para colar no app do entregador

> **App do entregador — o contrato**
>
> É uma tela nova, e **deve morar no repositório do front do cliente (o
> app)**, não no painel e não num repositório separado. Três motivos:
>
> 1. **é a mesma natureza técnica**: uma página web pública, aberta por link,
>    sem login de painel — exatamente como `/acompanhar/{tracking_token}` já
>    é. O painel é uma SPA autenticada por Bearer de lojista, e o entregador
>    nunca terá esse Bearer;
> 2. **compartilha o cliente HTTP e os tipos gerados do `openapi.json`**, que
>    o front já consome; um repositório separado seria uma terceira cópia do
>    gerador para cinco rotas;
> 3. **é a rota `/entregador/{link_token}`** ao lado das rotas públicas do
>    app. Deploy e domínio já existem.
>
> Se um dia virar PWA instalável com push, a decisão pode ser revista — mas
> push é fase 2 e nada aqui pede instalação.
>
> **Como autentica:** o link traz o `link_token`; a tela pede o código de 6
> dígitos **uma vez**, chama `GET /courier/{link_token}/me` com o cabeçalho
> `X-Courier-Code: <código>`; com 200, guarda os dois no `localStorage` e
> manda o cabeçalho em **toda** requisição. Não há sessão nem token
> derivado.
>
> | Código | Quando | O que mostrar |
> |---|---|---|
> | 404 `Link inválido` | link desconhecido, regenerado, entregador inativo ou excluído | "Este link não vale mais. Peça um novo ao restaurante." e apague o `localStorage` |
> | 401 | código ausente ou errado | "Código incorreto" e peça de novo |
> | 429 | 30/min ou 600/h por IP | tente mais tarde |
>
> **As rotas** (todas `/courier/{link_token}/...`, todas com o cabeçalho):
>
> | Rota | Resposta |
> |---|---|
> | `GET .../me` | `{name, branch_name}` |
> | `GET .../orders` | lista de `CourierOrderResponse`, do mais antigo ao mais novo, só os abertos |
> | `POST .../orders/out-for-delivery` `{order_ids: [...]}` | `{items: [{order_id, ok, error?, message?, order?}]}` |
> | `POST .../orders/{order_id}/delivered` | `CourierOrderResponse` já em `completed`; 409 se ainda não saiu; 404 se não é dele |
> | `GET .../history?start_date&end_date` | `{start_date, end_date, deliveries_count, deliveries_without_fee, fee_total, deliveries: [{order_id, order_number, delivered_at, address_neighborhood, distance_km, courier_fee}]}` |
>
> `CourierOrderResponse = {order_id, order_number, status, can_leave,
> can_deliver, customer_name, customer_phone, address_street,
> address_number, address_neighborhood, address_complement,
> address_reference, address_city, delivery_latitude, delivery_longitude,
> notes, payment_method, is_paid, amount_to_collect, total, courier_fee,
> assigned_at, created_at}`.
>
> - **`can_leave` / `can_deliver` decidem o botão.** Pedido em preparo
>   aparece na lista (para ele saber o que vem) mas nenhum dos dois é
>   verdadeiro. Não deduzam do `status`; usem os dois campos.
> - **`amount_to_collect` é o que receber na porta**: o total só quando o
>   pedido é pago na entrega; pago online é `0` e `is_paid: true`.
> - **`courier_fee` é a taxa DELE nesta corrida**, congelada na atribuição;
>   `null` = a loja não configurou taxa. No histórico, `fee_total` soma só
>   as que têm valor, e `deliveries_without_fee` conta as outras — é o
>   número para ele levar ao dono.
> - **"Saiu para entrega" aceita vários de uma vez** e responde por item.
>   Os `ok` já saíram mesmo que outro tenha falhado — não desfaçam nada na
>   tela; mostrem o `message` dos que falharam.
> - **Histórico sem datas é hoje** (fuso de Fortaleza). Até 92 dias.
> - O cliente vê "saiu para entrega" no acompanhamento dele no próximo poll.
>   Não há push.

### O que ficou para a fase 2

Registrado com o que cada um exigiria em `docs/entregadores.md` §8 e na
seção 2 deste arquivo: roteirização, despacho automático, agrupamento,
**localização (só "última posição conhecida", nunca tempo real)**, push,
QR Code, mapa de calor, tempo médio, tentativas fora da área — mais três
que apareceram construindo: motoboy na listagem de pedidos do painel,
relatório de taxas por entregador para o dono, e trava por falhas de
código.

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
