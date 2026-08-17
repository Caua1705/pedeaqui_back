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
restaurant_settings.accepts_delivery == false      → não atende
filial (a informada, ou a padrão — BranchRepository.get_default_branch)
   └─ sem NENHUMA filial ativa → 400
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
eta_min = prep_time_min + tempo de viagem
eta_max = prep_time_max + tempo de viagem
grava no cache
────────────────────────────────────────────────────────
GRAVA em delivery_estimates e devolve `estimate_token`
   └─ só quando serviceable=true
```

Dentro do fluxo de pedido, qualquer `serviceable=false` vira **400** com a
mensagem do motivo.

Latitude e longitude têm que vir **juntas** — uma sem a outra é 422.

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

---

## 3. Quando o Google cai

Existe **sim** uma taxa de contingência, e ela tem uma regra que confunde.

`GoogleMapsUnavailableError` (Google fora do ar, chave errada, timeout) faz o
serviço cair para `restaurant_settings.default_delivery_fee`, gravando
`provider="configured_fallback"` no pedido. É por esse valor que se separa depois
o pedido precificado por rota do precificado em contingência.

**Mas `default_delivery_fee` só vale como fallback se for MAIOR QUE ZERO.** A
coluna é nullable e tem default 0, e a maioria das linhas em produção está em 0
sem ninguém ter escolhido isso. Tratar esse 0 como "entrega grátis na
contingência" faria uma queda do Google virar frete grátis para a plataforma
inteira sem nenhum lojista ter pedido.

Consequência prática: **restaurante sem `default_delivery_fee` configurado
continua recusando todo pedido de entrega quando o Google cai**
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
