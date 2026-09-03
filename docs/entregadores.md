# Entregadores

Cobre `services/admin_courier_service.py`, `services/courier_delivery_service.py`,
`services/courier_fee.py`, `api/dependencies/courier_auth.py` e as revisões
`20260903_0045` e `20260904_0047`. Fase 1: cadastro, código de acesso, atribuição, a tela do
motoboy (lista, saiu, entregue, histórico) e a taxa que a loja paga por
corrida.

O contexto que moldou tudo: no piloto os motoboys são **fixos**. O cadastro
não precisa ser instantâneo e o código é gerado uma vez e raramente trocado.
Otimizado para durar, não para cadastrar rápido.

---

## 1. O que o entregador É — e o que ele não é

**Não é `admin_user`.** Não tem tela no painel, não tem Bearer, não alcança
rota nenhuma de `/admin`. É uma linha em `couriers`, da **filial** (pelo
critério do resto do sistema: setor de impressão, agente, forma de pagamento
— objeto físico da loja). Quem serve duas lojas tem dois cadastros.

**Não instala app.** Abre um link individual e digita um código curto uma
vez; o app do entregador guarda os dois e os manda em toda requisição.

**Não é papel de `admin_scope.py`.** `PESSOAS`, `GERENCIA` e `SOMENTE_DONO`
continuam sendo os papéis do painel; o entregador é outro público, com outra
dependência (`get_current_courier`) e outro prefixo (`/courier`). O agente de
impressão (`print_agent`) continua alcançando as quatro rotas de sempre e
nenhuma destas.

---

## 2. As credenciais: o par link + código

| Credencial | Onde viaja | Como é guardada | Por quê |
|---|---|---|---|
| link (`secrets.token_urlsafe(32)`, 256 bits) | no caminho: `/courier/{link_token}/...` | `sha256` sem chave, igual ao `tracking_token` | entrada de 256 bits não precisa de chave; e não existe variável de ambiente cuja perda mate todo link |
| código (6 dígitos) | cabeçalho `X-Courier-Code`, nunca na URL | `HMAC(chave = link_token, msg = código)` | 6 dígitos precisam de chave contra força bruta num dump; a chave é o próprio link, que o dump não tem — sem env var nova |

**O par vai em toda requisição. Sem sessão, sem JWT.** É isso que faz
"regenerar mata o link velho na hora" ser verdade literal: `POST
/admin/couriers/{id}/access` troca os dois hashes e a próxima requisição do
par antigo é 404. Um JWT derivado sobreviveria à regeneração até expirar, ou
exigiria consultar o banco a cada requisição — que é o que já fazemos.

**Os códigos HTTP são contrato do app:**

| Código | Quando | O que o app faz |
|---|---|---|
| 404 `Link inválido` | link desconhecido, regenerado, entregador inativo ou excluído — **mesma frase para os quatro** | "peça um link novo ao restaurante" |
| 401 | código ausente ou errado | "digite o código de novo" |
| 429 | cadastro travado por falhas (abaixo) | mostrar o prazo da frase, e a outra saída |

O 401 revela que o link existe, e isso é aceito: quem tem o link já sabe
disso.

**O acesso sai em claro UMA vez**, na resposta de `POST /admin/couriers/{id}/access`.
Não há rota que o devolva de novo; segunda via é gerar outro par. Mesmo
desenho da senha temporária de lojista.

### 2.1 A trava por falhas de código

**Cinco erros dentro de 15 minutos travam o cadastro por 15 minutos.** Três
colunas em `couriers` (`access_failed_attempts`, `access_failed_at`,
`access_blocked_until`, revisão `20260904_0047`) e a política inteira em
`CourierDeliveryService` — `MAX_ACCESS_ATTEMPTS` e `ACCESS_LOCK_WINDOW`.

**Por que não bastava o rate limit por IP.** `COURIER_RATE_LIMIT` conta
30/min e 600/h, e a conta antiga deste documento — "um milhão de combinações
a 600 por hora são mais de dois meses" — vale para **um** IP. Quem tem o link
não está preso a um: proxy residencial, 4G, outro café. O balde do rate limit
é o IP; o balde da trava é o **entregador**, e trocar de rede não o esvazia.
Cinco a cada quinze minutos são 480 por dia, e um milhão de combinações viram
anos.

**O contador só anda para quem já passou pelo link.** A conferência do link
vem antes, e link desconhecido é 404 sem tocar em coluna nenhuma. Quem faz o
contador subir é o motoboy digitando errado, ou alguém que roubou o link.

Quatro decisões que parecem detalhe e não são:

- **a trava vem ANTES de conferir o código, e durante ela nem o código certo
  abre.** Deixar a tentativa certa passar devolveria justamente a resposta que
  a força bruta procura — a trava só trava se valer para ela também;
- **falha isolada não acumula.** Com a última mais velha que a janela, o
  contador recomeça em 1. Sem isso, dois erros na segunda e três na sexta
  somariam cinco, e o motoboy ficaria de fora no meio do turno por digitação
  de semanas diferentes;
