# Identificador de origem e funil do cardápio

> **Estado: implementado.** Revisão `20260822_0031`, de 22/08/2026.

Até esta frente existir, a plataforma só enxergava **pedido**. Poucos pedidos
tinham dois diagnósticos possíveis — ninguém entrou no cardápio, ou entrou e
não comprou — e nenhuma forma de distinguir os dois. São diagnósticos opostos,
com soluções opostas.

---

## 0. A decisão que organiza tudo o resto

São **duas coisas com naturezas diferentes**, e tratá-las como uma só é o erro
que gera todos os outros:

| | O que é | Onde mora | Prazo |
|---|---|---|---|
| **Origem** | atributo da **venda** — "esse pedido veio do QR da mesa" | `orders.source_snapshot` | **para sempre**, como todo snapshot do pedido |
| **Funil** | **telemetria** de quem não comprou | `menu_events` | **90 dias** |

O pedido já congela nome, telefone, endereço, preço e cupom porque a venda
precisa continuar batendo em relatório e em fiscalização. A origem é da mesma
família — o lojista pergunta em março quanto o ímã de geladeira vendeu em
janeiro.

Com a origem vivendo só na tabela de evento, o relatório de origem morreria
junto com o primeiro expurgo.

---

## 1. Os quatro eventos, e por que não um quinto

| # | Evento | Dispara quando |
|---|---|---|
| 1 | `menu_view` | o cardápio da filial carrega — uma vez por sessão |
| 2 | `product_view` | a pessoa abre um produto — uma vez por produto/sessão |
| 3 | `cart_add` | item entra no carrinho |
| 4 | `checkout_start` | a tela de fechamento abre |
| 5 | *(o pedido)* | `orders`, que já existia |

**Cada degrau separa dois diagnósticos**, e é isso — não completude — que
justifica gravar:

- **1→2 baixo:** entrou e não abriu nada. Foto, preço de vitrine, categoria
  errada no topo. Problema de **cardápio**.
- **2→3 baixo:** abriu e não pôs. Preço do item, adicional obrigatório caro.
  Problema de **produto**.
- **3→4 baixo:** montou carrinho e não foi fechar. Frete, pedido mínimo.
  Problema de **regra comercial**.
- **4→5 baixo:** chegou no checkout e desistiu. Forma de pagamento, loja
  fechada, gateway. Problema de **operação**.
- **nenhuma sessão:** ninguém entrou. Problema de **divulgação**.

Tirar um degrau funde dois diagnósticos numa métrica que não diz o que fazer
na segunda-feira.

**O quinto degrau não é um evento**, e não pode ser: ele é o próprio pedido,
contado em `orders`. Gravá-lo como evento criaria uma segunda contagem de
venda, que divergiria da primeira no dia em que o pedido fosse cancelado.

### O que ficou de fora de propósito

| Fora | Por quê |
|---|---|
| scroll, busca, categoria aberta, item removido | nenhum muda uma decisão do lojista |
| tempo na tela, duração de sessão | exigem heartbeat — as N requisições por sessão que a seção 4 evita |
| visitante único, "usuários ativos" | exigem reconhecer a pessoa entre sessões, que é o que a escolha de `sessionStorage` recusa |
| preço, valor de carrinho abandonado | seria número calculado no cliente, e nenhum valor vem do cliente neste projeto |
| funil por produto | o dado fica gravado (`product_view` tem `product_id`); o **relatório** não. É a segunda pergunta |
| agregado diário | ver a nota com prazo na seção 3 |
| Google Analytics, Meta Pixel | mudam a base legal para consentimento (seção 5) |

---

## 2. Onde as duas coisas vivem

### `menu_events`

| Coluna | Por quê |
|---|---|
| `restaurant_id`, `branch_id` | `NOT NULL`. O cardápio é da filial (armadilha 36); evento sem loja não responde nada, e "as duas somadas" é um `GROUP BY` |
| `session_id` | opaco, gerado no cliente. Agrupador de contagem, **nunca chave de acesso** |
| `event_type` | `CHECK` espelhando `MENU_EVENT_TYPES` — armadilha 15, as duas listas mudam juntas |
| `source` | `NOT NULL`, `direct` quando não veio nada |
| `product_id` | só em `product_view` e `cart_add` |
| `occurred_at` | **do servidor** |

