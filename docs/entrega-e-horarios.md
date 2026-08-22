# Entrega, taxa e horário de funcionamento

Cobre `services/delivery_estimate_service.py`,
`services/branch_hours_service.py`,
`services/branch_availability_service.py` e
`integrations/google_maps_routes_client.py`.

O contrato da rota de escolha de filial, escrito para quem implementa o front,
está em [`contrato-filiais-frontend.md`](contrato-filiais-frontend.md).

---

## 0. A filial padrão, quando o cliente não escolheu

`BranchRepository.get_default_branch` é a **única** definição, e ela é:
principal se houver, senão a primeira ativa em ordem alfabética.

Antes existiam duas. `GET /restaurants/{slug}/info` pegava o primeiro item de
`list_active_by_restaurant` e a estimativa de entrega exigia `is_main`,
recusando com **400** o restaurante que não tivesse a flag marcada. Como
aquela listagem já ordena por `is_main DESC NULLS LAST, name ASC`, as duas
concordavam quando havia filial principal — e discordavam exatamente quando
ela faltava, que é o caso em que uma rota respondia e a outra falhava, para o
mesmo restaurante, no mesmo minuto.

**O que mudou na prática:** a estimativa de entrega deixou de responder 400
para restaurante que tem filial ativa mas nenhuma marcada como principal. O
400 continua existindo para o caso que ele sempre quis descrever — restaurante
sem filial ativa nenhuma.

---

## 1. O fluxo da estimativa

`POST /restaurants/{slug}/delivery/estimate` →
`DeliveryEstimateService.estimate_and_store`, que chama `estimate` e guarda o
resultado.

```
restaurante ativo
filial (a informada, ou a padrão — BranchRepository.get_default_branch)
   └─ sem NENHUMA filial ativa → 400
filial.accepts_delivery == false                   → não atende ("delivery_disabled")
filial pausada (delivery_paused_until > agora)     → não atende ("delivery_paused")
filial.is_open == false (o "fechar agora")         → não atende ("branch_closed")
endereço (address_id do cliente, ou inline)
   └─ address_id sem login → 401;  address_id de outro cliente → 404
faixa de funcionamento que contém o agora          → BranchHoursService
   └─ nenhuma faixa contém o agora  → NÃO ATENDE ("branch_closed")
   └─ faixa sem tempo de preparo    → NÃO ATENDE ("branch_business_hours_missing")
coordenadas: usa lat/lng se vierem, senão GEOCODIFICA (chamada PAGA)
cache (Redis, ou memória)
   └─ hit → devolve sem chamar o Google
Google Routes: compute_route (chamada PAGA)
taxa de entrega = base + km × valor/km, com piso e teto
   └─ filial sem base/por-km configurados → cai para o fallback (ver §3)
distância > delivery_max_distance_km → fora da área
eta_min = prep_time_min + deslocamento   ← faixa de distância, se houver
eta_max = prep_time_max + deslocamento     (senão, o tempo do Google)
grava no cache
────────────────────────────────────────────────────────
GRAVA em delivery_estimates e devolve `estimate_token`
   └─ só quando serviceable=true
```

Dentro do fluxo de pedido, qualquer `serviceable=false` vira **400** com a
mensagem do motivo.

Latitude e longitude têm que vir **juntas** — uma sem a outra é 422.

---

## 1.1 Pausar a entrega sem apagar configuração

`PATCH /admin/branches/{branch_id}/delivery-pause` →
`{"minutes": 40, "reason": "chuva forte"}`. `{"minutes": 0}` retoma na hora.
Teto de **24 horas**.

**Por que não bastava `accepts_delivery`.** Aquele já resolve "desligar sem
apagar configuração" — o que ele não resolve é o *temporariamente*. Uma chave
manual precisa de alguém que lembre de desligá-la, e o dia em que ela é usada
(chuva às 19h, entregador que sumiu, avenida em obra) é exatamente o dia em
que ninguém lembra. A loja amanhece aberta sem aceitar entrega, e o único
sintoma é a ausência de pedido — que não acende alarme nenhum.

Por isso a pausa é um **prazo**, e não uma chave: ela se desfaz sozinha
(`_delivery_esta_pausada` compara com o relógio, não lê um booleano).

| | `accepts_delivery` | `delivery_paused_until` |
|---|---|---|
| Natureza | estrutural: "este quiosque não entrega" | o dia: chuva, sem motoboy |
| Volta | quando alguém religa | sozinha, no prazo |
| Rota | `PATCH .../order-types` (GERÊNCIA) | `PATCH .../delivery-pause` (PESSOAS) |
| Teto | — | 24h |

