# Rodada de pendências do backend — fonte da verdade

Branch: `rodada/backend`. Nunca commitar na `main`. Um commit por item, verde,
com push.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa.**

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 1 | `tracking_token` — plano das duas etapas | **feito** (só plano; nada executado) |
| 2 | `print_agent` — criar a conta | **feito** (comando escrito; conta NÃO criada — exige o banco) |
| 3 | Custo de IA por restaurante | **feito** |
| 4.1 | Quebrar a tabela `branches` | **feito** (levantamento + plano; PARADO antes de executar) |
| 4.2 | README | **feito** |
| 4.3 | Diagrama ER | **feito** |

Fora desta rodada, e anotado se cruzar: `float × Decimal`, entregadores,
máquina de estados do pedido, histórico de chat sem Redis, assistente de voz,
merge na `main`.

---

## 1. `tracking_token` — plano do deploy em duas etapas

**Nada foi executado.** Esta seção é o roteiro; quem digita é você.

O roteiro longo já existe em `docs/deploy-hash-do-tracking-token.md` e continua
válido. O que está aqui é o que ele **não** responde: quanto tempo trava, o que
acontece se falhar no meio, o que fazer no meio da noite, e — o mais
importante — a suspeita de que este deploy **já foi feito**.

### 1.0 A primeira coisa: isto provavelmente já está aplicado

Antes de planejar o deploy, confira se ele ainda existe. Três indícios apontam
para "já foi", e nenhum deles é prova:

1. **A imagem em produção é recente.** A armadilha 49 registra um pedido real
   de R$ 0,01 no Júnior da Picanha em **28/08/2026**, com **cartão salvo** —
   funcionalidade da revisão `20260825_0040`. Se aquilo aconteceu em produção,
   o banco está em `0040` ou depois, e `0016`/`0017` ficaram para trás no
   caminho.
2. **O model não mapeia `orders.tracking_token`.** Antes da `0016` essa coluna
   é `NOT NULL`. Se o banco estivesse em `0015`, **todo pedido novo falharia no
   INSERT** — e pedidos estão entrando.
3. O `docker-entrypoint.sh` roda `alembic upgrade head` em todo deploy. Só um
   `ALEMBIC_TARGET` esquecido no `.env` seguraria a fila, e o entrypoint grita
   `ATENCAO` no log quando isso acontece.

**O comando que responde, e é só leitura:**

```bash
docker exec pedeaqui-api python scripts/estado_da_producao.py
```

Ele diz o `git_sha` da imagem, o alvo do alembic e a revisão do banco contra o
que existe no repositório. Se a revisão do banco for `20260828_0043` (o head de
hoje), **não há deploy pendente e o resto desta seção não se aplica** — pare
aqui.

A conferência que não depende da tabela `alembic_version` (é o schema
respondendo, e também é só leitura):

```bash
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    print(db.execute(text('''
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'orders'
           AND column_name IN ('tracking_token','tracking_token_hash')
         ORDER BY column_name
    ''')).scalars().all())
"
```

| Saída | Onde você está |
|---|---|
| `['tracking_token_hash']` | **as duas já rodaram.** Nada a fazer |
| `['tracking_token','tracking_token_hash']` | entre as duas etapas |
| `['tracking_token']` | antes da `0016` — o roteiro abaixo vale inteiro |

### 1.1 As duas etapas, na ordem, e o que cada uma faz

**Etapa 1 — revisão `20260812_0016`.** Numa transação só:

1. `ALTER TABLE orders ADD COLUMN tracking_token_hash text` (nullable);
2. backfill em Python: lê 1000 linhas por vez e manda **um `UPDATE` por
   linha** com o sha-256 do token que já está gravado;
3. `CREATE UNIQUE INDEX ix_orders_tracking_token_hash` (sem `CONCURRENTLY`);
4. `ALTER TABLE orders ALTER COLUMN tracking_token DROP NOT NULL`.

Nenhum token é regerado: link que já está no WhatsApp de cliente continua
funcionando. **Esta etapa só acrescenta.**

**Etapa 2 — revisão `20260812_0017`.** Também numa transação só:

1. roda o backfill de novo (varre o que tenha entrado por container com código
   antigo);
2. conta `tracking_token_hash IS NULL` — **se sobrar qualquer linha, levanta e
   a transação inteira volta, antes do DROP**;
