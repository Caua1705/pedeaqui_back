# Contrato da rota de escolha de filial

Documento para quem vai implementar o front. Tudo o que é preciso saber está
aqui; não é necessário acesso ao código do backend.

**Estado em 18/08/2026.** Base da API: a mesma do resto do app do cliente
(`https://api.pederapidex.com` em produção, `http://localhost:8000` em
desenvolvimento).

> **⚠️ Mudou em 20/08/2026 (revisões `20260820_0026`/`0027`): o cardápio
> passou a ser POR FILIAL.** A §8 deste documento diz que `/menu` devolve os
> mesmos produtos qualquer que seja a filial. **Não devolve mais.** O passo 3
> está em [`cardapio-por-filial.md`](cardapio-por-filial.md), e é ele que vale
> para produto, preço e disponibilidade. O resto deste documento — a rota de
> escolha de filial — continua correto.

> **Mudou em 18/08/2026 (revisão `20260818_0025`).** O "fechar agora" passou a
> ser de cada filial, e `is_open_now` agora combina a agenda da semana com essa
> pausa. Entrou o campo `closed_reason`, e `current_period` passou a poder vir
> preenchido com `is_open_now: false`. As três mudanças estão marcadas no texto;
> o documento completo do passo é [`operacao-por-filial.md`](operacao-por-filial.md).

---

## 1. O que esta rota resolve, em um parágrafo

O app precisa mostrar as filiais para o cliente escolher uma. Antes desta rota
não havia como montar essa tela: `/restaurants/{slug}/menu` devolve as filiais
sem dizer se estão abertas, e `/restaurants/{slug}/info` responde por uma
filial de cada vez entregando as **faixas de horário cruas** — o app teria que
refazer a conta de intervalo, inclusive a faixa que vira a noite. Agora o
backend responde `is_open_now` como booleano, e, se o cliente já informou onde
mora, responde também distância, taxa e se aquela filial entrega lá.

**O app não calcula horário e não estima taxa.** Os dois números saem daqui
prontos, e quando não dá para calcular vêm nulos em vez de aproximados.

---

## 2. A rota

```
POST /restaurants/{restaurant_slug}/branches/availability
```

**Por que POST e não GET**, já que nada é criado: o endereço do cliente é um
objeto com seis campos, e na querystring ele iria para o log de todo proxy no
caminho — endereço residencial de quem pediu. Além disso a chamada tem custo
(consulta paga de rota, por filial), e não convém convidar cache de CDN a
repeti-la.

**Autenticação: opcional.** Sem token a rota funciona; o token só é necessário
para usar `address_id` (endereço salvo na conta).

**Limite: 20 por minuto, 200 por hora, por IP.**

---

## 3. Requisição

O corpo tem três formas válidas, e nenhuma outra.

### 3.1 Sem endereço — o primeiro carregamento da tela

```json
{}
```

Devolve as filiais com aberta/fechada. Todos os blocos `delivery` vêm `null`.
**Nenhuma chamada paga é feita.**

### 3.2 Com endereço avulso

```json
{
  "address": {
    "street": "Travessa João Felipe",
    "number": "111",
    "neighborhood": "Mousa Brasil",
    "city": "Fortaleza",
    "state": "CE",
    "zipcode": "60000-000",
    "latitude": -3.75,
    "longitude": -38.55
  }
}
```

| Campo | Obrigatório | Observação |
|---|---|---|
| `street` | sim | não pode ser vazio |
| `number` | sim | não pode ser vazio |
| `neighborhood` | sim | não pode ser vazio |
| `city` | não | default `"Fortaleza"` |
| `state` | não | default `"CE"` |
| `zipcode` | não | aceita também a chave `zip_code` |
| `latitude` / `longitude` | não | **mande sempre que tiver** — ver abaixo |

**Mandar `latitude` e `longitude` economiza uma chamada paga** e deixa a
resposta mais rápida. Sem elas o backend precisa geocodificar o texto. Se
mandar uma, mande a outra: só uma das duas é erro **422**.

### 3.3 Com endereço salvo da conta

```json
{ "address_id": "0f3c2b1a-..." }
```

Exige `Authorization: Bearer <token do cliente>`. Sem token: **401**.

### Regras do corpo

- `address_id` e `address` são **excludentes**. Os dois juntos: **422**.
- Nenhum dos dois: válido, é o caso 3.1.
- Campo desconhecido no corpo: **422** (o corpo é `extra="forbid"`).
  Em especial, **`branch_id` não existe aqui** — esta rota devolve todas as
  filiais, por definição.

