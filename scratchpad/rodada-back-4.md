# Rodada 4 do backend — fonte da verdade

Branch: `rodada/backend-4`, saindo de `rodada/backend-3`. Nunca commitar na
`main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Rodadas anteriores: `rodada-back.md`, `rodada-back-2.md`, `rodada-back-3.md`.

**Nada de produção.** O comando das filiais não roda — quem roda é o dono. O
`6fcaccc` continua esperando.

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 1 | O 404 do cupom vencido: voltar atrás preservando a defesa | **feito** |
| 2 | Redis: varrer a classe, consertar a chave, e o plano da exclusão | pendente |
| 3 | A quebra de contrato para o painel e o app | pendente |

---

## 1. "Não existe" × "existe e venceu" voltaram a ser respostas diferentes

Você tem razão, e a razão é específica: **"Cupom não encontrado" para um código
que existe manda o cliente conferir se digitou errado e tentar de novo.** É uma
frase que ele não tem como resolver. "Cupom vencido" encerra o assunto.

### O que mudou

`_find_coupon` passou a devolver **dois valores**: o cupom e se ele está dentro
da janela.

```python
coupon, dentro_da_janela = self._find_coupon(..., agora=agora)
```

**Duas consultas, e a segunda só no caminho raro.** A primeira usa
`filtro_de_janela` — é o cupom aplicável, e é o caminho comum. Só quando ela
volta vazia é que a segunda pergunta, **sem filtro**, se aquele código existe.
É a diferença entre as duas respostas.

A segunda **nunca pede `FOR UPDATE`**: travar linha que já se sabe que não vai
ser aplicada seguraria o cupom de outro pedido por nada. Há teste para isso.

### As mensagens, agora

| Situação | Resposta |
|---|---|
| código não existe naquele restaurante | **404** "Cupom não encontrado para este restaurante" |
| existe e venceu | **400** `expired` |
| existe e ainda não começou | **400** `not_started` |
| preview de qualquer um dos dois | **200**, `valid=false`, `ineligibility_reason` preenchido |

`not_started` e `expired` dizem coisas diferentes ao cliente — "volte depois" e
"acabou" —, e o 404 apagava as duas de uma vez.

### A defesa em profundidade ficou, e agora ela é CONFERÍVEL

Este é o ganho que a volta atrás não custou.

O `dentro_da_janela` sai do **SQL** (`filtro_de_janela`); o
`expired`/`not_started` sai do **Python** (`evaluate`). São as duas formas da
mesma regra, e `lock_and_validate_for_order` **cobra que elas concordem**:

```python
if not dentro_da_janela_pelo_sql:   # e evaluate disse que vale
    logger.error("[Cupom] as duas formas da janela divergiram | ...")
    raise HTTPException(409, "Cupom indisponível neste momento")
```

**409 e não 400**: não há nada que o cliente possa mudar no corpo dele para
consertar isso. É defeito nosso, e o log tem que dizer alto.

Antes, a "defesa em profundidade" era um segundo filtro que ninguém contestava.
Agora são **duas formas que se conferem** — e o defeito que
`src/services/coupon_window.py` inteiro existe para impedir é justamente elas
divergirem. Há teste que força o desacordo e prova o 409.

### Um dublê que mentia, achado no caminho

`FakeCouponRepository.get_by_code_and_restaurant`, em
`tests/test_janela_do_cupom.py`, **ignorava o `code`** e devolvia o cupom para
qualquer string. Com isso, "código que não existe" e "código que venceu"
chegavam ao mesmo lugar — apagando exatamente a distinção que os testes novos
medem. O SQL real compara `lower(code)`; o dublê passou a comparar também.

É a armadilha 52 outra vez, e num arquivo que eu escrevi ontem: o dublê
respondia o que o teste precisava, não o que a aplicação faz.

### Arquivos

`src/services/coupon_service.py` (`_find_coupon` devolve o par, `_buscar_cupom`
novo, a guarda em `lock_and_validate_for_order`, `logger` do módulo) ·
`tests/test_janela_do_cupom.py` · `tests/test_coupons.py`.

**Portão: 2842 testes verdes** (2226 rápidos + 616 `db`), ruff limpo, openapi
em dia. O `openapi.json` **não mudou** — `ineligibility_reason` já era
`str | None` e nenhum status novo entrou em rota (o 409 é uma exceção de
service, como os outros).