3. `ALTER TABLE orders ALTER COLUMN tracking_token_hash SET NOT NULL`;
4. `ALTER TABLE orders DROP COLUMN tracking_token`.

O passo 4 é o ponto sem volta.

### 1.2 O que fica travado, e por quanto tempo

**O `docs/deploy-hash-do-tracking-token.md` erra por baixo aqui, e vale
corrigir na sua cabeça antes de abrir a janela.** Ele diz que "o `CREATE
UNIQUE INDEX` trava escrita em `orders` enquanto constrói". Trava mais que
isso:

- `alembic/env.py` abre **uma transação para o upgrade inteiro**
  (`context.begin_transaction()` em volta de `run_migrations`, sem
  `transaction_per_migration`);
- o **primeiro** comando da `0016` é `ALTER TABLE ... ADD COLUMN`, que toma
  **`ACCESS EXCLUSIVE`** sobre `orders`;
- lock de transação só é solto no `COMMIT`.

Logo: **`orders` fica em `ACCESS EXCLUSIVE` do primeiro comando até o commit**
— não só durante o índice, e não só para escrita. `ACCESS EXCLUSIVE` bloqueia
**`SELECT` também**.

Quem sente isso, já que a API está fora do ar durante a migração (o entrypoint
migra antes do Uvicorn):

- os containers `limpeza`, `estorno` e `reindex`, que **continuam de pé** e
  falam com o mesmo banco. O `estorno` roda a cada 15 min e escreve em
  `orders`; ele vai **esperar no lock** (ou estourar o timeout do statement, se
  houver um configurado no Supabase) e o laço dele tenta de novo em 5 min. Não
  é perigoso — é ruído no log que você precisa reconhecer;
- qualquer sessão sua aberta no SQL editor do Supabase contra `orders`.

**Duração — o que domina é o backfill, não o índice.** O backfill é
`N` round-trips ao banco (um `UPDATE` por pedido) mais `N/1000` `SELECT`s.
Round-trip para o Supabase de dentro do container é o que manda:

| pedidos em `orders` | a 3 ms/round-trip | a 20 ms/round-trip | a 60 ms/round-trip |
|---|---|---|---|
| 1 000 | ~3 s | ~20 s | ~1 min |
| 10 000 | ~30 s | ~3,5 min | ~10 min |
| 50 000 | ~2,5 min | ~17 min | ~50 min |
| 100 000 | ~5 min | ~35 min | ~1 h 40 |

O `CREATE UNIQUE INDEX` em cima disso é ruído: btree sobre um `text` de 64
caracteres, ordem de **segundos** até uns 100 mil registros.

**Nenhum desses números é medido — eu não consulto produção nesta sessão.** Os
dois que faltam, e os dois comandos que os dão (os dois são só leitura):

```bash
# 1. quantas linhas o backfill vai percorrer
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    print(db.execute(text('SELECT count(*) FROM orders')).scalar_one(), 'pedidos')
    print(db.execute(text(
        'SELECT pg_size_pretty(pg_total_relation_size(\'orders\'))'
    )).scalar_one())
"

# 2. quanto custa UM round-trip daqui até o banco
docker exec pedeaqui-api python -c "
from time import perf_counter
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    db.execute(text('SELECT 1'))          # aquece a conexao
    inicio = perf_counter()
    for _ in range(200):
        db.execute(text('SELECT 1'))
    print(f'{(perf_counter()-inicio)/200*1000:.2f} ms por round-trip')
