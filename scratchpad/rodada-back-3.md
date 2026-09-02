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
| 3 | Redis e LGPD — o comando, e se a exclusão alcança o Redis | pendente |
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
