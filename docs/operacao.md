# Operação

Subir, migrar, ler log e diagnosticar. Para "como eu rodo isso na minha máquina",
o `README.md` da raiz é mais curto e basta.

---

## 1. Deploy

```bash
docker compose up -d --build
docker logs -f pedeaqui-api
```

O compose **não expõe porta**: o Traefik roteia pela rede externa `n8n_default`,
pelas labels em `docker-compose.yml`. O container roda como usuário sem
privilégios (uid 10001) e com `/app` somente-leitura.

### O entrypoint migra antes de servir

**Isto mudou e é a coisa mais importante deste documento.**

`docker-entrypoint.sh` roda `alembic upgrade head` e **só então** passa o `CMD`
adiante. Não há `|| true`: se a migração falhar, o container morre e o
`restart: always` tenta de novo.

```sh
set -e
alembic upgrade head
exec "$@"        # uvicorn
```

Duas consequências que você precisa ter na cabeça:

1. **Migração ruim vira loop de restart.** Um banco fora do ar ou uma revisão que
   falha deixa o container reiniciando em ciclo, visível no `docker ps`. Isso é
   melhor que uma API de pé respondendo contra o schema errado — mas quando o
   container "não sobe", **olhe a saída do alembic antes de qualquer outra
   coisa**.
2. **`CREATE INDEX` em tabela grande derruba a operação.** Sem `CONCURRENTLY`, o
   comando trava escrita na tabela enquanto roda, e agora essa espera acontece com
   a API fora do ar — nenhum pedido novo é gravado. Aplicar uma revisão dessas em
   horário de pico é derrubar a loja. Rode fora do movimento, ou faça a troca à
   mão com `CREATE INDEX CONCURRENTLY` (que não roda dentro de transação) e depois
   `alembic stamp <revisão>`.

O entrypoint fica como `ENTRYPOINT`, e não embutido no `CMD`, para que
sobrescrever o comando (`docker compose run api bash`) continue migrando primeiro.

### Quebra de linha

`.gitattributes` força LF em `*.sh`, `Dockerfile` e `docker-compose.yml`. O
repositório é editado no Windows com `core.autocrlf=true`, e um shebang terminado
em `\r` vira `exec /app/docker-entrypoint.sh: no such file or directory` dentro do
container.

---

## 2. Migrações

Fonte da verdade: `alembic/versions/` (12 revisões hoje). A pasta `migrations/` é
**arquivo histórico congelado** dos 12 `.sql` aplicados à mão — não rode nada de
lá.

### Banco que já tem o schema mas nunca passou pelo Alembic

Uma vez só, **antes** do primeiro `up`. Não executa DDL:

```powershell
alembic stamp 20260726_0001
alembic current    # deve mostrar 20260726_0001
```

Sem isso, o `upgrade` tenta aplicar da 0002 em diante sobre tabelas que já
existem.

### Dia a dia

```powershell
alembic upgrade head
alembic revision -m "descricao"
alembic revision --autogenerate -m "descricao"
alembic downgrade -1
alembic history --verbose
```

### `--autogenerate` quer apagar metade do banco

O schema de produção nasceu antes do Alembic e tem objetos que o ORM não mapeia:
sequences (incluindo a de `order_number`), defaults e índices criados à mão. O
`--autogenerate` compara ORM × banco e propõe `DROP` em tudo que não achar no
ORM.

**Sempre leia o arquivo gerado antes de aplicar, e apague as linhas de drop.**
O aviso está em `alembic/env.py`.

### Índices: `if_not_exists` sim ou não

- Índice sobre tabela **do baseline** (que já existe em produção, possivelmente
  criado à mão): use `if_not_exists=True` no create e `if_exists=True` no drop.
- Índice sobre tabela **criada na própria revisão**: fica estrito. A tabela nasce
  vazia, não há colisão possível, e a permissividade só esconderia erro.