"
```

Multiplique um pelo outro e você tem a janela, com uma folga de 2x para o
índice, o commit e o `alembic upgrade` das revisões que vierem junto.

### 1.3 Se a etapa falhar no meio

**Etapa 1 (`0016`) falhando:** transação única, então **volta inteira** — sem
coluna, sem índice, sem meia-linha preenchida. O container morre, o
`restart: always` sobe de novo, o entrypoint tenta a migração de novo, morre de
novo: **loop de restart visível em `docker ps`**, com a API fora do ar. É o
comportamento desenhado (armadilha 5) e é preferível a uma API de pé contra o
schema errado. Nada foi perdido; leia a saída do alembic antes de qualquer
outra coisa.

O modo de falha que **não** volta sozinho: você derruba o processo no meio
(Ctrl-C, `docker stop`, OOM do container). A transação é abortada pelo
Postgres quando a conexão cai, então o schema volta igual — mas o
`pg_advisory_xact_lock` do `alembic/env.py` também é de transação e sai junto.
Não fica lock órfão.

**Etapa 2 (`0017`) falhando:** três lugares, três consequências diferentes:

1. falha **no backfill ou na contagem** (`RuntimeError: N pedido(s) sem
   tracking_token_hash`) — a transação volta, `orders.tracking_token`
   **continua lá**, nada foi perdido. É o desenho: falhar antes do DROP;
2. falha **no `SET NOT NULL`** — mesma coisa, volta inteira;
3. falha **depois do `DROP COLUMN`, antes do commit** — volta inteira também.
   Não existe "meio DROP".

O que **não** volta é o commit bem-sucedido. Depois dele, a coluna em claro não
existe em lugar nenhum e sha-256 não se desfaz.

**E há um risco na etapa 2 que o doc não destaca o suficiente:** tirar o
`ALEMBIC_TARGET` do `.env` não libera só a `0017`. Libera **da `0017` até o
head**, hoje `20260828_0043` — 27 revisões, **na mesma transação**, incluindo a
`20260820_0026` (cardápio por filial), que **copia dados** e cuja ordem interna
é sensível a banco com conteúdo. Se produção estiver mesmo em `0015`, a etapa 2
não é "apagar uma coluna": é o resto do repositório inteiro de uma vez. Nesse
cenário, faça a etapa 2 em duas partes, com `ALEMBIC_TARGET=20260812_0017`
primeiro e só depois sem variável nenhuma.

### 1.4 Como voltar de cada etapa

**Da etapa 1: dá para voltar, com uma ressalva que morde.**

```bash
docker exec pedeaqui-api alembic downgrade 20260812_0015
# e volte a imagem antiga (o código novo não escreve mais a coluna em claro)
```

A ressalva: o `downgrade` da `0016` só recoloca o `NOT NULL` em
`tracking_token` **se nenhuma linha estiver nula**. Todo pedido criado enquanto
o código novo esteve no ar tem `tracking_token` nulo — o model não escreve mais
essa coluna. Então:

- a coluna volta **nullable**, com um `RAISE NOTICE` explicando;
- e o código antigo, que busca por `tracking_token`, **não acha os pedidos
  criados na janela**. Esses clientes perdem o acompanhamento, e não há rota de
  reemissão. Quanto mais tempo a etapa 1 ficar no ar, maior esse buraco no
  rollback.

Tradução operacional: o rollback da etapa 1 é barato **nas primeiras horas** e
vai ficando caro. Não é uma saída que envelhece bem.

**Da etapa 2: NÃO HÁ VOLTA. Digo isso em voz alta.** O `downgrade` da `0017`
recria a coluna **vazia** — o schema volta, o acesso não. Os tokens em claro
deixaram de existir e sha-256 não se desfaz. O único caminho de volta é
**restaurar backup do banco**, e restaurar backup significa perder tudo o que
entrou desde o backup (pedidos, pagamentos, cashback). Na prática: **depois do
commit da etapa 2, você não volta.**

Por isso: **faça backup do banco imediatamente antes da etapa 2**, e confira
que o arquivo existe e tem tamanho plausível antes de rodar qualquer coisa.

### 1.5 Qual a janela segura

**Não tenho resposta segura para isto, e não vou inventar uma.** O horário de
funcionamento mora em `branch_business_hours`, no banco de produção, e eu não
consulto produção nesta sessão. Nada no repositório guarda o horário do Júnior:
grep por `junior`/`matriz`/`varjota` em `docs/` só devolve exemplo de
onboarding.

O comando que responde (só leitura), e ele já traz as duas filiais:

```bash
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    for linha in db.execute(text('''
        SELECT b.name, h.weekday, h.opens_at, h.closes_at, h.is_closed
          FROM branch_business_hours h
          JOIN branches b ON b.id = h.branch_id
          JOIN restaurants r ON r.id = b.restaurant_id
         WHERE r.slug = 'junior-da-picanha'
         ORDER BY b.name, h.weekday, h.opens_at
    ''')):
        print(linha)
