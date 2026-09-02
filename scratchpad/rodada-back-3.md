# Rodada 3 do backend — fonte da verdade

Branch: `rodada/backend-3`, saindo de `rodada/backend-2`. Nunca commitar na
`main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Rodadas anteriores: `scratchpad/rodada-back.md`, `scratchpad/rodada-back-2.md`.

---

## Regras desta rodada (do pedido)

- **Nada de produção.** O comando das filiais **não** roda — quem roda é o
  dono. O commit `6fcaccc` continua esperando. Nenhuma migração aplicada,
  nenhum merge na `main`.
- **FERRAMENTA ANTES DO CONSERTO.** Antes de consertar qualquer classe de
  defeito, escrever a varredura que a encontra em todo lugar. As varreduras
  entram no portão **como aviso**, para o número não crescer calado.

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 4 | A varredura das leituras de coluna nulável (ferramenta) | **feito** |
| 1 | `valid_until` — conserto no código, não no schema | **feito** |
| 2 | As outras 7 das 16, na ordem de gravidade | pendente |
| 3 | Redis e LGPD — o comando, e se a exclusão alcança o Redis | **feito** — resposta: NÃO alcança, e há coordenada na chave |
| 4b | As varreduras no portão como aviso | pendente |

---

## 4. A ferramenta, antes de tudo

`scripts/leituras_de_coluna_nulavel.py` (novo). Para cada coluna que o banco
deixa nula e o ORM declara `NOT NULL` — a lista sai de
`divergencias_orm_schema.comparar()`, não de uma segunda cópia —, acha **toda
leitura em forma que quebra com `None`**:

| Forma | Por que quebra |
|---|---|
| `.metodo()` / `.atributo` em cima | `AttributeError` |
| comparação de ordem (`< <= > >=`) | `TypeError` entre `None` e datetime |
| aritmética | idem |
| argumento nomeado ou posicional | é assim que o `None` entra num schema de resposta e vira 500 |

E considera **protegido** o acesso guardado por `if x.col`, `x.col is None`,
`x.col or padrão`, `not x.col`, ou `getattr(..., None)`.

**O que ele NÃO faz, e está escrito no módulo:** casa por **nome** de atributo,
não por tipo. `created_at` e `email` são colunas de meia dúzia de tabelas, e o
script não sabe de qual o objeto veio. Por isso a saída é **por coluna**, o
`--coluna` filtra, e o número que o portão vigia é **linha de base** — ele
responde "entrou leitura nova?", não "há N bugs".

**Linha de base de hoje: 81 leituras**, em 15 colunas. `created_at` sozinho
responde por 22, e a maioria é de outras tabelas.

`Modelo.coluna` (maiúscula) é ignorado de propósito: em expressão de SQL isso
não lê valor nenhum — o `None` vira `IS NULL` do lado do banco.

---

## 1. `valid_until` — o conserto foi no código

**A coluna continua `NULL`, por decisão.** Cupom sem data de fim é campanha
plausível, e o precedente está no mesmo módulo: a revisão `20260828_0043`
tornou `code` nulo **com significado**. Nas outras 15 do levantamento o banco
estava frouxo e o model certo; nesta era o contrário.

### O que a varredura achou antes do conserto

Três leituras que quebram, e **duas cópias em SQL** que o `--coluna` não pega
(são `Modelo.coluna`, ignorado de propósito) mas que o grep completo mostrou:

```
src/schemas/coupon_schema.py:177   [comparacao de ordem]  if self.valid_until <= self.valid_from
src/services/coupon_service.py:174 [argumento posicional] valid_until = self._aware(coupon.valid_until)
src/services/coupon_service.py:384 [argumento nomeado]    valid_until=coupon.valid_until,
src/repositories/coupon_repository.py:94   RestaurantCoupon.valid_until >= current
src/repositories/menu_repository.py:108    RestaurantCoupon.valid_until >= datetime.now(...)
```

**Eram TRÊS cópias da regra de janela** — duas em SQL, uma em Python. Consertar
duas delas faria a campanha permanente aparecer numa superfície e sumir na
outra, sem erro e sem log, com o lojista jurando que criou a campanha. É o
achado que só a varredura completa dá.

### O conserto

**`src/services/coupon_window.py` (novo) — UM lugar, as duas formas.**

- `dentro_da_janela` / `ja_comecou` / `ja_acabou`: o predicado em Python, que é
  o que **explica a recusa** (`not_started` × `expired`). Um `WHERE` não
  devolve motivo.
- `filtro_de_janela(agora)`: as mesmas condições em SQL, com o
  `valid_until IS NULL` que faltava. Sem ele, `NULL >= agora` não é falso — é
  **nulo** —, e a campanha permanente sumiria de toda consulta.

`evaluate`, `list_in_window` e a vitrine passaram a chamar daí.

**`_find_coupon` passou a recortar pela janela**, como pedido: as duas
superfícies do cliente (`preview` e `lock_and_validate_for_order`) recebem
`agora` obrigatório e o repositório aplica `filtro_de_janela`. "Aparece na
vitrine" e "chega ao checkout" viraram a mesma pergunta.

**O painel NÃO passa por lá.** `agora` é opcional no repositório e o admin não
o envia — ele precisa enxergar a campanha vencida para editá-la ou reativá-la.
Há teste para os dois lados.

**Um instante só por requisição.** `preview` e `lock_and_validate_for_order`
leem o relógio uma vez e passam o mesmo `agora` para a busca e para a
avaliação. Lendo duas vezes, o cupom podia passar pelo filtro e ser recusado
por `expired` na linha seguinte.

### A outra metade: os schemas mentiam junto

Consertar só o service deixaria a campanha permanente funcionando no checkout
e **derrubando a tela onde o lojista a criou**. Passaram a `datetime | None`:
`CouponCampaignFields` (entrada), `CouponAdminResponse` e
`CustomerCouponResponse`. E o model, `Mapped[datetime | None]`.

O validador `valid_until <= valid_from` só roda quando há data: campanha sem
fim não pode terminar antes de começar.

**`CouponUpdate` não precisou de nada**: `update_admin` usa
`exclude_unset=True`, então `{"valid_until": null}` já **tira** o prazo e um
PATCH que não mande o campo preserva o gravado. Mesma mecânica do `code`.

### O que mudou no contrato — o painel precisa saber

`openapi.json` regravado. Três mudanças:

- **`CouponAdminResponse.valid_until` e `CustomerCouponResponse.valid_until`
  passam a poder vir `null`.** O painel e o app precisam escrever "sem prazo"
  em vez de data vazia. **É a única mudança que pode quebrar cliente.**
- **`valid_until` saiu do `required` de `CouponCreate`.** Relaxamento: quem já
  mandava continua funcionando.

### O que mudou de COMPORTAMENTO, e como voltar atrás

Código de campanha **vencida** digitado no checkout respondia `expired` em
`ineligibility_reason`; passa a responder **404 "Cupom não encontrado"**. A
recusa continua correta e o desconto nunca saiu nos dois casos — o que mudou é
a frase.

Foi consequência direta de aplicar o filtro em `_find_coupon`, como pedido.
Está isolado numa linha: **se a mensagem específica importar mais que a defesa
em profundidade, é o `agora=agora` das duas chamadas de `_find_coupon` que
volta atrás.** Os ramos `not_started`/`expired` de `evaluate` continuam vivos e
cobertos, porque as outras chamadas dele (a lista do cliente e a
auto-aplicação) recebem cupom que já veio de `list_in_window`.

### Visto vermelho antes

`tests/test_janela_do_cupom.py`, 16 testes. Os dois principais falharam com
`AttributeError: 'NoneType' object has no attribute 'tzinfo'` em
`coupon_service.py:174`, dentro de `lock_and_validate_for_order` — o caminho do
dinheiro. Cobrem:

- o cupom sem prazo **atravessa o checkout inteiro** e aplica desconto;
- o cupom **vencido continua recusado** (o conserto não podia virar "todo prazo
  é opcional" — isso seria pior que o 500, porque o lojista paga desconto de
  campanha encerrada);
- código de campanha vencida e de campanha futura **não chegam a ser
  avaliados**;
- o painel **continua enxergando** a campanha vencida;
- as duas formas da regra concordam, e o `IS NULL` está na expressão SQL;
- **dois testes `db`**, contra o Postgres de verdade, porque `NULL >= agora`
  silencioso não se reproduz com dublê: a campanha permanente aparece nas
  **duas** superfícies, e a coluna **continua aceitando nulo** — este último é
  o único lugar do repositório que registra que ela é a **exceção** da lista de
  alinhamento.

### O efeito colateral que a ferramenta da rodada 2 pegou sozinha

Com o model corrigido, `restaurant_coupons.valid_until` deixou de ser
divergência: **42 → 41**, e as 16 viraram **15**.
`tests/test_revisoes_preparadas.py` ficou vermelho na hora — foi exatamente
para isso que ele existe. Atualizados: as duas revisões preparadas (a coluna
**saiu** da lista de alinhamento, com o motivo escrito), o `--limite` do CI e o
README.

E os dois testes da rodada 2 que **afirmavam o defeito** mudaram de lado: agora
afirmam que ele não volta.

### Arquivos

`src/services/coupon_window.py` (novo) · `src/services/coupon_service.py` ·
`src/repositories/coupon_repository.py` · `src/repositories/menu_repository.py` ·
`src/schemas/coupon_schema.py` · `src/models/coupon_model.py` ·
`scripts/leituras_de_coluna_nulavel.py` (novo) ·
`tests/test_janela_do_cupom.py` (novo) · `tests/test_coupons.py` ·
`tests/test_colunas_em_desacordo.py` · `alembic/preparadas/*` ·
`.github/workflows/ci.yml` · `README.md` · `openapi.json`.

**Portão: 2836 testes verdes** (2222 rápidos + 614 `db`), ruff limpo,
`openapi.json` regravado, lock em dia.

---

## 3. Redis e LGPD

### A resposta, antes de qualquer plano: **NÃO. A exclusão de conta não alcança nada do Redis.**

`CustomerAnonymizationService` **não menciona Redis em lugar nenhum** — grep no
arquivo inteiro não devolve uma linha. Ele trabalha só no Postgres: anonimiza a
linha do cliente, apaga endereços, códigos de verificação, cartões (inclusive no
gateway), tira a pessoa dos pedidos, limpa comentários de avaliação e — a
ironia deste item — **apaga as linhas de `delivery_estimates` pelo
`customer_id`**.

A cópia da MESMA estimativa que está no Redis não é tocada.

### E há dado pessoal no Redis hoje. Achei um que não estava anotado

**O pior é o cache de estimativa de entrega, e ele está na CHAVE:**

```
delivery-estimate:v1:{slug}:{branch_id}:{latitude:.4f}:{longitude:.4f}:...
```

`src/services/delivery_estimate_service.py:1018`. **Quatro casas decimais são
~11 metros** — isso identifica uma casa, não um bairro. E o valor guardado ao
lado é o `DeliveryEstimateResult` serializado em JSON, que **também** carrega
`latitude` e `longitude`.

Isso é exatamente o cuidado que `ChatCache.embedding_key` toma e explica com
todas as letras, três arquivos ao lado:

> **`{hash}` — o digest, e nunca a mensagem crua.** O que o cliente digita não
> vai para chave de Redis. É texto de pessoa, aparece em `KEYS`, em `MONITOR`,
> em qualquer dump.

A regra estava escrita. A estimativa de entrega não a seguiu — e o que ela põe
na chave é mais preciso que o texto: coordenada da porta da pessoa.

**Inventário completo do que está no Redis hoje:**

| Chave | Dado pessoal? | Onde | TTL |
|---|---|---|---|
| `delivery-estimate:v1:...` | **sim — coordenada da entrega, ~11 m** | na CHAVE e no valor | 600 s |
| rate limit (`slowapi`) | **sim — endereço IP** | na chave (`key_func=client_ip`) | janela do limite |
| `emb:v1:{modelo}:{restaurant_id}:{digest}` | não diretamente — a mensagem vai **hasheada**, e o valor é o vetor | — | 3600 s |
| `MenuGeneration` (contador) | não | — | — |

O embedding é o único que já nasceu certo, e nasceu certo **de propósito**.

### O que isso é, e o que isso não é

**O que É:** dado pessoal em sistema que a rotina de exclusão não alcança, e
que ela **não tem como alcançar** — a chave da estimativa não carrega
`customer_id`. Achar "as chaves desta pessoa" exigiria saber a coordenada dela
de antemão. Não é um `DELETE` faltando; é uma chave com a forma errada.

**O que NÃO é:** um vazamento aberto. A janela é curta (10 minutos para a
estimativa) e a expiração é automática. Quem pede exclusão às 14h00 tem a
coordenada fora do Redis, no pior caso, às 14h10 — **desde que não haja
persistência.**

### E é aí que a sua pergunta muda de tamanho

**A pergunta sobre `RDB`/`AOF` não é detalhe do plano de histórico de chat. Ela
é sobre dado que JÁ está no Redis hoje.**

Com persistência ligada, o TTL deixa de ser a garantia: um snapshot tirado no
minuto 5 grava a chave **em disco**, e o arquivo não expira sozinho. "10
minutos" vira "até alguém apagar o dump" — e dump costuma ir para backup.

Por isso este item vem **antes** do 5.1 da rodada 2, e não depois.

### Os comandos, para você rodar

**Os três são somente leitura.** Nenhum escreve, nenhum apaga, nenhum bloqueia
o Redis. `REDIS_URL` é a mesma do `.env` de produção.

```bash
# 1. A PERGUNTA QUE DECIDE: há persistência?
docker exec pedeaqui-api python -c "
import redis
from src.core.config import settings
r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
info = r.info('persistence')
print('aof_enabled          =', info.get('aof_enabled'))
print('rdb_last_save_time   =', info.get('rdb_last_save_time'))
print('rdb_changes_since    =', info.get('rdb_changes_since_last_save'))
print('rdb_bgsave_in_progress=', info.get('rdb_bgsave_in_progress'))
"
```

`INFO persistence` e não `CONFIG GET save`: **Redis gerenciado costuma
bloquear `CONFIG`**, e o `INFO` responde a mesma pergunta sem privilégio
nenhum. Se o seu aceitar `CONFIG`, `CONFIG GET save` e `CONFIG GET appendonly`
dão a configuração declarada, que é útil ter junto.

| Resposta | O que significa | O que fazer |
|---|---|---|
| `aof_enabled=0` **e** `rdb_last_save_time` antigo/zero | **sem persistência.** O TTL é a garantia e ela vale | nada urgente. O 5.1 pode seguir |
| `aof_enabled=1` | **AOF ligado** — toda escrita vai para disco | a coordenada sobrevive ao TTL. Ver "o que fazer" abaixo |
| `rdb_last_save_time` recente e mudando entre duas execuções | **RDB ligado** e salvando | mesma coisa |

```bash
# 2. QUANTO existe agora, sem imprimir nenhuma coordenada
docker exec pedeaqui-api python -c "
import redis
from src.core.config import settings
r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
for prefixo in ('delivery-estimate:*', 'emb:*', 'LIMITER*', 'ai:menu-generation:*'):
    print(f'{prefixo:28} {sum(1 for _ in r.scan_iter(match=prefixo, count=500))}')
print('total no banco       ', r.dbsize())
"
```

**`scan_iter` e não `KEYS`**: `KEYS` varre tudo de uma vez e trava o Redis
inteiro enquanto isso — num Redis de produção com o `estorno` e o `reindex`
batendo nele, é um jeito rápido de derrubar o rate limit da API.

E **conta, nunca imprime**. Listar as chaves de `delivery-estimate:*` põe
coordenada de cliente no seu terminal e no seu histórico de shell — que é o
mesmo problema que este item está descrevendo, feito à mão. Se você precisar
ver o formato de uma, tire a coordenada antes de colar em qualquer lugar.

```bash
# 3. O TTL está mesmo lá? (uma chave só, e sem imprimir a chave)
docker exec pedeaqui-api python -c "
import redis
from src.core.config import settings
r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
uma = next(r.scan_iter(match='delivery-estimate:*', count=100), None)
print('achou uma chave  =', uma is not None)
print('ttl em segundos  =', r.ttl(uma) if uma else 'nao ha nenhuma agora')
"
```

`ttl` respondendo `-1` é chave **sem expiração** — não deveria acontecer
(`setex` sempre põe TTL), e se acontecer é achado próprio.

### O que fazer em cada resposta

**Sem persistência** — o cenário provável, e o bom. O TTL de 10 min é a
garantia, ela funciona, e o resíduo da exclusão de conta é de minutos. Continua
valendo a pena tirar a coordenada da chave (abaixo), mas deixa de ser urgente,
e o plano do histórico de chat (5.1 da rodada 2) pode seguir como está escrito.

**Com `RDB` ou `AOF` ligado** — três saídas, e a ordem é de decisão sua:

1. **Desligar a persistência.** Nada do que este Redis guarda é durável por
   desenho: cache de embedding, cache de estimativa, contador de rate limit e
   um contador de geração. Perder tudo num restart custa alguns segundos de
   latência a mais e **zero** correção. É a saída que eu recomendaria.
2. **Manter a persistência e tirar a coordenada da chave** — trocar
   `{lat:.4f}:{lon:.4f}` por um digest, como `ChatCache.embedding_key` já faz
   com a mensagem. O cache continua funcionando igual (o digest é
   determinístico), e a chave deixa de dizer onde a pessoa mora. **Isto vale
   fazer de qualquer jeito**, e não depende da resposta.
3. **Manter tudo e registrar** — se houver motivo operacional para a
   persistência, então o dump entra no inventário de dados pessoais e ganha
   prazo de retenção próprio. É a saída mais cara, e a única que exige decisão
   além de código.

**O IP do rate limit é caso à parte.** Ele é inerente ao mecanismo — sem
identificar quem chama não há limite por cliente —, a janela é de minutos, e
o `docs/lgpd-proposta.md` é onde ele deve ser declarado, não consertado.

### O que fica pendente, e é decisão sua

- **rodar os três comandos** (só leitura);
- **a saída 2 acima** — o digest na chave da estimativa — é código, cabe numa
  rodada, e não depende da resposta dos comandos. Não fiz nesta porque muda o
  formato da chave de um cache de produção e isso é deploy, não conserto de
  teste;
- **declarar o Redis no `docs/lgpd-proposta.md`**, que hoje não o menciona.
  Independente da resposta: o inventário de dados pessoais está incompleto
  enquanto ele estiver de fora.