- **é fixa, sem escalada.** Dobrar a trava a cada reincidência castiga o
  motoboy que erra de novo depois de destravar; o atacante só espera. O teto
  fixo já compra os anos;
- **código AUSENTE não conta.** Não é chute: é app mal montado, ou a
  requisição que sai antes de a pessoa digitar. Contar isso travaria o motoboy
  por um bug do app.

**O que acontece quando o motoboy legítimo erra.** Quase nunca acontece em
turno: o app guarda o par e o manda sozinho, e ele digita o código **uma vez**,
na instalação. Se o painel regenerou o acesso, o **link** também mudou, e a
resposta é 404 ("peça um link novo") — que não encosta no contador. Quando
acontece, há duas saídas e **nenhuma delas está dentro do app**:

1. esperar (≤ 15 min; a frase do 429 diz quantos faltam);
2. **ligar para a loja.** `POST /admin/couriers/{id}/access` gera outro par e
   zera contador e trava na mesma escrita — destrava na hora, e de quebra mata
   o par que alguém esteve chutando. Para o dono saber que é isso,
   `AdminCourierResponse.access_blocked_until` mostra a trava **enquanto ela
   vale** (instante já passado sai nulo: publicar o cru faria o painel escrever
   "travado até 19h10" às 20h).

**O custo aceito:** quem tem o link pode travar o motoboy de propósito, 15
minutos por vez. Quem tem o link já é um problema maior, e a saída — regenerar
— mata o link dele junto.

Escrita e commit ficam em `authenticate`, que roda na **dependência**: a
requisição termina em 401, e sem commit próprio o `get_db` fecharia a sessão
sem gravar — a trava inteira viraria enfeite. No acerto só escreve quando há o
que zerar, senão a lista que o motoboy recarrega a cada parada viraria um
UPDATE em `couriers` por requisição.

---

## 3. A taxa do entregador

**Onde mora:** `branches.courier_fee_base` e `branches.courier_fee_per_km`,
no mesmo regime das cinco colunas do frete do cliente — só da filial, sem
herança, `NULL` = não configurado. Motoboy pago por corrida = `base`
preenchida e `per_km = 0`.

A referência (Cardápio Web) põe a taxa do entregador como campo da área de
entrega. Este projeto não tem área: tem raio com fórmula, e a taxa do
entregador espelha a fórmula em vez de inventar uma tabela de regiões.

**Onde ela entra em quem calcula frete: em lugar nenhum.**
`DeliveryEstimateService` não é tocado. A taxa é calculada **na atribuição**
(`AdminCourierService.assign_orders`), por `calculate_courier_fee` em
`services/courier_fee.py`, a partir do `orders.delivery_distance_km` que o
pedido já congelou, e gravada como **snapshot** em
`courier_assignments.courier_fee_snapshot`. Mudar a taxa amanhã não muda a
corrida de ontem. Reatribuir é uma linha nova com a taxa de agora.

```
base + per_km × distância           os dois configurados, distância conhecida
base                                sem distância (Google fora do ar), ou só a base
NULL                                nada configurado, ou só per_km sem distância
```

**Nulo nunca vira zero.** Zero é um número que soma no histórico que o dono
usa para pagar, e faria a corrida de uma filial que nunca configurou taxa
aparecer como "grátis" em vez de "sem taxa". O histórico do entregador
separa as duas em `deliveries_without_fee`.

**Nenhum número de dinheiro do cliente muda:** não entra em `cartTotals`, em
`orders.total`, na comissão nem na estimativa. É registro.

Quem escreve: `PATCH /admin/branches/{id}/courier-fee`, SOMENTE_DONO (é o
que a loja paga). Quem lê: GERENCIA (termo comercial, como o cupom).

---

## 4. A atribuição

`courier_assignments`: uma linha por atribuição, `unassigned_at` nulo =
ativa. O índice parcial único `order_id WHERE unassigned_at IS NULL` é o que
faz "um pedido, um motoboy". Reatribuir fecha a antiga e abre outra;
desatribuir fecha; o histórico guarda tudo com quem e quando.

`POST /admin/couriers/{id}/assignments` `{order_ids: [...]}` responde **por
item, na ordem do corpo**, e grava os `ok` juntos numa escrita só:

| `error` | Quando |
|---|---|
| `not_found` | não existe, é de outro restaurante, ou de filial que este lojista não enxerga — os três no mesmo código |
| `not_delivery` | retirada |
| `order_closed` | `completed`, `cancelled`, `rejected` |
| `other_branch` | o pedido é de outra filial que o lojista enxerga: o motoboy do Centro não sai com o pedido da Aldeota |

`out_for_delivery` ainda aceita troca de motoboy (a moto quebrou). Só os
terminais fecham a porta.

**O motoboy aparece na listagem de pedidos do painel** (`AdminOrderListItem.courier_id`
e `courier_name`, nulos quando ninguém está com o pedido) e no evento do
stream, que monta o mesmo item. É `Order.courier_assignment`, uma relação
`viewonly` para a atribuição aberta, carregada por `selectinload` nas três
consultas que alimentam o item — a página e os dois polls do stream. O
custo é fixo por página: uma consulta para as atribuições e uma para os
entregadores, qualquer que seja o tamanho da página.
`tests/test_entregador_na_listagem_db.py` conta as consultas.

