# Rodada 6 do backend — fonte da verdade

Branch: `rodada/backend-6`, saindo de `rodada/backend-5`. Nunca commitar na
`main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Rodadas anteriores: `rodada-back.md` a `rodada-back-5.md`.

**Nada de produção.** Merge na `main` continua fora. O `6fcaccc` continua
esperando. O histórico de chat está **desbloqueado mas NÃO implementado** — fica
depois do escopo de tenant.

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 1 | Redis: o que é a única chave, e por que os contadores deram zero | **feito** |
| 2 | A varredura do escopo de tenant | pendente |

---

## 1. O `dbsize=1`: dois erros meus no comando, e uma resposta

### A persistência: você está certo, e vale registrar por quê

`aof_enabled=0`, e `rdb_last_save_time` de 14/ago com **2.568 mudanças
acumuladas** desde então. Um RDB configurado salvaria muito antes disso — as
regras `save` padrão disparam em centenas de mudanças. Três semanas com 2.568
pendentes é a assinatura de **`save ""`**, ou seja, snapshot desligado.

**Sem persistência. O TTL é a garantia e ela vale.** O histórico de chat está
desbloqueado.

E o `rdb_last_save_time` de 14/ago não é resíduo: é o instante em que o Redis
subiu (ele carimba isso no boot mesmo sem salvar nada). Serve como **uptime do
container** — ele não reinicia desde então.

### O segundo comando: dois dos quatro padrões que eu te dei estavam errados

Fui conferir no código, e o erro é meu:

| O que eu te mandei | O prefixo de verdade | |
|---|---|---|
| `delivery-estimate:*` | `delivery-estimate:` | **certo** |
| `emb:*` | `emb:` | **certo** |
| `LIMITER*` | **`LIMITS:`** | **errado** |
| `ai:menu-generation:*` | **`ai:menu_generation:`** (underscore) | **errado** |

O do rate limit é o prefixo da biblioteca `limits` 5.8.0
(`venv/.../limits/storage/redis.py:64`, `PREFIX = "LIMITS"`); eu escrevi
`LIMITER` de cabeça. O do `MenuGeneration` está em
`chat_cache.py:138` e usa **underscore**; eu escrevi hífen.

Então "tudo zerado" não é o que os quatro números dizem: **dois deles não
podiam ser diferentes de zero, por construção.**

### O que a única chave quase certamente é

**`ai:menu_generation:{restaurant_id}`**, e o motivo é que ela é a **única chave
do sistema inteiro sem expiração**:

| Chave | TTL |
|---|---|
| `emb:` | 3600 s |
| `delivery-estimate:` | 600 s |
| `LIMITS:` | a janela do limite (minutos) |
| **`ai:menu_generation:`** | **nenhum** — `bump()` é um `incr` cru (`chat_cache.py:131`) |

Um restaurante em produção = **uma** chave permanente. `dbsize=1` bate exato.

**E a ausência de TTL ali é correta, não é defeito.** Se a chave expirasse,
`current()` voltaria a zero, a geração do cardápio reiniciaria e o cache de
busca passaria a servir entrada de uma geração anterior — exatamente o que o
contador existe para evitar. Some com ela e o Rapi volta a oferecer produto
que a loja mudou.

Vale saber o correlato: o `docker-compose.yml` roda o Redis com
`maxmemory-policy volatile-lru`, que **só descarta chave com expiração**. Essa
chave é imune ao despejo — o que é o comportamento desejado, e é bom que seja
deliberado e não sorte.

### Falta de movimento ou cache que não grava? O que decide

Os dois padrões que eu acertei (`emb:` e `delivery-estimate:`) têm TTL de **1
hora** e **10 minutos**. Zero neles significa: ninguém falou com o Rapi na
última hora e ninguém pediu estimativa nos últimos 10 minutos, **no instante em
que você rodou**. Numa churrascaria fora do horário de pico, isso é o esperado.

Mas "é esperado" não é resposta — e sua pergunta está certa: **cache que nunca
grava não dá erro, só faz o sistema trabalhar mais.**

O que decide, e **nenhum destes precisa de movimento agora** — os três leem
histórico:

```bash
# 1. QUAL e a unica chave, e se ela tem TTL.
#    `-1` = sem expiracao (o esperado para menu_generation).
docker exec pedeaqui-api python -c "
import redis
from src.core.config import settings
r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
for chave in r.scan_iter(count=100):
    print(repr(chave), 'ttl=', r.ttl(chave), 'valor=', r.get(chave) if r.type(chave)=='string' else r.type(chave))
"
```

Esta é a que responde a sua pergunta direto. **Se a chave for
`ai:menu_generation:<uuid>` com `ttl=-1` e um valor inteiro > 0, o caminho de
ESCRITA está provado**: quem a escreveu foi o `reindex`
(`scripts/reindex_worker.py:174`), pelo mesmo `cliente_redis()` que o cache de
embedding usa. Escrita funcionando + zero nas outras = **falta de movimento**,
e nada a consertar.

```bash
# 2. O cache JA acertou alguma vez? Le o historico, nao precisa de trafego.
docker logs pedeaqui-api 2>&1 | grep -o "embedding_cache_origem=[a-z_]*" | sort | uniq -c
```

| Saída | O que significa |
|---|---|
| aparece `origem=redis` | **o cache grava e acerta.** Encerrado |
| só `origem=memoria` e `origem=nenhuma` | grava, mas nunca é lido de outro worker/deploy — com **um** worker isso é normal |
| **nenhuma linha** | ninguém usou o Rapi desde o último deploy. Volte ao comando 1 |

```bash
# 3. Alguma escrita FALHOU? Estas linhas so aparecem quando algo deu errado.
docker logs pedeaqui-api 2>&1 | grep -c \
  -e "redis_initialization_failed" \
  -e "embedding_redis_write_failed" \
  -e "menu_generation_bump_failed" \
  -e "redis_write_failed"
```

**Zero é o resultado esperado.** Qualquer número diferente de zero é a resposta
"cache que não grava", e o log dirá qual dos quatro.

### O que eu concluo, com o que já se sabe

**Provavelmente falta de movimento**, e a evidência é o próprio `dbsize=1`: se o
Redis não aceitasse escrita, **nem essa chave existiria** — ela só pode ter
nascido de um `incr` bem-sucedido do `reindex`. O comando 1 confirma isso em
dez segundos.

O que **não** dá para descartar sem o comando 1 é o caso torto: a única chave
ser um `LIMITS:` (o rate limit escreve por outra biblioteca, com outro cliente),
e o `cliente_redis()` do cache estar quebrado. É improvável — o
`redis_initialization_failed` apareceria no log —, mas é exatamente o que o
comando 3 fecha.