As duas se somam em **`accepts_delivery_now`**, e é ele que decide pedido.
Ler `accepts_delivery` sozinho aceita pedido de uma filial pausada.

**A recusa acontece em DOIS lugares, e os dois são necessários.** A estimativa
recusa com `reason = "delivery_paused"`; `OrderService._validate_branch_accepts_order`
recusa de novo. O segundo não é redundante: pedido que chega com
`delivery_estimate_token` válido reaproveita a estimativa guardada e **não
passa** por `DeliveryEstimateService.estimate()`. Sem ele, quem estimou às
18h55 fecharia o pedido às 19h05 com a entrega pausada desde as 19h.

O `reason` é próprio, e não o `delivery_disabled` de quem não entrega: "esta
loja não entrega" manda o cliente escolher outra filial, "voltamos às 20h30" é
um convite a esperar — e o app só consegue mostrar telas diferentes se souber
distinguir. (É o contrário da decisão de `branch_closed`, onde juntar os dois
casos foi o certo: lá o cliente faz a mesma coisa nos dois.)

A mensagem sai pronta com prazo e motivo: *"A entrega está pausada no
momento. Motivo: chuva forte. Voltamos a entregar às 20:30."* — pausa sem
prazo faz o cliente fechar o app; com prazo, ele volta. O horário sai no fuso
da operação.

---

## 2. A taxa

```
raw_fee = delivery_base_fee + (distance_km × delivery_fee_per_km)
taxa    = min( max(raw_fee, delivery_min_fee), delivery_max_fee )
```

Os cinco campos são da **filial** (`branches`), não do restaurante:
`delivery_base_fee`, `delivery_fee_per_km`, `delivery_min_fee`,
`delivery_max_fee`, `delivery_max_distance_km`. Piso e teto são opcionais; sem
eles a taxa é a bruta.

Para depurar taxa errada, o grep é `event=delivery_fee_algorithm` — ele imprime
distância, base, valor por km, piso, teto, raio máximo, taxa bruta e taxa final
numa linha só.

### Frete grátis acima de X

Dois campos, os dois no regime de **termo comercial** da revisão
`20260818_0025` (nulo na filial = herda do restaurante):
`free_delivery_enabled` e `free_delivery_min_order_value`.

**Por que são dois campos e não só o valor.** Sem o booleano, a filial não
teria como recusar a campanha da marca: `NULL` significa "herda" e não existe
número que signifique "desligado" — `0` seria "grátis sempre", o oposto. É a
mesma forma do par `service_fee_enabled` + `service_fee_amount`, que existe
por essa razão. A loja de Messejana, a 12 km de tudo, manda
`free_delivery_enabled: false` e continua herdando o resto.

**O ligado resolvido é `false` por omissão**, ao contrário da taxa de serviço
(que `resolve_branch_operation` resolve como ligada quando os dois lados são
nulos). A assimetria é a armadilha 11 de novo: taxa de serviço ligada sem
valor cobra zero e não machuca ninguém; frete grátis ligado por omissão **dá a
entrega de graça em nome de um lojista que não pediu**.

**A regra é aplicada no PEDIDO, nunca na estimativa** — e isso não é detalhe
de implementação:

- a estimativa acontece **antes de existir carrinho fechado**; ela não sabe o
  subtotal;
- uma rota que recebesse o subtotal do cliente para responder "grátis ou não"
  seria **preço vindo do cliente** por outra porta.

O que a estimativa e o `/menu` publicam é o **teto**, e o app calcula o
"faltam R$ 12" com o próprio subtotal. Quem decide o valor cobrado é
`OrderService._apply_free_delivery`, com o subtotal que o servidor calculou.

Três detalhes dessa função que já têm teste:

- **compara com o SUBTOTAL, não com o total.** Incluir a própria taxa faria o
  frete grátis se autoconceder: um pedido de R$ 55 com taxa de R$ 7 alcançaria
  o teto de R$ 60 por causa da taxa que ele está tentando não pagar;
- **`>=`, não `>`**: "acima de R$ 60" na tela do lojista é lido por todo
  cliente como "60 fecha o pedido com entrega grátis";
- **roda ANTES do cupom.** Um cupom `free_delivery` desconta o valor da taxa;
  com a ordem invertida, o cliente ganharia o desconto sobre uma taxa que não
  ia pagar — dinheiro de volta do nada.

