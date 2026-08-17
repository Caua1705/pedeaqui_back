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
