# CI e proteção da `main`

## O problema, e o que é código e o que é clique

O workflow (`.github/workflows/ci.yml`) roda a suíte inteira em todo push e em
todo pull request desde que existe. Isso nunca foi o problema.

**O problema é que ele não impede nada.** Um push direto na `main` com a suíte
vermelha entra do mesmo jeito; o Actions marca a bolinha vermelha no commit
_depois_, e quem já seguiu para o deploy não volta para olhar. Foi assim que a
`main` já subiu quebrada sem ninguém perceber.

A parte que barra o merge **não mora no repositório**. Nenhum arquivo do
GitHub Actions pode se declarar obrigatório: quem decide isso é a configuração
do repositório, no GitHub, e ela tem que ser clicada uma vez. Esta página é a
lista do que clicar — e existe porque, sem ela escrita, "o CI está ligado" e "o
CI barra o merge" parecem a mesma frase.

## Os quatro checks

O nome que o GitHub mostra na lista de status checks é o `name:` do job, não o
nome do arquivo:

| Check | O que roda | Precisa de banco |
|---|---|---|
| **Lint** | `ruff check .` — API e agente de impressão | não |
| **Testes da API** | `pytest -m "not db"` **e** `python scripts/export_openapi.py --check` | não |
| **Testes contra o Postgres** | `pytest -m db` contra `pgvector/pgvector:pg17` | sim (service container) |
| **Testes do agente de impressão** | `pytest` dentro de `print-agent/` | não |

**`export_openapi --check` é um passo de "Testes da API", não um check
próprio.** Ele barra o merge do mesmo jeito — o job inteiro fica vermelho —, e
fica junto porque compartilha o `pip install` e o `.env` do job. Se um dia ele
precisar aparecer separado na lista do GitHub, o custo é uma segunda instalação
de dependências para rodar um comando de dois segundos.

## Os cliques, uma vez

`Settings` → `Rules` → `Rulesets` → `New branch ruleset`:

1. **Target**: `main` (Include default branch).
2. **Restrict deletions** e **Block force pushes** — ligados.
3. **Require a pull request before merging.**

   Este é o que fecha o buraco de verdade. Sem ele, os status checks
   obrigatórios do item 4 valem só para quem passa por PR, e o `git push`
   direto na `main` continua entrando com a suíte vermelha — que é exatamente
   o caso que já aconteceu.

   `Required approvals: 0` é uma escolha consciente enquanto o time é uma
   pessoa: exigir aprovação de outra pessoa que não existe transforma a regra
   num bloqueio permanente, e o primeiro conserto urgente vira o dia em que
   alguém desliga tudo. O que se quer aqui não é revisão humana — é que
   nenhum commit chegue à `main` sem os quatro checks verdes.

4. **Require status checks to pass**, e marcar os quatro nomes da tabela acima.

   Marque também **Require branches to be up to date before merging**. Sem
   isso, um PR verde criado antes de outro merge pode entrar quebrado: os dois
   passam sozinhos e falham juntos. É o modo de falha em que o CI verde mente.

5. **Do not allow bypassing the above settings** — ou, se preferir manter a
   saída de emergência, deixe o bypass restrito ao dono e saiba que ele
   existe. Bypass que ninguém lembra que existe é o mesmo que não ter regra.

## Por que os checks aparecem só depois do primeiro PR

O GitHub só oferece um check na lista de obrigatórios depois de tê-lo visto
executar ao menos uma vez naquele repositório. **Lint** é um job novo: abra um
PR qualquer, deixe o CI rodar, e só então volte ao ruleset para marcá-lo. Um
nome digitado à mão antes disso vira um check obrigatório que nunca reporta —
e o PR fica pendente para sempre esperando um job que não existe com aquele
nome.

O mesmo vale ao **renomear um job**: o `name:` do YAML é o contrato com o
ruleset. Trocar "Testes da API" por outra coisa não quebra o CI — quebra o
bloqueio, em silêncio, porque o check obrigatório antigo deixa de reportar e
o PR trava, ou (pior, dependendo da configuração) o novo entra sem ser
exigido.

## O que isto ainda NÃO cobre

O deploy. `docker-entrypoint.sh` roda `alembic upgrade head` e sobe o Uvicorn
quando a imagem sobe, e nada liga "a `main` está verde" a "a imagem que está
rodando é a da `main` verde". A proteção acima garante que o que entra na
`main` passou pela suíte; garantir que só o que está na `main` chega ao
servidor é outra frente.