`if_not_exists` casa por **NOME, não por definição**. Dois índices com nomes
diferentes sobre a mesma coluna são, para o Postgres, dois índices — mantidos os
dois em toda escrita. `scripts/audit_indexes.py` procura as duas coisas.

Faça backup antes. É Supabase/Postgres; DDL ruim custa caro.

---

## 3. Ver logs

Todo log da aplicação vai para o logger `uvicorn.error`.

```bash
docker logs -f pedeaqui-api
docker logs pedeaqui-api --since 30m | grep "\[Delivery estimate debug\]"
docker logs pedeaqui-api --since 1h  | grep "\[Idempotency\]"
docker logs pedeaqui-api             | grep "\[Pagamento\]"
docker logs pedeaqui-api             | grep "\[Startup\]"
```

| Prefixo | Investiga |
|---|---|
| `[Startup]` | config faltando ou arriscada no boot |
| `[Delivery estimate debug]` | taxa/prazo errados, passo a passo |
| `[Delivery estimate]` | estimativa reaproveitada ou não, e por quê |
| `[Delivery Google]` | latência e status HTTP do Google |
| `[Idempotency]` | reenvio devolvendo resposta gravada |
| `[AdminAuth]` | quem logou no painel e de qual restaurante |
| `[AdminStream]` | `Last-Event-ID` inválido no stream do painel |
| `[Impressao]` | item que caiu na via "SEM SETOR" — sempre configuração errada |
| `[AI /chat]` | chat: tempo por etapa e cache |
| `[Contract validation]` | 422 em rota crítica |
| `[Pagamento][mercadopago]` | latência/status de cada chamada e, no erro, código e causa |
| `[Pagamento]` | cobrança que não saiu, webhook aplicado ou ignorado |
| `[Body limit]` | requisição rejeitada com 413 |

**Não há dado pessoal nesses logs, por escolha.** A mensagem do chat vira digest
sha256, a chave de idempotência também, e o e-mail do pagador sai mascarado como
`[email]` nas linhas do Mercado Pago. Números **não** são mascarados de propósito
— levariam junto o id do pagamento e o código do erro, que são exatamente o que
se lê. Não passe a logar conteúdo cru para depurar.

### Greps que valem ter no radar

```bash
# dinheiro de cliente parado: pedido pago cancelado sem estorno
grep "pedido pago foi .* sem estorno automatico"

# pedidos duplicados: cliente sem Idempotency-Key
grep "requisicao sem Idempotency-Key"

# conta do Google subindo
grep "estimativa nao reaproveitada motivo="

# comanda saindo errada
grep "item sem setor utilizavel"
```

---

## 4. Manutenção

```bash
docker exec pedeaqui-api python scripts/cleanup_idempotency_keys.py --dry-run
docker exec pedeaqui-api python scripts/cleanup_idempotency_keys.py
docker exec pedeaqui-api python scripts/reindex_ai.py
docker exec pedeaqui-api python scripts/audit_indexes.py
```

- **`cleanup_idempotency_keys.py`** apaga chaves de idempotência **e** estimativas
  de entrega vencidas. Seguro: passado o TTL nenhuma das duas protege ou vale mais
  nada. Bota no cron.
- **`reindex_ai.py`** regenera os embeddings dos produtos. Rode depois de mexer no
  cardápio, senão o chat continua recomendando o cardápio velho.
- **`audit_indexes.py`** procura colisão de nome e duplicata de definição entre
  índices.

---

## 5. Quando não sobe

Em ordem.

**1. Leia os logs. A causa quase sempre está escrita.**

```bash
docker logs pedeaqui-api --tail 50
```

**2. Falha do `alembic upgrade head`, antes de qualquer log da aplicação.**
O entrypoint migra antes de servir (§1). Uma revisão que falha ou um banco
inacessível derruba o container em loop. A linha `[entrypoint] alembic upgrade head`
é a última coisa que aparece antes do erro.

