# Deploy do hash do `tracking_token` — roteiro das duas etapas

As revisões `20260812_0016` e `20260812_0017` tiram o token de acompanhamento
do texto puro. **A segunda não tem volta**: sha-256 não se desfaz, e o
`downgrade` dela recria a coluna vazia. Por isso são duas, e por isso existe
uma janela de conferência entre elas.

Fora do horário de movimento — o `CREATE UNIQUE INDEX` da 0016 trava escrita
em `orders` enquanto constrói, e o entrypoint migra com a API fora do ar
(armadilha 5).

**Faça backup do banco antes de começar.** Depois da etapa 2, restaurar backup
é o único caminho de volta que existe.

---

## Passo 0 — onde o banco está agora

Não pule. **Nada no repositório responde isto** — o `schema_baseline.sql` é uma
foto congelada na revisão `0012`, e o histórico de commits diz quando a revisão
foi escrita, não quando ela foi aplicada.

```bash
docker exec pedeaqui-api alembic current
```

E a conferência que não depende da tabela `alembic_version` — é o schema de
verdade respondendo:

```bash
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    colunas = db.execute(text(\"\"\"
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'orders'
           AND column_name IN ('tracking_token', 'tracking_token_hash')
         ORDER BY column_name
    \"\"\")).scalars().all()
    print(colunas)
"
```

| O que sai | Onde você está | O que fazer |
|---|---|---|
| `['tracking_token_hash']` | **as duas já foram aplicadas** | Nada. Não há deploy pendente; confirme com `alembic current` e pare aqui |
| `['tracking_token', 'tracking_token_hash']` | na janela entre as duas | A etapa 1 já foi. Vá para **"O que conferir entre as duas"** e depois para a **etapa 2** |
| `['tracking_token']` | antes da 0016 | As duas etapas, na ordem abaixo |

Se a terceira linha for a sua, saiba o que ela também significa: a API está
rodando um código que **não escreve** `tracking_token` (o model não mapeia a
coluna) contra um banco onde ela é `NOT NULL`. Todo pedido novo estaria
falhando. Se não está, é porque a imagem em produção é antiga — e aí a etapa 1
traz o código junto com a revisão, como sempre foi para ser.

---

## Etapa 1 — acrescentar o hash, mantendo o token em claro

A 0016 acrescenta `tracking_token_hash`, calcula o hash **a partir dos tokens
que já estão gravados**, cria o índice UNIQUE e tira o `NOT NULL` da coluna em
claro. Nenhum token é regerado: **todo link que já está no WhatsApp de alguém
continua funcionando.**

O `ALEMBIC_TARGET` existe para esta etapa. Sem ele, `upgrade head` aplicaria
0016 **e** 0017 no mesmo segundo, e a janela de conferência não existiria — as
duas revisões estão na mesma imagem desde 12/08/2026, então o "deploy do commit
que termina na 0016" que o docstring da 0017 descreve não é mais alcançável.

```bash
# 1. a variável, ANTES de subir
echo "ALEMBIC_TARGET=20260812_0016" >> .env

# 2. sobe a imagem nova; o entrypoint migra até a 0016 e para lá
docker compose up -d --build

# 3. acompanhe a migração — é aqui que ela falha, se falhar
docker logs -f pedeaqui-api
```

No log tem que aparecer, nesta ordem:

```
[entrypoint] alembic upgrade 20260812_0016
[entrypoint] ATENCAO: ALEMBIC_TARGET=20260812_0016 — o banco NAO esta em head.
[entrypoint] iniciando: uvicorn ...
```

O `ATENCAO` é esperado nesta etapa. Ele existe para o caso oposto: a variável
esquecida no `.env` depois, quando toda revisão nova deixaria de ser aplicada
com o deploy passando verde.

**Se a migração falhar**, o container morre e o `restart: always` tenta de novo
— um loop visível em `docker ps`. Leia a saída do alembic antes de qualquer
outra coisa. Nada foi perdido: a 0016 só acrescenta.

---

## O que conferir entre as duas

**Esta é a parte que não pode ser pulada, e é a razão de existirem duas
etapas.** Enquanto você está aqui, dá para voltar atrás a qualquer momento: a
coluna em claro ainda está lá e ainda está preenchida.