---

## 4. Resposta

```json
{
  "restaurant_slug": "junior-da-picanha",
  "address_provided": true,
  "default_branch_id": "6a1f...",
  "branches": [
    {
      "id": "6a1f...",
      "name": "Matriz",
      "display_name": null,
      "slug": "matriz",
      "address": {
        "street": "Av. Beira Mar",
        "number": "1200",
        "neighborhood": "Meireles",
        "city": "Fortaleza",
        "state": "CE",
        "zipcode": null,
        "full_address": "Av. Beira Mar, 1200 - Meireles - Fortaleza - CE"
      },
      "phone": null,
      "whatsapp": null,
      "latitude": -3.73,
      "longitude": -38.52,
      "is_main": true,
      "is_open_now": true,
      "closed_reason": null,
      "current_period": { "weekday": 0, "opens_at": "11:00:00", "closes_at": "23:00:00" },
      "delivery": {
        "delivers_to_address": true,
        "reason": null,
        "message": null,
        "distance_km": 4.2,
        "travel_time_min": 18,
        "delivery_fee": 11.3,
        "eta_min": 48,
        "eta_max": 63
      }
    }
  ]
}
```

### 4.1 Raiz

| Campo | Tipo | O que é |
|---|---|---|
| `restaurant_slug` | string | eco do slug pedido |
| `address_provided` | bool | `true` quando o corpo trouxe endereço. Continua `true` mesmo se o endereço não pôde ser localizado — ver §5.3 |
| `default_branch_id` | uuid \| null | a filial que a plataforma usa quando o cliente não escolhe nenhuma. `null` se o restaurante não tem filial ativa |
| `branches` | lista | ordenada: principal primeiro, depois alfabética por nome |

### 4.2 Cada filial

| Campo | Tipo | O que é |
|---|---|---|
| `id` | uuid | **é este o valor que vai em `branch_id`** nas rotas seguintes |
| `name` | string | nome interno da filial |
| `display_name` | string \| null | nome para exibir, quando o lojista configurou um diferente |
| `slug` | string | identificador da filial |
| `address` | objeto | os campos separados **e** `full_address` já montado |
| `phone`, `whatsapp` | string \| null | contato da loja |
| `latitude`, `longitude` | float \| null | para pin no mapa. Nulos quando o lojista não cadastrou |
| `is_main` | bool | filial principal |
| `is_open_now` | bool | **aberta agora**, calculado no servidor, no fuso `America/Fortaleza`. Combina a agenda da semana **e** o "fechar agora" daquela filial |
| `closed_reason` | string \| null | por que não está atendendo. Ver §4.5 |
| `current_period` | objeto \| null | a faixa da AGENDA que está valendo. Ver §4.3 — pode vir preenchido com `is_open_now: false` |
| `delivery` | objeto \| null | ver §4.4. **`null` significa "não perguntei"** |

### 4.3 `current_period`

| Campo | Tipo | O que é |
|---|---|---|
| `weekday` | int | **0 = segunda, 6 = domingo** (padrão Python, não o `getDay()` do JS) |
| `opens_at` | `"HH:MM:SS"` | abertura da faixa vigente |
| `closes_at` | `"HH:MM:SS"` | fechamento |

**`closes_at` menor que `opens_at` é faixa que vira a noite** (18:00–02:00), e
o dado está correto: a faixa pertence ao dia em que **começa**. À 1h da manhã
de terça, quem está aberto é a faixa da segunda — e é por isso que `weekday`
pode não ser o dia de hoje.

Serve para escrever "aberta até 23:00" sem outra chamada. **Não use para
recalcular `is_open_now`** — esse já veio pronto, e refazer a conta no app é
justamente o defeito que esta rota existe para eliminar.

**Desde 18/08/2026 este campo pode vir PREENCHIDO com `is_open_now: false`**, e
isso não é bug: a agenda está em ordem e quem fechou a loja foi o balcão. São
duas coisas — a agenda é cadastro, a pausa é o dia de hoje. `current_period`
fala da agenda; quem decide se a filial atende é `is_open_now`, sempre.

### 4.5 `closed_reason`

| Valor | O que aconteceu | O que o front escreve |
|---|---|---|
| `null` | a filial está atendendo | nada |
| `outside_business_hours` | fora do horário cadastrado | "abre às 18:00", usando `current_period` de `/info` |
| `branch_paused` | o balcão apertou "fechar agora" | "fechada no momento" — **não** prometa horário |