**Não tem `created_at` separado.** `occurred_at` já é o instante do servidor;
duas colunas de tempo idênticas seriam ruído em disco na tabela que mais
cresce.

**`occurred_at` é do servidor** porque relógio de celular erra, às vezes por
meses. Um evento datado em 2027 não aparece em erro nenhum: ele some da janela
do relatório e o número fica errado para sempre.

**A FK composta `(restaurant_id, branch_id) → branches(restaurant_id, id)`** é
a mesma amarra que a revisão `20260820_0026` pôs em `categories` e `products`.
É ela que permite à rota **não** conferir a filial com um `SELECT` por
requisição — a regra fica escrita no schema, que é a lição da armadilha 13.

**Dois índices, cada um com dono:**

- btree `(branch_id, occurred_at)` — o relatório;
- **BRIN em `occurred_at`** — o expurgo, que varre por data sem filial e não
  aproveita o índice acima. Tabela append-only ordenada no tempo é o caso de
  livro do BRIN: kilobytes onde um btree teria centenas de megabytes. O tamanho
  importa porque esta tabela escreve muito mais do que lê, e todo índice é
  custo em cada `INSERT` do caminho quente.

### `orders.source_snapshot`

`NOT NULL DEFAULT 'direct'`. `ADD COLUMN` com default constante não reescreve a
tabela no PG 11+, então custa milissegundos mesmo com `orders` grande.

**Entrou sem índice, de propósito.** O `docker-entrypoint.sh` roda
`alembic upgrade head` com a API fora do ar (armadilha 5), e um índice sobre a
maior tabela do banco nesse ponto é a operação parada enquanto ele constrói. Os
relatórios já filtram por restaurante, filial e período — que os índices
existentes cobrem — e a origem entra como predicado adicional sobre um conjunto
já pequeno. Se um dia precisar, nasce à mão com `CREATE INDEX CONCURRENTLY` e um
`alembic stamp` em seguida.

### O que **não** existe: `orders.session_id`

Os quatro degraus se contam por `(filial, dia, origem)`, e o quinto sai de
`orders` pelo mesmo recorte. **Desenhar o funil não exige ligar sessão a
pedido.**

Se exigisse, o rastro de navegação inteiro passaria a estar amarrado a uma
linha com nome, telefone e endereço, e `menu_events` deixaria de ser contagem
anônima para virar histórico comportamental identificado.

**Preço aceito:** não dá para responder "esta pessoa viu seis produtos antes de
comprar".

---

## 3. Volume e retenção

Eventos/dia = **sessões/dia × eventos por sessão**. Com a deduplicação do
cliente (seção 4): 1 evento numa sessão que só olha, 3–6 numa que navega, 8–12
numa que compra.

Na hipótese pessimista de 10% de conversão — a que motiva o projeto, e a cara:

```
piloto, 60 pedidos/dia   →   600 sessões   →  ~2.400 linhas/dia  →  ~875 mil/ano
30 restaurantes iguais   →                    ~72 mil linhas/dia →  ~26 milhões/ano
```

A ~200 B por linha com overhead de tupla e os dois índices: **~5 GB no primeiro
ano** com 30 restaurantes — maior que todo o resto do banco somado.

### 90 dias, e por que desde o dia 1

O argumento não é o disco.

**Primeiro:** este repositório já cometeu este erro. `email_verification_codes`
e `password_reset_codes` guardaram o e-mail de todo mundo que já pediu um
código, para sempre, porque ninguém pensou em prazo no dia 1 (está no docstring
do `cleanup_idempotency_keys.py`). `ai_feedback` foi a mesma coisa um ano
depois. Este seria o mesmo erro com mil vezes o volume.

**Segundo:** ligar agora custou uma função e uma chamada — o container
`limpeza` já existe e já roda em laço de 24h. Ligar depois custaria isso mais
uma conversa sobre o que fazer com o acumulado.