**3. `StartupConfigurationError`.**
É a validação de boot recusando de propósito. Três coisas derrubam:

- `GOOGLE_MAPS_ROUTES_API_KEY` vazia com `DELIVERY_ESTIMATE_PROVIDER=google_routes`
  (com Routes API **e** Geocoding API habilitadas no projeto GCP);
- `PAYMENT_PROVIDER=mercadopago` sem `PAYMENT_CREDENTIALS_ENCRYPTION_KEY`;
- `MERCADOPAGO_ENVIRONMENT` diferente de `test` ou `production`.

**4. `ValidationError` do pydantic-settings.**
Falta variável obrigatória no `.env`. As sem default são: `DATABASE_URL`,
`SUPABASE_URL`, `CUSTOMER_AUTH_SECRET`, `EMAIL_CODE_SECRET`,
`PASSWORD_RESET_SECRET`, `OPENAI_API_KEY`.

**5. Erro de conexão com o banco.**
`DATABASE_URL` precisa do driver: `postgresql+psycopg://...`. Sem o `+psycopg` o
SQLAlchemy tenta `psycopg2` e o comportamento muda. Confira também se o IP do
servidor está liberado no Supabase.

**6. Sobe, mas o Traefik dá 404 / gateway error.**
Não é o app. Confira se o container está na rede externa `n8n_default` e se o DNS
de `api.pederapidex.com` aponta para o servidor. Teste por dentro:
`docker exec pedeaqui-api curl -s localhost:8000/health`.

**7. Sobe e responde, mas `/docs` dá 404.**
Comportamento correto com `APP_ENV=production` — o schema completo entrega a
superfície de ataque inteira a quem só achou o domínio. Para ligar sem sair de
produção: `ENABLE_API_DOCS=true`.

**8. Warnings no boot que NÃO derrubam** — cada um tem consequência real:

| Warning | Consequência |
|---|---|
| `REDIS_URL` vazia | cache de estimativa e rate limit só em memória; com N workers o limite vira N × o configurado |
| `ADMIN_AUTH_SECRET` vazia | tokens de lojista assinados com o segredo do cliente |
| `SUPABASE_SERVICE_ROLE_KEY` vazia | upload de imagem do painel responde 503; leitura das imagens segue |
| `PAYMENT_WEBHOOK_SECRET` vazia com provider sandbox | webhook responde 503 e nenhum pedido online sai de "aguardando pagamento" |
| `PAYMENT_PROVIDER=sandbox` em produção | cobranças criadas localmente, nenhum dinheiro movimentado de verdade |
| `INTERNAL_API_KEY` presente | depreciada, nenhuma rota usa; pode sair do `.env` |
| `RATE_LIMIT_CLIENT_IP_HEADER` vazio | atrás do proxy, todos os clientes no mesmo balde |

---

## 6. Testes

```powershell
py -m pytest -q                        # 614 testes da API, ~11s
py -m pytest tests/test_idempotency.py -v
cd print-agent && py -m pytest -q      # 65 testes do agente
```

`pytest.ini` da raiz tem `testpaths = tests` de propósito: sem isso um `pytest`
sem argumentos varreria o repositório inteiro e coletaria os testes do
`print-agent/` junto — dois ciclos de vida diferentes, a API sobe em container a
cada merge e o agente é instalado à mão numa máquina Windows.

Os testes **não precisam de banco** (usam fakes em memória), mas precisam de um
`.env` válido, porque `src.core.config` é importado na cadeia e `pydantic-settings`
levanta `ValidationError` se faltar variável obrigatória.

O que os testes **não** cobrem, e você verifica à mão: o `ON CONFLICT DO NOTHING`
real, o bloqueio entre transações concorrentes, o round-trip do JSONB, a limpeza
por TTL, e qualquer chamada de verdade ao Mercado Pago ou ao Google.