A diferença não é cosmética: `outside_business_hours` passa sozinho quando o
relógio virar, `branch_paused` só passa quando alguém reabrir a loja.

Com as duas coisas valendo ao mesmo tempo (fora do horário **e** pausada), vem
`outside_business_hours` — porque `current_period` já sai nulo nesse caso, e
dizer "pausada" faria a tela afirmar que a agenda está em ordem enquanto o
campo dela vem vazio.

### 4.4 `delivery`

**`delivery: null` quer dizer "não perguntei", não "não entrega".** Enquanto o
cliente não informou o endereço, a tela não deve desabilitar filial nenhuma.

| Campo | Tipo | O que é |
|---|---|---|
| `delivers_to_address` | bool | **é este o campo que habilita ou desabilita a filial na tela** |
| `reason` | string \| null | por que não. Nulo quando entrega. Códigos na §5 |
| `message` | string \| null | texto em português, exibível ao cliente |
| `distance_km` | float \| null | distância **de carro** |
| `travel_time_min` | int \| null | tempo de deslocamento |
| `delivery_fee` | float \| null | taxa em reais, **número** (`11.3`, não `"11.30"`) |
| `eta_min` / `eta_max` | int \| null | preparo + deslocamento, em minutos |

**`distance_km` e `delivery_fee` podem vir preenchidos com
`delivers_to_address: false`.** É o caso de `outside_delivery_area` calculado
por rota: a distância é conhecida e reprovou no raio. Vêm nulos quando nem
chegou a calcular.

---

## 5. Todos os códigos de erro

### 5.1 Da requisição — HTTP

| Código | Quando | O que o front faz |
|---|---|---|
| **200** | sempre que a requisição é válida, mesmo sem nenhuma filial entregando | renderiza a lista |
| **401** | `address_id` sem `Authorization` | manda para o login, ou reenvia com `address` avulso |
| **404** | slug não existe, ou o restaurante está inativo | tela de "restaurante indisponível" |
| **404** | `address_id` não é um endereço daquele cliente | recarrega a lista de endereços da conta |
| **422** | `address_id` e `address` juntos; campo desconhecido; só uma das duas coordenadas; campo obrigatório do endereço vazio | erro de programação do front — não deve chegar ao cliente |
| **400** | endereço sem texto **e** sem coordenadas | peça o endereço de novo |
| **429** | passou de 20/min ou 200/h no mesmo IP | espere e tente de novo; não faça retry em laço |

Todo erro vem no envelope padrão do FastAPI:

```json
{ "detail": "Endereco nao encontrado" }
```

### 5.2 De cada filial — `delivery.reason`

Estes **não são erro HTTP**: a resposta é 200 e a filial aparece na lista, só
que sem entregar. O vocabulário é o mesmo de `POST /delivery/estimate`.

| `reason` | O que aconteceu | O que o front faz |
|---|---|---|
| `null` | entrega normalmente | habilita a filial |
| `outside_delivery_area` | endereço fora do raio daquela filial | mostra "fora da área", sugere retirada |
| `branch_closed` | a filial está fechada agora — pelo horário **ou** pelo "fechar agora" | `is_open_now` já é `false`; `closed_reason` diz qual dos dois |
| `delivery_disabled` | **esta filial** não aceita entrega | esconda a seção de entrega **daquela filial**; as outras da lista podem estar entregando |
| `prep_time_unavailable` | falta tempo de preparo cadastrado no horário atual | trate como indisponível; é configuração faltando no painel |
| `delivery_fee_config_unavailable` | a filial não tem taxa cadastrada e não há contingência | trate como indisponível |
| `route_not_found` | o Google não achou rota até o endereço | peça para revisar o endereço |
| `route_unavailable` | o Google não respondeu | mostre "não foi possível calcular agora" e ofereça tentar de novo |
| `address_not_found` | o endereço informado não pôde ser localizado | peça para revisar o endereço |

### 5.3 Quando o Google cai, a tela continua de pé

`route_unavailable` e `address_not_found` na resolução do endereço são
aplicados **a todas as filiais de uma vez**, e a lista sai mesmo assim, com
`is_open_now` correto — aberta/fechada não depende de Google nenhum.

`address_provided` continua `true` nesse caso: o cliente **informou** o
endereço; o que faltou foi localizá-lo. É como o front distingue "ainda não
perguntei" (`address_provided: false`) de "perguntei e não deu"
(`address_provided: true` com `reason` preenchido em todas as filiais).

---

