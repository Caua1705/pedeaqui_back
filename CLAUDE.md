# PedeAqui — backend

Convenções e armadilhas de domínio (pedido, pagamento, comissão, horário de
funcionamento, Alembic, impressão de comanda) estão na skill `rapidex-backend`.
Este arquivo cobre o que não cabe lá.

## Testes

A suíte é dividida por marcador:

    pytest -m "not db"    # rápida, sem banco, não precisa de Docker
    pytest -m db          # contra um Postgres de verdade

O banco da suíte `db` sobe com:

    docker compose -f docker-compose.test.yml up -d

`TEST_DATABASE_URL` sobrepõe a URL padrão, para quem já tem um Postgres 17 na
máquina. O schema não nasce de `Base.metadata.create_all()` — o motivo está no
docstring de `tests/conftest.py`.

### Variáveis de ambiente obrigatórias

`src/core/startup_checks.py` roda no lifespan do app e recusa subir se faltar
configuração essencial — comportamento correto, e que vale também sob teste:
qualquer teste que monte o `TestClient` levanta `StartupConfigurationError` no
**setup**, não no assert. O sintoma é dezenas de erros idênticos que não têm
nada a ver com o que o teste checa.

Para que isso não aconteça, `tests/conftest.py` injeta valores falsos para essas
variáveis no nível do módulo, antes de qualquer `import src.*`:

```python
for _variavel, _valor_falso in {
    "GOOGLE_MAPS_ROUTES_API_KEY": "chave-de-teste-nao-usada",
}.items():
    os.environ.setdefault(_variavel, _valor_falso)
```

**Teste novo que dependa de uma env var obrigatória deve seguir esse padrão**, e
os três detalhes não são decorativos:

- **No nível do módulo, não numa fixture.** `settings` é construído no primeiro
  `import src.*` e lê o ambiente uma única vez. Fixture roda tarde demais.
- **`setdefault`, não atribuição.** Quem tiver o valor real no ambiente continua
  rodando com ele; o dublê só preenche o buraco.
- **Valor falso, nunca credencial real.** Nenhum teste da suíte chama serviço
  externo — as integrações são dubladas. Se um teste seu precisa de credencial
  de verdade para passar, o problema é o dublê faltando, não a variável.

Se preferir escopo menor que a sessão inteira, use `monkeypatch.setenv` dentro
do teste — mas só funciona para variável lida em tempo de execução, não para as
que `settings` congela no import.

### Dublê de schema ou de model: construa o tipo real

**Nunca dublar um schema Pydantic ou um model do ORM com `SimpleNamespace` (ou
qualquer objeto de atributos livres).** Construa o tipo de verdade:
`ProductResponse(...)` quando a função sob teste recebe o schema, uma instância
**transiente** do model (`Product(...)`, sem sessão e sem banco) quando ela
recebe o model.

O motivo não é purismo. Um objeto de atributos livres **não tem o contrato que
o teste diz estar verificando**: ele responde qualquer atributo que o teste
escrever e nenhum que o teste esquecer. O resultado é um teste que fica verde
descrevendo um objeto que a aplicação nunca produz.

**O caso que fecha esta porta.** A revisão `20260825_0039` acrescentou
`serves_people` a `products`. O campo chegou ao `AdminProductResponse` (o do
painel) e a `_serve_quantas_pessoas` (a voz), e **não** ao `ProductResponse` —
que é o que `MenuService.product_response` devolve e, portanto, o que a
hidratação entrega à ferramenta de voz.

Os testes rápidos da voz montavam o produto assim:

```python
SimpleNamespace(name=nome, price=..., description=None, serves_people=None)
```

O atributo existia **porque o teste o escreveu**. Os testes passaram, e
`buscar_no_cardapio` levantava `AttributeError` em **toda** busca falada em
produção. Quem denunciou foi um teste de fumaça com banco, um dia depois — e o
lojista, nesse meio-tempo, preenchia o campo no painel para um atendente que
não conseguia lê-lo.

O que o tipo real dá de graça, e o dublê solto não dá:

- **coluna nova não passada vale `None`**, em vez de estourar — o teste continua
  falando do que ele veio falar;
- **atributo que não existe levanta na hora**: `Product(nome_errado=...)` é
  `TypeError`, e `ProductResponse(...)` sem um campo obrigatório é
  `ValidationError`. Erro de nome deixa de sobreviver a uma suíte verde;
- **os tipos são os de verdade.** Foi assim que apareceu que `price` chega ao
  resumo da voz como `float` e nunca como `None` (a coluna é `NOT NULL`) — o
  dublê com `Decimal` e `None` descrevia um produto que não existe.

Instância transiente **não precisa de banco**, então isto vale igual na suíte
rápida: `Product(...)` sem `db.add` é um objeto Python comum. O que ela **não**
traz é `default=` de coluna, que só é aplicado no INSERT — por isso os fixtures
continuam escrevendo os campos explicitamente.

`SimpleNamespace` continua certo para o que ele é bom: dublar **colaborador**,
não dado. Um cliente HTTP (`SimpleNamespace(post=...)`), um serviço de
embedding, um objeto de resposta de biblioteca externa — nesses não há contrato
nosso a respeitar. A regra é sobre schema e model deste repositório.

Correlato, do lado das dependências: a armadilha 42 da skill diz a mesma coisa
sobre biblioteca de terceiro — dublar o transporte e deixar a biblioteca real
por cima. **Um dublê alto demais testa o dublê.**
