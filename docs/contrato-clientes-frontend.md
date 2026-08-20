# Contrato da tela de Clientes do painel

Documento para quem vai implementar o painel do lojista. Tudo o que é preciso
saber está aqui; não é necessário acesso ao código do backend.

**Estado em 20/08/2026.** Rota: `GET /admin/customers`. Autenticação: token de
lojista, papel **owner** ou **manager** (atendente e agente de impressão
recebem **403** — a rota entrega nome e telefone da base inteira, e é a que
mais pesa numa senha vazada).

> **Novo em 20/08/2026.** A resposta ganhou quatro campos:
> `billable_orders_count`, `average_ticket`, `days_since_last_order` e
> `segment`. Nada foi removido nem renomeado — quem já consome a rota continua
> funcionando sem tocar em nada.

---

## 1. Os campos novos

| Campo | Tipo | O que é |
|---|---|---|
| `billable_orders_count` | int | pedidos que viraram dinheiro: `orders_count` **menos** cancelado e recusado |
| `average_ticket` | float | `total_spent / billable_orders_count`; **0.0** quando não há pedido faturável |
| `days_since_last_order` | int \| null | dias inteiros desde o último pedido; `null` só se não houver data |
| `segment` | enum | a classificação RFV — os cinco valores estão na §2 |

**Por que `billable_orders_count` vai na tela, e não só nos bastidores.** Sem
ele os três números não fecham e o lojista abre chamado: *"5 pedidos, R$ 160
gastos, ticket médio R$ 40 — a conta não bate"*. Bate: dois dos cinco pedidos
foram cancelados. Mostre os dois contadores, ou mostre o ticket médio ao lado
de `billable_orders_count` em vez de `orders_count`.

`total_spent` **nunca** somou cancelado. Isso não mudou.

---

## 2. Os cinco rótulos

O backend manda **o código**, em minúsculas e sem acento. **Não existe
`segment_label`**, e é decisão: rótulo em português vindo da API transformaria
mudança de texto de tela em deploy de backend. O texto visível é do painel.

| `segment` | Sugestão de rótulo | Como ler |
|---|---|---|
| `novo` | Novo | relacionamento recente — **primeiro** pedido dentro dos últimos 30 dias |
| `ocasional` | Ocasional | poucos pedidos, relacionamento antigo, mas **em dia** com o ritmo dele |
| `fiel` | Fiel | três pedidos ou mais, e dentro do ritmo dele |
| `em_risco` | Em risco | passou o **dobro** do intervalo habitual dele sem aparecer |
| `perdido` | Perdido | passou o **quádruplo** |

**A armadilha de leitura é `novo`.** Ele conta do **primeiro** pedido, não do
último. Cliente com dois pedidos espaçados em dez meses que pediu semana
passada é `ocasional`, e não `novo` — chamá-lo de novo faria a tela mentir na
primeira leitura de quem a abre.

Os valores saem no `openapi.json` como enum (`CustomerSegment`): gere o tipo a
partir do documento em vez de escrever as cinco strings à mão.

---

## 3. O ritmo é de cada cliente, não do restaurante

Esta é a parte que vale explicar ao lojista quando ele perguntar por que o
vizinho da lista tem rótulo diferente com o mesmo tempo sem pedir.

O backend mede o intervalo médio **daquele cliente**:

```
cadência = (último pedido − primeiro pedido) / (nº de pedidos − 1)
```

E compara o silêncio atual com ela:

| Cliente | Cadência medida | 16 dias sem pedir |
|---|---|---|
| pede toda semana | 7 dias | **em risco** (passou de 14) |
| pede uma vez por mês | 30 dias | **fiel** (o limite dele é 60) |

Uma faixa fixa não consegue dizer as duas coisas, e uma faixa por restaurante
também não — os dois clientes acima são do **mesmo** restaurante.

A cadência é grampeada entre **7 e 60 dias**, e quem tem um pedido só (não há
intervalo a medir) vale **30**. Os seis valores são configuráveis por ambiente
(`RFV_*`); os defaults são os desta página.

---

## 4. A classificação é do RECORTE da consulta

**Sem `branch_id`, é do restaurante. Com `branch_id`, é daquela filial.** E
gerente preso a uma filial recebe sempre o recorte dela, sem precisar pedir.

Consequência que **vai** gerar chamado, e é melhor a tela se antecipar:

> O mesmo cliente pode ser **`fiel`** no restaurante e **`perdido`** na filial.
> As duas leituras estão certas.

Quem pede toda semana no Centro e passou uma vez na Aldeota há seis meses está
perdido **para a Aldeota** — e é a leitura útil, porque quem chama de volta é
uma loja. **Deixe visível na tela qual recorte está ativo**, ao lado do rótulo.

---

## 5. Exemplo de resposta

```json
{
  "items": [
    {
      "customer_name": "Maria Silva",
      "customer_phone": "85999990000",
      "orders_count": 13,
      "billable_orders_count": 12,
      "total_spent": 780.50,
      "average_ticket": 65.04,
      "first_order_at": "2026-05-24T19:12:00Z",
      "last_order_at": "2026-08-17T20:03:00Z",
      "days_since_last_order": 3,
      "segment": "fiel"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

Os parâmetros continuam os mesmos: `branch_id`, `search` (telefone só com
dígitos, ou parte do nome), `limit` (1–100, padrão 50) e `offset`. A ordem é
do pedido mais recente para o mais antigo.

---

## 6. O que esta rota NÃO faz

- **Não filtra por `segment`.** Não existe `?segment=em_risco`. A
  classificação é calculada sobre a página já paginada, então filtrar aqui
  devolveria página com três linhas. Quando essa tela for pedida, a regra
  precisa descer para o SQL — é trabalho de backend, não dá para contornar no
  painel filtrando o array recebido (você estaria filtrando 50 linhas, não a
  base).
- **Não ordena por `segment` nem por `average_ticket`.** Só pelo último
  pedido.
- **Não avisa quando alguém muda de classe.** O rótulo é derivado na leitura;
  não há evento, não há histórico e não há "entrou em risco hoje".
- **Não dispara nada.** O gatilho de reativação não existe ainda, e quando
  existir passa por `marketing_opt_in` do cadastro — que esta rota nem lê.
- **Não devolve e-mail, CPF, data de nascimento nem `customer_id`.** São do
  cadastro global da plataforma; o lojista é dono do que o cliente informou ao
  pedir na loja dele, e isso é nome e telefone.
