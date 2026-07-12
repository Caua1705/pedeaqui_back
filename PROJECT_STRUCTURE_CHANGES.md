# Project Structure Changes

Data: 2026-07-12

## Arquivos/pastas removidos

Foram removidos todos os diretórios `__pycache__` e arquivos `.pyc` encontrados no projeto.

Observacao: `python -m compileall src` recria `__pycache__` por natureza. A verificacao foi rodada e, depois dela, os caches gerados foram removidos novamente. A verificacao final nao encontrou `__pycache__` nem `.pyc`.

## Alteracoes no `.gitignore`

Foi adicionada a entrada explicita:

```gitignore
*.pyc
```

O `.gitignore` ja continha as demais entradas relevantes:

```gitignore
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
.env
```

## `.agents`

A pasta `.agents` existe e esta vazia.

Foi tentada a remocao por estar vazia, mas o filesystem retornou `Acesso negado ao caminho`. Por isso, a pasta foi mantida. Como esta vazia, ela nao afeta o backend.

## `requirements-dev.txt`

Foi criado `requirements-dev.txt` com:

```text
pytest
```

`requirements.txt` de producao nao foi alterado.

## `tests`

A pasta `tests` foi mantida. Ela contem testes de contrato e regressao para delivery, customer addresses, cashback, restaurant info e rotas novas. Mesmo com `pytest` ausente no ambiente atual, esses testes fazem parte da seguranca da refatoracao.

## `migrations`

A pasta `migrations` foi mantida. Migrations sao historico do banco e nao devem ser removidas em uma limpeza estrutural. Nenhuma migration foi criada ou alterada.

## `scripts/reindex_ai.py`

O script foi mantido. Ele importa e usa:

- `EmbeddingService`
- `AIRepository`

Isso confirma que ainda faz parte da operacao de Rapi/AI embeddings.

## Por que `src/ai/services` e `src/services` continuam separados

- `src/services` contem regras de negocio do backend principal: pedidos, delivery, auth, cliente, menu, restaurante, cashback e admin.
- `src/ai/services` contem servicos especificos de Rapi/AI: cache de chat, LLM, embeddings e retrieval.

Manter essa separacao reduz acoplamento e evita misturar dependencias de IA/OpenAI com fluxos core do backend.

## Verificacoes rodadas

```bash
python -m compileall src
```

Resultado: passou.

```bash
python -m pytest
```

Resultado: pytest nao rodou porque nao esta instalado:

```text
No module named pytest
```

Verificacao final de caches:

```bash
Get-ChildItem -Path . -Recurse -Directory -Force -Filter __pycache__
Get-ChildItem -Path . -Recurse -File -Force -Filter *.pyc
```

Resultado: nenhuma ocorrencia.