**O teto do relatório (92 dias, `MAX_REPORT_DAYS`) bate com os 90 de propósito.**
Não existe tela capaz de pedir um recorte que o banco já apagou. Mexer num sem
o outro cria exatamente esse buraco.

> **A única pendência com data desta frente.** Se a comparação ano-a-ano vier a
> ser desejada, o **agregado diário** (data, filial, origem, contagem por
> degrau — sem pessoa dentro, e portanto sem prazo) precisa existir **antes do
> primeiro expurgo**, isto é, dentro dos primeiros 90 dias a partir de
> 22/08/2026. Depois disso o histórico não volta.

O expurgo é o único das seis tabelas que apaga **em lotes**, com commit entre um
e outro: um `DELETE` único de centenas de milhares de linhas segura lock e infla
a tabela pela duração inteira da transação. Interrompido no meio (deploy,
container reiniciado), o que já saiu está commitado e a próxima execução
continua de onde parou.

---

## 4. Como o front dispara

**Uma rota em lote:** `POST /menu-events`, com `restaurant_id`, `branch_id`,
`session_id` e `source` no envelope e a lista de eventos dentro.

O front acumula em memória e manda:

- **no primeiro evento** da sessão — para a visita chegar mesmo se a pessoa
  fechar em três segundos;
- depois **a cada ~10 s ou 20 eventos**, o que vier primeiro;
- e em `visibilitychange → hidden` via **`navigator.sendBeacon`** — o único
  jeito de a última leva sobreviver ao fechamento da aba. `fetch` no `unload` é
  descartado pelo navegador, em silêncio.

**Resultado típico: 2 a 4 requisições por sessão**, não N.

**O cliente deduplica:** `product_view` do mesmo produto na mesma sessão vale
uma vez. A pergunta é "quantas pessoas abriram", não "quantos toques" — e essa
regra sozinha corta o volume quase pela metade. O `COUNT(DISTINCT session_id)`
do servidor garante o número mesmo quando o front parar de deduplicar.

### As quatro regras do servidor

1. **Rate limit por IP** (`60/minute;600/hour`). Sem ele, o número que decide
   onde o lojista gasta em divulgação é escrito por qualquer um com `curl`.
2. **Teto de 50 eventos por corpo**, e acima disso o corpo inteiro é recusado —
   nunca truncado. Truncar entrega um funil silenciosamente errado.
3. **`INSERT` em lote, sem `SELECT` nenhum.** As FKs recusam no banco o que não
   existe e o que não combina.
4. **A rota nunca derruba o cardápio.** Falhou o `INSERT`: responde `202` com
   `recorded: 0` e loga. É a única regra desta lista que, esquecida, o cliente
   sente.

O `session_id` é entrada não confiável: 64 caracteres no máximo, e nunca chave
de acesso a coisa nenhuma.

### `202` e não `200`

A página não espera esta resposta — o `sendBeacon` nem a entrega a quem chamou
— e o service responde `recorded=0` em vez de erro quando o `INSERT` falha. Um
`200` prometeria uma gravação que a rota deliberadamente não garante.

---

## 5. LGPD

### O evento é dado pessoal?

**Trate como se fosse, e minimize até a pergunta importar pouco.** É por isso
que a decisão de **não** gravar `session_id` no pedido (seção 2) é a peça
central e não um detalhe de schema. Sem esse vínculo, `menu_events` guarda uma
filial, um instante, um rótulo de campanha e um número aleatório que morre em
90 dias.

Três coisas que **não** entram — e a primeira é a que alguém vai querer
acrescentar "para analisar depois":

1. **IP.** É o que transforma evento em rastro de pessoa mesmo sem cookie, e
   não responde a nenhum dos cinco degraus.
2. **User-Agent.** Idem.
3. **`customer_id`.** Nem quando a pessoa está logada.

O `session_id` fica em **`sessionStorage`, não `localStorage`** — morre com a
aba. É escolha de privacidade, não detalhe de front.

