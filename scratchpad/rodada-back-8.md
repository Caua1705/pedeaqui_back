# Rodada 8 do backend — fonte da verdade

Branch: `rodada/backend-8`, saindo de `rodada/backend-7`. Nunca commitar na
`main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Rodadas anteriores: `rodada-back.md` a `rodada-back-7.md`.

Os três itens vieram dos outros dois repositórios (painel e app).

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 1 | `sort_order` dos grupos de opção e das opções não chega na vitrine | **feito** |
| 2 | Cliente logado não cancela o próprio pedido de outro aparelho | pendente |
| 3 | Vitrine ordena cupom por `sort_order` e o painel não define esse valor | pendente |

---

## 1. A ordem do cardápio

### O relato estava certo, e faltava metade dele

`Product.option_groups` e `ProductOptionGroup.options` eram
`relationship(...)` sem `order_by`, e **`selectinload` não ordena**: ele emite
um segundo `SELECT ... WHERE fk IN (...)` sem `ORDER BY` nenhum. O Postgres
devolve na ordem que for barata — na prática, a de inserção.

São **seis** `selectinload(Product.option_groups).selectinload(...options)`
espalhados por três repositórios (`menu_repository`, `product_repository` ×4,
`admin_menu_repository`). Nenhum ordenava nada.

**A metade que o relato não mencionava:** a única consulta que ordenava era
`AdminMenuRepository.list_option_groups`, e ela ordenava **só os grupos** —
`.order_by(sort_order, name)`. As opções de dentro de cada grupo saíam soltas
**também no painel**. Então não era "painel ordenado × vitrine desordenada": as
opções não tinham ordem em lugar nenhum.

### O conserto: uma definição, não duas parecidas

O pedido foi "a ordem tem que ser a mesma nas duas". Fazer isso com dois
`.order_by` iguais seria repetir, uma camada acima, exatamente o defeito que se
está consertando — duas expressões parecidas divergem no dia em que alguém
ajusta uma.

`relationship(order_by=...)` **aceita callable** (conferido antes de escrever).
Então a ordem virou duas funções em `src/models/product_option_model.py`:

```python
def ordem_dos_grupos() -> list:
    return [ProductOptionGroup.sort_order, ProductOptionGroup.name, ProductOptionGroup.id]

def ordem_das_opcoes() -> list:
    return [ProductOption.sort_order, ProductOption.name, ProductOption.id]
```

e as três pontas chamam **a função**: os dois `relationship` e o
`.order_by(*ordem_dos_grupos())` de `list_option_groups`. Consertar no
`relationship` é o que alcança os seis `selectinload` de uma vez — um
`.order_by` por consulta deixaria o sétimo sem ordem de novo, e calado.

Funções e não listas prontas também por um motivo mecânico: chamadas na
definição da classe, elas rodariam antes de `ProductOption` existir. O callable
só é avaliado quando o mapper é configurado.

### O `id` no fim não é decoração

`sort_order` é `DEFAULT 0` nas duas tabelas, então **enquanto o lojista não
arrastar nada, todo mundo tem 0** e o desempate decide a ordem inteira. Sem uma
chave única no fim, duas linhas com o mesmo `sort_order` e o mesmo `name` podem
sair trocadas entre duas requisições — o cardápio piscando sem nada ter mudado.
Há teste exigindo que a última chave seja `primary_key`.

`created_at` seria mais expressivo, mas não serve: duas linhas criadas na mesma
transação compartilham `now()`, então a ordem continuaria parcial.

**Nulo não é problema aqui:** as duas colunas são `NOT NULL DEFAULT 0` no
schema. (O ORM diz `Mapped[int | None]` — é uma das divergências já catalogadas
em `docs/alinhamento-orm-schema.md`, e continua parada esperando a etapa 0.)

### Os testes, e o vermelho

`tests/test_ordem_do_cardapio.py`, 7 testes. Os de banco são os que valem: sem
`ORDER BY`, a ordem de um `SELECT` é decisão do planejador, e planejador não se
dubla.

**Visto vermelho antes**, com o `order_by` desligado e as funções mantidas (a
primeira tentativa deu `ImportError`, que não é vermelho de verdade — é o teste
não carregando). Os seis falharam, e a mensagem é o defeito relatado:

```
assert ['Bebida', 'Tamanho', 'Borda'] == ['Tamanho', 'Borda', 'Bebida']
```

A fábrica cadastra **fora de ordem de propósito**. Inserindo já na ordem certa o
teste passaria com ou sem `ORDER BY` — verde pelo motivo errado.

O teste de "uma definição só" também foi conferido por mutação: trocando o
callable por uma lista escrita à mão no `relationship`, ele fica vermelho. É a
regressão que ele existe para pegar.

### Arquivos

`src/models/product_option_model.py` · `src/models/product_model.py` ·
`src/repositories/admin_menu_repository.py` · `tests/fabricas_db.py` (as duas
fábricas ganharam `sort_order`) · `tests/test_ordem_do_cardapio.py` (novo).

**Portão: 2920 verdes** (2297 rápidos + 623 `db`), ruff limpo, `openapi.json` em
dia, varredura de escrita/commit em 0.

### Para o front: nada muda no contrato

Nenhum campo novo, nenhum campo removido — só a ordem dos arrays
`option_groups` e `option_groups[].options` passa a ser a do painel. Quem já
ordenava do lado do cliente pode parar; quem não ordenava passa a ver a ordem
certa sozinho.