"
```

**Cuidado ao ler:** `weekday` aqui é o do Python — **0 = segunda, 6 = domingo**
(armadilha 1). E faixa que vira a noite (18:00–02:00) pertence ao dia em que
**começa**: uma loja que fecha às 02:00 de domingo aparece como faixa do
sábado.

O que dá para dizer sem consultar: a janela tem que caber **depois do último
pedido do dia e antes da primeira abertura seguinte**, com folga de 2x a
estimativa da seção 1.2, e não pode ser madrugada de sexta ou sábado — que em
churrascaria é o pico, e é justamente quando a faixa vira a noite.

### 1.6 O passo a passo, comando por comando

Tudo daqui é o que **você** digita, no servidor. Nada disto foi executado.

```bash
# ── PASSO 0 — onde a produção está (só leitura) ──────────────────────────
docker exec pedeaqui-api python scripts/estado_da_producao.py
# Se a revisão do banco já for 20260828_0043 → PARE, não há deploy pendente.

# ── PASSO 0b — dimensionar a janela (só leitura) ─────────────────────────
#   os dois comandos da seção 1.2 (contagem de pedidos e ms por round-trip)

# ── PASSO 0c — BACKUP. Não pule. ─────────────────────────────────────────
#   pelo painel do Supabase, ou pg_dump da DATABASE_URL.
#   Confira o tamanho do arquivo antes de seguir.

# ── ETAPA 1 ──────────────────────────────────────────────────────────────
cd /caminho/do/projeto
echo "ALEMBIC_TARGET=20260812_0016" >> .env
grep ALEMBIC_TARGET .env                     # confira que ficou exatamente isso

GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
docker logs -f pedeaqui-api
#   esperado, nesta ordem:
#     [entrypoint] alembic upgrade 20260812_0016
#     [entrypoint] ATENCAO: ALEMBIC_TARGET=... — o banco NAO esta em head.
#     [entrypoint] iniciando: uvicorn ...
#   o ATENCAO é esperado NESTA etapa.

# ── CONFERÊNCIA (a parte que não se pula) ────────────────────────────────
# 1. todo pedido tem hash — tem que dizer "0 sem hash"
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    sem = db.execute(text('SELECT count(*) FROM orders WHERE tracking_token_hash IS NULL')).scalar_one()
    total = db.execute(text('SELECT count(*) FROM orders')).scalar_one()
    print(f'{sem} sem hash, de {total} pedidos')
"

# 2. um link ANTIGO de verdade (do WhatsApp da loja, de antes do deploy) abre:
#    GET https://api.pederapidex.com/restaurants/{slug}/orders/track/{token}
#    Tem que responder 200. Pedido criado DEPOIS do deploy não serve —
#    ele nasceu com hash e passaria mesmo com o backfill errado.
#    Se der 404: PARE. Não faça a etapa 2.

# 3. feche um pedido de teste e abra o link que ele devolveu.

# 4. deixe assar. UM DIA INTEIRO de operação normal, no mínimo.

# ── ETAPA 2 (ponto sem volta) ────────────────────────────────────────────
#   refaça a conferência 1 imediatamente antes. E o backup de novo.
sed -i '/^ALEMBIC_TARGET=/d' .env
grep ALEMBIC_TARGET .env                     # não pode sobrar nada
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d
docker logs -f pedeaqui-api
#   esperado: [entrypoint] alembic upgrade head   e NENHUM "ATENCAO".

# ── CONFERÊNCIA FINAL ────────────────────────────────────────────────────
docker exec pedeaqui-api python scripts/estado_da_producao.py
#   a coluna em claro tem que ter sumido; a revisão do banco tem que ser o
#   head do repositório (hoje 20260828_0043), não a 0017.
```

### 1.7 O que continua sem resposta segura

- **quantas linhas tem `orders` hoje** — não consultei produção; comando na
  seção 1.2;
- **quanto custa um round-trip até o Supabase** — idem;
- **o horário de fechamento das duas filiais** — idem, seção 1.5;
- **se o Supabase tem `statement_timeout` configurado** — se tiver, e for menor
  que a duração do backfill, a etapa 1 morre no meio (volta inteira, sem
  estrago, mas você fica sem saber por quê). Confira com
  `SHOW statement_timeout;` antes;
- **se existe backup automático e qual a idade dele** — a etapa 2 depende
  disso e eu não tenho como ver.

Plano incompleto é melhor que plano confiante e errado: os cinco acima são
buracos conhecidos, e cada um tem o comando que o fecha.