## 6. O fluxo que o app deve seguir

1. **Abre a tela** → `POST .../branches/availability` com `{}`.
   Mostra as filiais com aberta/fechada. Nada é desabilitado por entrega
   ainda.
2. **Cliente informa o endereço** (ou escolhe um salvo) → mesma rota, agora
   com `address` ou `address_id`. Cada filial ganha taxa, distância e
   veredito.
3. **Cliente escolhe a filial** → guarde o `id` dela.
4. **Cardápio** → `GET /restaurants/{slug}/menu?branch_id=<a filial escolhida>`.
   Desde 20/08/2026 o `branch_id` resolve a resposta INTEIRA: produtos,
   categorias, preços e o bloco `settings`. **Chamar sem o parâmetro depois de
   o cliente ter escolhido mostra o cardápio e a taxa de outra loja.**
5. **Fechar o pedido** → `POST /restaurants/{slug}/delivery/estimate` com o
   `branch_id` escolhido, para obter o `estimate_token`, e depois
   `POST /orders`.

**O passo 5 não paga rota de novo:** o resultado calculado no passo 2 já está
no cache do servidor, e a estimativa da filial escolhida cai nele. Foi de
propósito que esta rota **não** devolve `estimate_token` — emitir um token por
filial gravaria N-1 estimativas que ninguém usaria.

---

## 7. O custo, e por que a tela pode ser reaberta à vontade

Cada filial dentro do raio pode custar uma consulta paga de rota. Três coisas
seguram isso, e vale saber para não lutar contra elas:

1. **Sem endereço, custo zero.** O passo 1 do fluxo nunca chama o Google.
2. **O endereço é geocodificado uma vez por requisição**, não uma vez por
   filial.
3. **Filial provadamente fora do raio não vira consulta.** O servidor mede a
   distância em linha reta primeiro; como ela é sempre menor ou igual à
   distância de carro, quem já estoura o raio em linha reta está fora com
   certeza. Essa filial volta com `outside_delivery_area` e `distance_km:
   null` — **é assim que se reconhece uma filial descartada sem consulta.**

O que o front pode fazer para ajudar: **mandar `latitude`/`longitude` sempre
que tiver** (economiza o geocode) e **não chamar a rota a cada tecla digitada**
no campo de endereço — chame quando o endereço estiver completo.

---

## 8. O que esta rota NÃO faz

- **Não filtra o cardápio por filial** — mas `/menu` filtra, desde
  20/08/2026. Esta rota devolve as filiais; quem devolve o cardápio de uma
  delas é `GET /restaurants/{slug}/menu?branch_id=...`, e lá o parâmetro
  resolve produtos, preços e o bloco `settings`. Ver
  [`cardapio-por-filial.md`](cardapio-por-filial.md).
- **Não diz a que horas a filial fechada abre de novo.** `current_period` é
  nulo com a loja fechada. Para montar "abre às 18:00" use
  `GET /restaurants/{slug}/info?branch_id=...`, que devolve a semana inteira.
- **Não devolve `estimate_token`** — ver §6.
- **Não reserva nada.** Uma filial aberta na resposta pode fechar antes de o
  pedido ser enviado; quem recusa nesse caso é `POST /orders`.
- **Não considera formas de pagamento.** Elas são por filial e saem em
  `GET /restaurants/{slug}/info?branch_id=...`.

---

## 9. Coisas que já quebraram, para não repetir

- **`weekday` 0 é segunda-feira**, não domingo. O `getDay()` do JavaScript usa
  0 = domingo. Converter errado faz a loja aparecer aberta no dia errado, e a
  tela mostra exatamente o que o lojista digitou — ninguém desconfia.
- **Não recalcule `is_open_now` no app.** Já houve duas implementações da
  mesma regra de horário, e quando elas discordam a loja aparece aberta numa
  tela e fechada na outra, sem erro em lugar nenhum.
- **`delivery: null` não é "não entrega".** Desabilitar a filial por causa
  disso esconde, no primeiro carregamento, exatamente as filiais que o cliente
  poderia escolher.
- **A taxa é número, não string.** `11.3` e não `"11.30"`. Formate na
  exibição; não compare como texto.
- **`current_period` preenchido não significa aberta.** Desde 18/08/2026 a loja
  pode estar pausada dentro do horário. Quem responde é `is_open_now`.
- **`delivery_disabled` é da filial.** Esconder a seção de entrega da tela
  inteira ao ver esse código esconde as outras lojas, que podem estar
  entregando normalmente.
