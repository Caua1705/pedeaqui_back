# Pendências do app e do painel — fonte da verdade

Branch: `rodada/pendencias-front`, saindo de `rodada/entregadores`. Nunca
commitar na `main`. Um commit por item, verde, com push. Portão sem pipe.

**Ao retomar depois de compactação: leia este arquivo inteiro antes de
qualquer coisa, e continue do que ele diz — nunca da lembrança.**

Nada de frente nova: são as pendências que os outros dois repositórios
listaram como bloqueadas pelo contrato.

---

## Tamanho, dito antes de fazer

| # | Item | Tamanho | Estado |
|---|---|---|---|
| a | `expires_at` do Pix em `StartPaymentResponse` | pequeno | **feito** |
| b1 | `visibility` no card do cliente | trivial | pendente |
| b2 | `auto_apply` calculado pela mesma escolha do checkout | pequeno | pendente |
| b3 | restrição por forma de pagamento (campo + estado) | médio | pendente |
| b4 | restrição por horário do dia (campo + estado) | médio | pendente |
| b5 | restrição por itens do cardápio | **grande** | **não feito — decisão do dono** |

b5 é o mais caro dos três com folga (`docs/cupons.md` §7.3): tabela nova,
reescrita de `calculate_discount`, a base da comissão muda, o cardápio é por
filial e o cupom é do restaurante (vínculo por `catalog_key`), e um estado
novo "falta o item". Não entra sem decisão.

---

## a) `expires_at` do Pix

**O que existe.** `POST /v1/payments` do Mercado Pago aceita
`date_of_expiration` e devolve o mesmo campo na resposta. Hoje não mandamos
nada (vale o padrão deles) e não lemos nada de volta; `StartPaymentResponse`
não tem prazo, e o app conta pelo relógio do cliente.

**Desenho.**

- `PIX_EXPIRATION_MINUTES` na configuração, padrão 30. O service calcula o
  instante (`utcnow() + minutos`) só para pix e passa ao gateway;
- o corpo ganha `date_of_expiration` no formato deles
  (`2026-09-03T19:30:00.000-03:00`, no fuso da operação);
- **o prazo NÃO entra na chave de idempotência.** Ele muda a cada segundo;
  se entrasse no hash, o segundo clique em "pagar" abriria um segundo pix em
  vez de devolver o mesmo (armadilha 6, a propriedade que tem que continuar
  valendo). `_mercadopago_idempotency_key` descarta o campo antes do hash;
- `_mercadopago_intent` lê `date_of_expiration` da resposta e o publica como
  `expires_at` (aware). Como o segundo clique devolve a MESMA cobrança, o
  `expires_at` que volta é o original — o contador do app não reinicia;
- sandbox devolve o instante que recebeu;
- `StartPaymentResponse.expires_at: datetime | None` — nulo no cartão.

**O que NÃO muda:** nenhuma coluna nova. O pedido não guarda o prazo; ele
existe na cobrança do gateway, e é de lá que volta.

**Feito.** Vermelho visto (`create_payment() got an unexpected keyword
argument 'pix_expires_at'`), 11 verdes em `tests/test_pix_expira_em.py`;
os 200 de gateway e pagamento continuam verdes. Lição da rodada: um guarda
`if nome not in arquivo` num patch falha quando o nome já foi inserido em
outro ponto pelo mesmo patch — a constante ficou de fora e o `NameError`
apareceu no teste, não no ruff.

### Pronto para colar no app

> `POST /restaurants/{slug}/orders/{tracking_token}/payment` passou a devolver
> `expires_at` (ISO 8601 com fuso, ou `null` no cartão). **Contem o tempo por
> ele**, não pelo relógio local: é o `date_of_expiration` da cobrança no
> Mercado Pago. Um segundo clique em "pagar" devolve a mesma cobrança com o
> mesmo `expires_at` — o contador não reinicia. Quando o prazo vence, o
> webhook marca o pagamento como `failed` e um novo POST gera outro pix, com
> outro prazo. O padrão é 30 minutos (`PIX_EXPIRATION_MINUTES`).
