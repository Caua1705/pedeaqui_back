# Rodada 2 do backend — fonte da verdade

Branch: `rodada/backend-2`. Nunca commitar na `main`. Um commit por item,
verde, com push. Portão sem pipe. `down -v` ao fim de cada bloco.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

A rodada anterior está em `scratchpad/rodada-back.md` e continua válida.

---

## Regras desta rodada (do pedido)

- **Nada de produção.** Nenhum `docker exec`, nenhuma migração aplicada,
  nenhum comando contra o banco de produção. Postgres de teste e leitura de
  código, só.
- **O comando das filiais (§4.1.2 da rodada 1, commit `ec0aaae`) não roda
  nesta madrugada.** O dono roda amanhã. O commit `6fcaccc` fica esperando o
  resultado — não é para subir. Item que dependa desse resultado: anotar e
  seguir.
- Fora da rodada: produção · criar o `print_agent` · aplicar migração ·
  `float × Decimal` · entregadores · máquina de estados · merge na `main`.

---

## Estado dos itens

| # | Item | Estado |
|---|---|---|
| 0 | Scratchpad da rodada | **feito** |
| 1.1 | As 16 colunas: caminho de leitura que quebra | pendente |
| 1.2 | Teste que prova que os 2 models não são instanciados | pendente |
| 1.3 | Revisão de alinhamento escrita, sem aplicar | pendente |
| 1.4 | `divergencias_orm_schema.py` no portão como aviso | pendente |
| 2 | Dublês falsos na suíte inteira | pendente |
| 3 | Testes dependentes da hora | pendente |
| 4 | Query do `tracking_token`, pronta para colar | pendente |
| 5.1 | Plano do histórico de chat em memória | pendente |
| 5.2 | Plano do índice ANN no pgvector | pendente |
| 8 | Relatório final + armadilhas novas na skill | pendente |

---

## 0. Ponto de partida conferido

Postgres de teste de pé (`docker-compose.test.yml`), schema montado do
`schema_baseline.sql` + `alembic upgrade head`, e
`scripts/divergencias_orm_schema.py` rodado contra ele: **42 divergências**,
exatamente a decomposição da rodada 1 (16 + 20 + 6). Números confirmados, não
herdados.

As 16 da primeira classe (ORM diz NOT NULL, banco aceita NULL):

```
admin_users.is_active
ai_feedback.user_message
ai_feedback.assistant_message
ai_feedback.selected_product_ids
ai_feedback.created_at
ai_product_embeddings.product_id
ai_product_embeddings.embedding
coupon_redemptions.idempotency_key
customer_addresses.number
customer_addresses.neighborhood
customers.email
customers.password_hash
customers.birth_date
order_item_options.option_group_id
order_item_options.option_id
restaurant_coupons.valid_until
```