`orders.delivery_fee_waived` grava quanto foi perdoado. Nenhum relatório lê
essa coluna hoje, e ela existe assim mesmo: **dado não capturado na escrita
não se recupera depois.** Com só `delivery_fee = 0` gravado, "quanto essa
campanha me custou em agosto" não tem resposta — não dá para saber quanto a
rota teria cobrado num pedido que já passou.

---

## 3. Quando o Google cai

Existe **sim** uma taxa de contingência, e ela tem uma regra que confunde.

`GoogleMapsUnavailableError` (Google fora do ar, chave errada, timeout) faz o
serviço cair para o `default_delivery_fee` **resolvido** — o da filial quando
ela sobrescreveu, o do restaurante quando não (`operacao-por-filial.md`) —
gravando `provider="configured_fallback"` no pedido. É por esse valor que se
separa depois o pedido precificado por rota do precificado em contingência.

**Mas `default_delivery_fee` só vale como fallback se for MAIOR QUE ZERO.** A
coluna é nullable e tem default 0, e a maioria das linhas em produção está em 0
sem ninguém ter escolhido isso. Tratar esse 0 como "entrega grátis na
contingência" faria uma queda do Google virar frete grátis para a plataforma
inteira sem nenhum lojista ter pedido.

Consequência prática: **filial sem `default_delivery_fee` configurado — nem
nela, nem no padrão do restaurante — continua recusando todo pedido de entrega
quando o Google cai**
(`route_unavailable`). A saída operacional é a retirada (`pickup`), que não passa
pela estimativa.

O preço desse desenho: não dá para configurar "entrega grátis quando a rota
falha". Quem quer entrega grátis configura `delivery_base_fee = 0` na filial, que
é o caminho normal.

Por isso a chave do Maps é validada no boot e **derruba a API** se faltar: antes
disso, a API subia e recusava todo pedido de entrega em silêncio.

Endereço não localizado (`ZERO_RESULTS`) é outro caso: vira `route_not_found`,
não atende, e é cacheado como negativo por `DELIVERY_ESTIMATE_NEGATIVE_CACHE_TTL_SECONDS`
(120s por padrão).

---

## 3.1 Prazo por faixa de distância

`branch_delivery_time_bands`, por filial, **sem herança nenhuma**: a faixa
mede o tempo entre a porta *daquela* loja e a porta do cliente, e duas lojas
da mesma marca em pontas opostas da cidade não têm faixa em comum.

`GET`/`PUT /admin/branches/{branch_id}/delivery-time-bands` (leitura PESSOAS,
escrita GERÊNCIA). PUT substitui todas — a tela é uma tabelinha salva junta,
como o horário da semana. **`{"bands": []}` apaga tudo**, e o resultado não é
"sem entrega": é o prazo voltando a sair do tempo do Google.

### Onde a faixa entra na conta

```
                    ANTES              DEPOIS (com faixa que alcance)
eta_min   prep_min + travel(Google)    prep_min + faixa.delivery_time_min
eta_max   prep_max + travel(Google)    prep_max + faixa.delivery_time_max
```

**A faixa substitui o tempo de DESLOCAMENTO, não o ETA inteiro**, e as duas
metades dessa frase têm motivo:

- *substitui o deslocamento* porque o tempo do Google é tempo de **dirigir**.
  Ele não inclui ensacar o pedido, a segunda entrega da mesma corrida,
  estacionar e subir escada — e é por isso que o prazo saía curto justamente
  no bairro longe, que é onde o cliente já desconfia. O lojista sabe esse
  número; a API não;
- *o preparo continua somando* porque é ele que o botão de **"+10 minutos"** do
  almoço (`PATCH /admin/branches/{id}/prep-time`) ajusta. Uma faixa que
  respondesse pelo prazo todo desligaria aquele botão para entrega e o
  deixaria valendo só para retirada — bem no horário em que ele existe para
  ser usado.

O `travel_time_min` do Google **continua na resposta**, como informação: é o
que permite comparar o prazo prometido com o que a rota diz.

### As faixas são TETOS, não intervalos

Só `max_distance_km` é gravado. Vale a **primeira faixa, em ordem crescente,
cujo teto alcança a distância** (`<=`, então a distância exata do teto cai na
própria faixa).

Guardar também o piso permitiria cadastrar `0–5` e `6–10` e deixar o endereço
de 5,4 km **sem faixa nenhuma** — um buraco que aparece no endereço de um
cliente específico e some quando alguém vai conferir. Com teto, a cobertura
sai de graça, e o UNIQUE `(branch_id, max_distance_km)` fecha o resto: dois
tetos iguais não são ambiguidade de apresentação, são duas respostas para a
mesma distância, e a que vale mudaria entre duas consultas idênticas.

