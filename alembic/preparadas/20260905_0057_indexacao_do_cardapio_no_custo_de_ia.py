"""a indexacao do cardapio entra em ai_usage_events, como terceira superficie

Revision ID: 20260905_0057
Revises: 20260905_0056
Create Date: 2026-09-05

**ESCRITA E NAO APLICADA.** Este arquivo mora em `alembic/preparadas/`, que o
Alembic nao le (`script_location = alembic` faz ele enxergar so `versions/`).
Nada aqui entra em `alembic upgrade head` — nem no CI, nem na suite `db`, nem
no container. Ver `alembic/preparadas/LEIA-ME.md` e a armadilha 53.

O roteiro de aplicacao, com os passos de codigo que vao JUNTO, esta em
`docs/custo-de-ia.md`, secao 7.

---

## O que falta hoje

`ai_usage_events` (revisao `20260902_0044`) responde "quanto o restaurante X
custou este mes" para a CONVERSA — `/chat` de texto e sessao de voz. A
indexacao do cardapio nao entra: cada produto que muda paga um embedding, e o
proprio cabecalho da `0044` diz por que ficou de fora ("um milhao de buscas
custa quarenta centavos de dolar").

O argumento continua verdadeiro para a BUSCA, que e o caminho quente, e falso
para a INDEXACAO, que e o caminho que esta revisao abre. A diferenca nao e o
preco por token — e o mesmo modelo — e sim quem paga e quando:

- a busca e um embedding por pergunta de cliente, no caminho da resposta;
- a indexacao e um embedding por PRODUTO QUE MUDOU, num varredor de fundo.

E a indexacao tem um modo de falha que a busca nao tem: **mexer na formatacao
de `build_product_content` invalida todo `content_hash` gravado e reindexa o
cardapio inteiro** (esta escrito la, com todas as letras). No Junior da
Picanha sao 161 produtos de uma vez. Isso nao aparece em conta nenhuma hoje —
nao ha onde ver que uma linha de codigo comprou 161 embeddings — e e
exatamente o numero que "o custo tem que caber na comissao de 4%" precisa
enxergar.

## O que a revisao faz, e por que e so isto

Uma CHECK, um valor a mais. `surface` passa de `('text', 'voice')` para
`('text', 'voice', 'indexing')`.

Nenhuma coluna nova, e isso e resultado e nao economia: `restaurant_id`,
`branch_id`, `model`, `input_tokens`, `output_tokens` e `cost_usd` ja sao
exatamente o que uma chamada de embedding precisa gravar. A indexacao nao tem
saida (`output_tokens = 0`, o embedding e um vetor e nao tokens cobrados) e
nao tem cache (`cached_input_tokens = 0`).

**A outra CHECK nao muda, e vale conferir por que.**
`(surface = 'voice') = (voice_session_id IS NOT NULL)` continua correta com o
terceiro valor: para uma linha de indexacao os dois lados sao falsos, e falso
= falso e verdadeiro. Ela amarra a voz, nao "as superficies".

**O indice de leitura tambem nao muda.**
`(restaurant_id, created_at)` e a consulta do relatorio, e ela e a mesma com
tres superficies.

## A ordem de aplicacao NAO e livre

`AI_SURFACES` (`src/models/ai_usage_event_model.py`) e o espelho declarado
desta CHECK em `scripts/espelhos_de_enum.py`, e o portao compara valor a
valor. Isso torna a ordem obrigatoria e a torna VERIFICAVEL:

| Ordem | O que acontece |
|---|---|
| codigo antes do schema | valor so no CODIGO: o INSERT morre na CHECK, e o portao fica vermelho ate a revisao entrar |
| schema antes do codigo | valor so no BANCO: o portao fica vermelho ate o codigo entrar |
| **os dois no mesmo commit** | verde |

E o motivo de esta revisao nao ter vindo com o codigo do lado: enquanto ela
esta em `preparadas/`, o banco de teste (baseline + `upgrade head`) NAO a tem,
e um `AI_SURFACES` com tres valores deixaria o portao vermelho em todo commit
ate a noite da aplicacao. Portao vermelho por semanas e portao que se aprende
a ignorar — a mesma razao pela qual `divergencias_orm_schema.py` e aviso e nao
portao.

## Downgrade: apaga as linhas de indexacao, e isso e deliberado

`ALTER TABLE ... ADD CONSTRAINT ... CHECK` VALIDA a tabela inteira. Com uma
linha `surface = 'indexing'` gravada, recriar a CHECK antiga falha — o
downgrade morreria no meio, deixando a tabela sem CHECK nenhuma.

Por isso o `DELETE` antes. Ele apaga MEDICAO, nao dinheiro: nenhuma linha
desta tabela e pedido, comissao ou saldo de cliente, e o que se perde e a
serie historica de custo de indexacao — que e justamente a que o upgrade
acabou de comecar a juntar. Perder isso e o preco de voltar, e ele e pequeno
e conhecido.

## Custo de aplicar

Nenhum bloqueio pratico. `ADD CONSTRAINT ... CHECK` toma `ACCESS EXCLUSIVE` e
varre a tabela — mas esta tabela e nova (02/09/2026) e tem ordem de milhares
de linhas, nao de milhoes. Nao ha aqui o caso das duas etapas da armadilha 53:
a varredura termina em milissegundos.

O `DROP` da CHECK antiga e instantaneo, e as duas operacoes cabem no mesmo
`alembic upgrade` — nao ha o problema do `VALIDATE` dentro da transacao que
segura o lock, porque nao ha `NOT VALID` aqui.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_0057"
down_revision: Union[str, None] = "20260905_0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NOME_DA_CHECK = "ck_ai_usage_events_surface"


def upgrade() -> None:
    op.drop_constraint(NOME_DA_CHECK, "ai_usage_events", type_="check")
    op.create_check_constraint(
        NOME_DA_CHECK,
        "ai_usage_events",
        "surface IN ('text', 'voice', 'indexing')",
    )


def downgrade() -> None:
    # ANTES de recriar a CHECK antiga, e nao depois: `ADD CONSTRAINT ... CHECK`
    # valida a tabela inteira, e uma linha 'indexing' faria o downgrade morrer
    # no meio — com a tabela sem CHECK nenhuma, que e pior que os dois estados
    # que ele tenta alternar.
    op.execute(sa.text("DELETE FROM ai_usage_events WHERE surface = 'indexing'"))

    op.drop_constraint(NOME_DA_CHECK, "ai_usage_events", type_="check")
    op.create_check_constraint(
        NOME_DA_CHECK,
        "ai_usage_events",
        "surface IN ('text', 'voice')",
    )