### 1. Todo pedido tem hash

O número que a etapa 2 vai exigir. Se não for zero, ela **falha antes do
DROP** — de propósito —, e você teria descoberto isso na hora errada.

```bash
docker exec pedeaqui-api python -c "
from sqlalchemy import text
from src.db.session import SessionLocal
with SessionLocal() as db:
    sem = db.execute(text(
        'SELECT count(*) FROM orders WHERE tracking_token_hash IS NULL'
    )).scalar_one()
    total = db.execute(text('SELECT count(*) FROM orders')).scalar_one()
    print(f'{sem} sem hash, de {total} pedidos')
"
```

Tem que dizer **`0 sem hash`**. Qualquer outro número é um pedido cujo
acompanhamento sumiria, e não há como devolvê-lo.

### 2. Um link ANTIGO de verdade abre

O passo que mais gente pula, e o único que prova o que interessa: que o hash foi
calculado **a partir dos tokens que já existiam**, e não que a rota funciona
para pedidos novos.

Pegue um link real, de antes do deploy — no WhatsApp da loja, no histórico de
um cliente — e abra:

```
GET https://api.pederapidex.com/restaurants/{slug}/orders/track/{token}
```

**Tem que responder 200 com o pedido certo.** Um pedido criado depois do deploy
não serve para esta conferência: ele nasceu com hash, e passaria mesmo que o
backfill tivesse errado.

Se responder 404, **pare**. Não faça a etapa 2. Com a coluna em claro ainda no
lugar, dá para investigar (o backfill é idempotente e pode ser rodado de novo) e
voltar a imagem antiga.

### 3. Pedido NOVO fecha e é rastreável

Feche um pedido de teste e abra o link que ele devolve. Isto prova a outra
metade: que o código novo grava o hash sozinho, sem depender do backfill.

### 4. Deixe assar

Não emende as duas etapas na mesma janela. O que se está esperando é o
cliente que guardou o link há dias abrir o dele — e é justamente esse caso que
nenhum teste da suíte cobre, porque só existe no banco de produção.
**Um dia inteiro de operação normal é o mínimo razoável.** Enquanto isso, o
grep que interessa:

```bash
docker logs pedeaqui-api | grep -i "track"
```

---

## Etapa 2 — apagar a coluna em claro

**Ponto sem volta.** A 0017 roda o backfill de novo (para varrer o que tiver
entrado por um container com código antigo — rollback de imagem, deploy pela
metade), **falha se sobrar alguma linha sem hash**, põe `NOT NULL` no hash e só
então apaga `orders.tracking_token`.

Refaça a conferência 1 imediatamente antes. Ela é barata e é a última chance.

```bash
# 1. TIRE a variável do .env — é isto que libera o resto das revisões
sed -i '/^ALEMBIC_TARGET=/d' .env
grep ALEMBIC_TARGET .env    # não pode sobrar nada

# 2. sobe; o entrypoint volta a ir até head
docker compose up -d

# 3. acompanhe
docker logs -f pedeaqui-api
```

No log:

```
[entrypoint] alembic upgrade head
```

e **nenhuma** linha de `ATENCAO`. Se ela aparecer, a variável ficou no `.env` e
nada foi aplicado.

Confirme o estado final com o comando do passo 0: tem que sair
`['tracking_token_hash']`, e `alembic current` tem que mostrar a revisão mais
nova do repositório — não a `0017`. Tirar a variável libera **todas** as
revisões posteriores de uma vez, e isso é o esperado: elas já estavam na
imagem, esperando.

---

## O que sobra depois, e não é fechado por isto

`idempotency_keys.response_body` guarda a resposta inteira da criação do
pedido, **incluindo o `tracking_token` em claro**, por até 24h (armadilha 19).
O hash não alcança essa cópia. Ela sai sozinha pelo expurgo do container
`limpeza`; conta na hora de decidir quem lê aquela tabela.

E não existe rota de reemissão de token, nem passa a existir: o segredo **é** a
chave de acesso, e depois da etapa 2 nem o servidor consegue reemiti-lo — o
banco só tem o hash.