Endereço **além do último teto** não cai em faixa nenhuma, e isso é um estado
válido: vale o tempo do Google, como antes desta tabela existir. Não confundir
com `delivery_max_distance_km`, que é até onde a filial **atende** — são
perguntas diferentes e não se misturam.

### O que NÃO mudou

`estimated_delivery_time_min/max` (do restaurante/filial, herdado) continua
sendo o **rótulo de vitrine** publicado em `/menu`, digitado pelo lojista, e
**não é recalculado a partir das faixas**: o campo mudaria de significado sem
mudar de nome, e o lojista que digitou "30 a 60" veria outro número sem ter
editado nada. Ele nunca entrou no ETA real — quem sempre respondeu por isso é
`prep_time` (da faixa de horário) + deslocamento.

O app recebe as faixas em `/menu` (`settings.delivery_time_bands`) e decide o
que mostrar antes de existir endereço.

### As faixas entram na chave do cache

`_bands_fingerprint` entra em `_cache_key`. Sem isso, editar uma faixa no
painel continuaria servindo o prazo antigo por até
`DELIVERY_ESTIMATE_CACHE_TTL_SECONDS` — e o lojista que acabou de corrigir um
prazo desonesto veria a correção não surtir efeito, sem nada no log. O cache
existe para poupar chamada paga ao Google, não para congelar configuração.

---

## 4. Horário de funcionamento

`services/branch_hours_service.py`. A regra é a óbvia: **ou existe uma faixa que
contém o agora, ou a filial está fechada.** Não existe faixa "mais parecida".

Isso não é detalhe de estilo — foi um bug: quando a hora atual não caía em
nenhuma faixa, o código pegava a **primeira faixa cadastrada**, e às 3h da manhã
um pedido passava com o tempo de preparo do almoço, com a loja fechada.

### `weekday` 0 = segunda

É o `datetime.weekday()` do Python. **No JavaScript, `getDay()` devolve 0 =
domingo.** Os dois lados não conversam sozinhos — ver a armadilha em
`.claude/skills/rapidex-backend/SKILL.md`.

### A faixa que vira a noite

Uma faixa 18:00–02:00 pertence ao weekday em que **ela começa**. À 1h da manhã de
terça, quem está aberto é a faixa da segunda. Por isso `find_current_period`
consulta **hoje e ontem**:

- `_period_covers_same_day` — faixa normal (`opens_at <= closes_at`) cobre o
  intervalo; faixa que vira cobre de `opens_at` até a meia-noite.
- `_period_covers_after_midnight` — só faixas que viram, e só até `closes_at`.

### `is_closed` é lido

Era ignorado, e uma linha marcada como fechada mas com horários preenchidos (o
jeito comum de registrar "domingo fechado") abria a filial. Hoje `_open_periods`
descarta linha com `is_closed=true` ou sem `opens_at`/`closes_at`.

### A pausa manual e a agenda são checagens diferentes

`is_open` da filial é o "fechar agora" — a pausa do dia. A agenda da semana é
`branch_business_hours`. **`OrderService` checa as duas**, nessa ordem, e nenhuma
substitui a outra: estar dentro do horário não significa que o balcão não
apertou "fechar agora" às 21h.

Na tela de escolha de filial as duas se combinam em `is_open_now`, e
`closed_reason` diz qual delas fechou. Ver `operacao-por-filial.md` §3.

### `ensure_branch_is_open` vale para os dois tipos de pedido

Inclusive retirada. Metade do motivo da função existir: pedido de retirada não
passa pela estimativa de entrega, então antes disso nunca chegava perto de uma
checagem de horário.

**Sintoma que continua valendo:** sem faixa cadastrada para o weekday de hoje, a
filial está fechada e nenhum pedido entra. Confira `branch_business_hours` para o
`weekday` atual (0 = segunda), com `prep_time_min` e `prep_time_max` preenchidos.

---

## 5. Editar horários pelo painel

`PUT /admin/branches/{branch_id}/business-hours` **substitui a semana inteira**:
apaga todas as faixas da filial e grava as do corpo. Dia ausente do corpo = dia
fechado.

Validações antes de gravar (`AdminSettingsService._validate_weekly_periods`):

- no máximo `MAX_PERIODS_PER_WEEKDAY` faixas por dia;
- não misturar faixa fechada com faixa aberta no mesmo dia — seria contraditório,
  e o `BranchHoursService` resolveria a favor da aberta, abrindo a loja no dia que
  o lojista marcou como fechado;