### Base legal: legítimo interesse, não consentimento

Art. 7º, IX. A finalidade é o lojista medir o próprio funil de venda, não
perfilar pessoa nem alimentar terceiro. A base tem **três condições, e as três
são exigência**:

1. estar escrito na política de privacidade (texto abaixo);
2. não ter IP nem UA;
3. ter prazo.

**A fronteira é nítida:** no dia em que alguém quiser mandar esses eventos para
Meta Pixel ou Google Ads, a base vira **consentimento** e entra banner de cookie
numa tela cujo objetivo é a pessoa pedir comida em três toques.

### A retenção é o mecanismo de exclusão

`menu_events` não tem `customer_id`, então `customer_anonymization_service`
**não alcança** linha nenhuma dela, hoje ou nunca. É exatamente a situação de
`ai_feedback` e do comentário de `order_reviews` — e nas três a resposta
escolhida foi a mesma: **prazo curto é a defesa**.

Está escrito com todas as letras em `menu_event_retention_cutoff`, para o dia em
que alguém quiser esticar o número "porque disco é barato".

### O rótulo de origem, que fica para sempre

`qr-mesa-04` é atributo da venda, não da pessoa. **Isso proíbe uma coisa, e ela
precisa estar dita:** nada de QR por cliente, nada de `source=whatsapp-do-joao`.
No dia em que o rótulo identificar alguém, ele deixa de ser dado de venda e vira
dado pessoal permanente dentro de uma coluna que não tem prazo nenhum.

### O parágrafo para a política de privacidade

> **Medição de uso do cardápio.** Quando você abre o cardápio digital de um
> restaurante na PedeAqui, registramos de forma agregada quais etapas foram
> alcançadas — cardápio aberto, produto visualizado, item adicionado ao
> carrinho e início do checkout —, junto do restaurante, da loja e do
> identificador de origem que consta no link ou no QR Code pelo qual você
> chegou. Esse registro usa um identificador aleatório de sessão, criado no seu
> navegador e apagado quando você fecha a aba, e **não** inclui seu nome,
> e-mail, telefone, endereço IP ou dados do seu aparelho, nem é vinculado à sua
> conta ou aos seus pedidos. A finalidade é permitir que o restaurante entenda
> em que ponto as pessoas desistem de pedir e onde vale divulgar, com base no
> legítimo interesse (art. 7º, IX, da LGPD). Esses registros são **apagados
> automaticamente após 90 dias** e não são compartilhados com terceiros nem
> usados para publicidade direcionada.
>
> O identificador de origem do link ou QR Code pelo qual você chegou também
> fica registrado no pedido que você fizer, como parte do histórico daquela
> venda, e nesse caso segue o prazo de guarda dos pedidos.

---

## 6. A origem sobrevive à navegação?

**Em parte, e o "em parte" é uma escolha.** O caminho:

1. a pessoa abre `?src=qr-mesa-04`;
2. o front grava em `sessionStorage` na primeira carga **e limpa o parâmetro da
   URL**;
3. todo lote de evento carrega essa origem;
4. `CreateOrderRequest` leva `source`, e o `OrderService` congela em
   `orders.source_snapshot`.

**O passo 2 não é cosmético.** Sem limpar a URL, a pessoa compartilha o link no
WhatsApp e o `src` do QR da mesa 4 vira a origem de todo mundo que clicar. Não
dá erro nenhum, e só se descobre quando a mesa 4 aparece com 300 pedidos.

| Situação | O que acontece |
|---|---|
| mesma aba, mesmo parada meia hora | `sessionStorage` vive. **Funciona** |
| aba fechada e reaberta pelo histórico | a origem se perde; o pedido cai em `direct` |
| entrou de novo por outro canal | **primeira origem vence** (first-touch) |

A segunda linha é perda aceita. A alternativa é `localStorage`, que é
rastreamento persistente entre visitas e muda a conversa da seção 5 inteira, por
alguns pontos percentuais de atribuição. **Decidido medir primeiro**: se a perda
for grande, o meio-termo é `localStorage` com 24 h guardando só o rótulo.