**O dono vê quanto deve a cada motoboy** em `GET /admin/reports/couriers?start_date&end_date&branch_id`
(GERENCIA com recorte, como os relatórios de dinheiro de Desempenho): uma
linha por entregador com entregas concluídas no período, as sem taxa
separadas, e a soma. É a mesma definição de entrega do histórico que o
próprio motoboy vê (`CourierRepository.totals_by_courier` e
`list_deliveries_by_courier` leem a mesma atribuição aberta e a mesma linha
`completed`), então os dois números batem. Excluído entra, marcado.

**A atribuição de um pedido entregue continua aberta.** Ela é o registro de
quem entregou. Por isso desativar ou excluir um motoboy fecha as corridas
abertas **exceto as de pedido terminal** (`except_order_statuses`, passado
pelo service porque quem sabe o que é terminal é a máquina de estados) —
sem essa exceção, desativar apagaria as entregas do histórico.

---

## 5. A quarta porta do writer de status

`OrderStatusChangeService.apply` continua sendo a única escrita de
`orders.status`. O entregador é a quarta porta, depois do PATCH de status do
painel, do cancelamento do painel e do cancelamento pelo cliente.

O que a porta acrescenta é só o que é dela:

- **até onde ele anda:** `ensure_courier_can_set` em `order_state_machine.py`,
  sobre `COURIER_TRANSITIONS = {ready: out_for_delivery, out_for_delivery: completed}`.
  Não abre aresta nenhuma — só diz quais das existentes a porta usa. Nem
  cancelar, nem voltar;
- **quem autoriza:** a atribuição aberta com o `courier.id` dele. Pedido que
  não está com ele é 404 (inclusive o já entregue: terminal saiu da lista
  dele);
- **a assinatura:** `changed_by = "entregador:<nome>"`.

O que ele ganha do writer sem pedir: `ensure_payment_allows_order_status`,
o histórico, o crédito de cashback no `completed`, e o evento
`order.status_changed` no stream do painel.

**"Vários de uma vez"** (`POST .../orders/out-for-delivery`): cada pedido
passa pelo `apply` sozinho, uma transação por pedido, e a resposta é por
item. Não é tudo-ou-nada de propósito: com o terceiro pedido em estado
errado, os dois primeiros já saíram e o motoboy já está na rua.

---

## 6. O que "saiu para entrega" dispara

**Nada além do que já existia.** Não há notificação de status para o cliente
em canal nenhum (nem push, nem SMS, nem WhatsApp). O app do cliente lê o
status por `GET /restaurants/{slug}/orders/track/{token}` e vê
`out_for_delivery` no `status` e no `status_history` no próximo poll. O
painel recebe `order.status_changed` no SSE porque o stream lê
`order_status_history`. Push é pendência de fase 2 (ver §8).

---

## 7. Escopo: como a varredura cobra

`scripts/escopo_das_rotas.py` ganhou a quarta pergunta: toda rota `/courier`
recebe `Courier` por dependência, não aceita `restaurant_id`/`branch_id`/
`courier_id` do cliente, e a cadeia de chamadas passa `courier.id` a alguma
consulta. Exceção com motivo: só o `/me`. Para essa dimensão a cadeia é
considerada respondida ao chegar ao recorte — o que vem depois (o writer, o
cupom, o cashback) é o caminho que as portas do painel já percorrem, e ele
passa da profundidade máxima.

`tests/test_entregadores_e2e_db.py::test_as_duas_credenciais_nao_se_cruzam`
prova pelo HTTP que Bearer não abre `/courier` e o par não abre `/admin`.

---

## 8. Fora da fase 1, e o que cada um exigiria

| Item | O que exigiria |
|---|---|
| Roteirização no mapa | coordenadas do pedido já existem; falta rota com waypoints no Google (paga, por corrida), ordem gravada na atribuição, e tela de mapa |
| Despacho automático | regra de escolha, que exige localização (abaixo) ou fila simples; e um "desfazer" no painel |
| Agrupamento de pedidos | objeto "corrida" juntando várias atribuições com ordem |
| Localização do entregador | **navegador não rastreia com a tela apagada.** Qualquer coisa nessa direção é "última posição conhecida" quando o app estava aberto — nunca tempo real. Exigiria `POST /courier/.../position`, coluna na atribuição, retenção curta (dado pessoal) e a tela dizendo "visto às 19h42" |
| Notificação push | não existe canal de notificação para o cliente hoje. Exigiria service worker no app, tabela de subscrições, chave VAPID e um disparo no writer |
| Auto-atribuição por QR Code | token de atribuição por pedido na comanda, rota de "claim", e o que fazer quando dois leem o mesmo |
| Mapa de calor | agregação de `orders.delivery_latitude/longitude` por período |
| Métricas de tempo médio | `order_status_history` já tem os instantes; é consulta de relatório por entregador |
| Tentativas fora da área | estado "não entregue" com motivo — aresta NOVA no grafo, e aí a frente da máquina de estados abre de verdade |