- faixas do mesmo dia não podem se sobrepor. Duas faixas sobrepostas não "abrem
  mais": tornam o tempo de preparo imprevisível, porque `find_current_period`
  devolve a **primeira** que contém o agora.

Faixa que vira a noite é esticada para depois das 24h (18:00–02:00 vira 1080–1560)
para caber na mesma comparação. A sobreposição entre a virada de um dia e a
madrugada do dia **seguinte** não é conferida — nesse caso a loja está aberta
pelos dois lados, que é o que o lojista quis dizer de qualquer jeito.

### Ajustar o tempo de preparo no aperto

`PATCH /admin/branches/{branch_id}/prep-time` muda o `prep_time` **da faixa que
está valendo agora**, e só dela.

Por quê: `prep_time` mora em `branch_business_hours`, uma linha por faixa, e o
pedido lê exatamente a faixa que contém o agora. Escrever em qualquer outra
mudaria um número que o próximo pedido não consulta; escrever em todas faria o
aperto do almoço de hoje virar o prazo padrão do jantar de quinta.

Fora do horário de funcionamento não há faixa vigente, e a resposta é **409** em
vez de escolher uma por conta própria.

---

## 6. Reaproveitar a estimativa

Sem isso, criar o pedido refazia geocode + rota no Google poucos minutos depois de
`/delivery/estimate` ter feito exatamente as mesmas duas chamadas: custo dobrado
por pedido, e a conexão de banco presa durante um I/O externo de segundos.

`OrderService._reuse_stored_estimate` aceita o `delivery_estimate_token` e lê a
linha de `delivery_estimates`. **Nada de valor vem do cliente** — ele devolve só o
token; taxa, distância e prazo saem do banco.

A estimativa é **descartada** (e o Google chamado de novo) quando:

| Motivo logado | Quando |
|---|---|
| `token_invalido_ou_vencido` | passou de `DELIVERY_ESTIMATE_REUSE_TTL_SECONDS` (900s = 15 min) |
| `restaurante_ou_filial_diferente` | o token é de outra loja |
| `cliente_diferente` | estimativa de um cliente logado não vale para outro nem para visitante — ela pode ter usado endereço salvo da conta |
| `endereco_diferente` | **a defesa que importa**: estimar o endereço perto e fechar o pedido para o distante pagando a taxa do primeiro |

O grep para quando a conta do Maps subir:
`[Delivery estimate] estimativa nao reaproveitada motivo=`.

**O que o token NÃO carrega, e por que isso importa desde a revisão 0030.** A
linha guardada tem taxa, distância e prazo — e mais nada. Frete grátis e a
pausa da entrega são decididos no PEDIDO, com a configuração viva da filial:

- o token de uma estimativa feita antes de a campanha ser ligada **ganha** o
  frete grátis, porque a regra roda depois com o subtotal real;
- o token de uma estimativa feita antes da pausa **é recusado**, porque
  `_validate_branch_accepts_order` roda antes de o token ser lido.

Congelar qualquer uma das duas no token seria o mesmo defeito da armadilha 12
com outro nome: valor de estimativa velha decidindo o pedido de agora.

O motivo legítimo e frequente é o cliente demorar mais de 15 minutos para fechar
o pedido. Aumentar esse TTL reduz custo e aumenta a chance de cobrar uma taxa
calculada com o trânsito de meia hora atrás — e o TTL de 15 min também foi
escolhido para não atravessar para a próxima faixa de horário da filial, cujo
tempo de preparo é outro.

### O fingerprint do endereço

`build_address_fingerprint` identifica o endereço que a estimativa mediu:

- endereço de conta → `address_id:{uuid}`. O conteúdo pode ser editado depois, e
  aí a estimativa antiga deixa de valer — o id continua o mesmo, mas a expiração
  de 15 minutos limita a janela.
- endereço avulso → sha256 de rua, número, bairro, cidade, estado, CEP e
  coordenadas, normalizados. **Complemento e ponto de referência ficam de fora**
  porque não mudam distância nenhuma.

---

## 7. O cupom é validado depois da chamada paga ao Maps

Ordem em `create_order`: estimativa primeiro, cupom depois. Um cliente com cupom
inválido tentando várias vezes queima chamadas do Google a cada tentativa.

**Não é bug.** A taxa de entrega é *insumo* do desconto: `free_delivery` desconta
exatamente o valor da entrega, então o cupom não pode ser avaliado antes de a
taxa existir.

O que reduz o desperdício é o cache de estimativa e o front chamar
`POST /restaurants/{slug}/coupons/preview` antes de fechar o pedido.