**First-touch está escrito** porque senão cada relatório responde uma coisa. O
motivo: a pergunta é "o que trouxe essa pessoa"; last-touch premia o canal que
ela já ia usar de qualquer jeito.

### O rótulo

- minúsculo, `[a-z0-9-]`, máximo 40, normalizado por `normalize_traffic_source`,
  que reusa o `slugify` e por isso é imune a NFC/NFD de graça (armadilha 31);
- **rótulo desconhecido não é recusado**, vira uma linha a mais no relatório.
  Recusar transformaria um QR impresso com typo em **pedido perdido**;
  quinhentos ímãs errados se consertam no relatório, um pedido recusado não;
- ausente = `direct` **gravado**, não `NULL`. O nulo dividiria o relatório entre
  "veio direto" e "não sabemos".

### `source` fica fora da impressão digital da idempotência

`OrderService._idempotency_fingerprint` exclui o campo. Duas razões, e as duas
valem sozinhas:

- **no mérito:** o fingerprint separa retry de conflito. Origem não muda item,
  preço, endereço nem forma de pagamento — mesma chave com outra origem deve
  devolver o pedido original, não um 409;
- **no deploy:** o fingerprint sai de `model_dump()`, então **qualquer** campo
  novo muda o hash de todos os corpos. Uma chave reservada antes do deploy e
  retentada depois receberia 409 por um pedido idêntico, pelas 24 h de vida da
  chave. É a armadilha 7 pelo outro lado.

**A regra que fica:** campo novo em `CreateOrderRequest` que não mude *o que foi
pedido* entra na lista de exclusão. Campo que mude o pedido fica de fora dela,
porque aí o conflito é a resposta certa.

---

## 7. O que o painel ganhou

**`GET /admin/reports/funnel`** — os cinco degraus, a conversão de um para o
outro e a divisão por origem. `GERENCIA`, sem `ensure_pode_ler_dinheiro`: não há
um número de dinheiro na resposta, e quem toca o balcão é quem consegue agir
sobre "o carrinho enche e o checkout esvazia".

**`?source=` nos seis relatórios que já existiam.** Entra pelos mesmos
`billable_order_conditions` / `excluded_order_conditions` do recorte de filial —
nenhum relatório filtra por conta própria. Nulo é "todas as origens", nunca "as
sem origem".

### A ressalva que viaja na resposta

**O `orders_count` do funil não fecha com o de `/reports/summary`.** O funil
conta *todo* pedido feito, cancelado e recusado inclusive, porque ele mede se a
**pessoa** terminou de pedir — a loja recusar meia hora depois é outro problema,
com outra solução. Contá-lo como não-conversão faria o funil dizer "o checkout
está perdendo gente" quando o que houve foi a loja recusando pedido: exatamente
o colapso de diagnósticos que o funil existe para desfazer.

O predicado mora em `funnel_order_conditions`, ao lado dos outros dois, e a
resposta carrega `orders_note` — mesma forma do `revenue_note` de
`/reports/products`.

**`sources` lista todas as origens do período mesmo com `?source=` preenchido:**
filtrada, teria uma linha só e não responderia nada. Uma origem entra na lista
se trouxe sessão **ou** se trouxe pedido — origem com sessão e zero pedido é o
canal que traz gente que não compra, a linha mais importante da tela.

---

## 8. O que fica pendente

1. **O agregado diário, se a comparação ano-a-ano for desejada** — e ele tem
   data (seção 3).
2. **`localStorage` de 24 h para a origem**, se a medição mostrar perda grande
   de atribuição (seção 6).
3. **Resolver a origem no servidor** pela primeira visita da sessão, quando o
   pedido chega sem `source`. Custa um `SELECT` no caminho mais quente do
   sistema e reintroduz o vínculo pedido↔sessão que a seção 2 recusou. Os 90
   dias de evento vão medir quanto se perde; decidir com o número.
4. **O front.** Nada desta frente mede coisa alguma enquanto o cardápio não
   disparar os lotes e não devolver `source` no pedido.
