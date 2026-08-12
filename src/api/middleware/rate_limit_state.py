"""Garante que `request.state.view_rate_limit` sempre exista.

POR QUE ISTO EXISTE

O wrapper que o `@limiter.limit` monta em volta da rota faz, depois de a
funcao responder:

    self._inject_headers(response, request.state.view_rate_limit)

Sem `getattr`, sem default. Quem grava esse atributo e
`Limiter.__evaluate_limits`, na ULTIMA linha — depois de consultar o storage.
Se a consulta levantar (Redis recusando AUTH foi o caso real), o atributo
nunca chega a ser gravado, e a leitura acima vira `AttributeError`. O
resultado e 500 em toda rota com rate limit, justamente quando o storage
esta com problema: a falha de um componente auxiliar derrubando o principal.

`in_memory_fallback_enabled` (ver `src/api/rate_limit.py`) resolve o caso
conhecido, porque a reavaliacao em memoria grava o atributo. Este middleware
cobre o resto: qualquer caminho futuro em que a avaliacao morra antes do fim
— storage novo, bug de versao, `key_func` levantando — encontra o atributo
ja com `None`, e `_inject_headers` trata `None` sem injetar cabecalho nenhum.

Middleware ASGI puro, como o `BodySizeLimitMiddleware`: precisa escrever no
`scope` antes de qualquer `Request` ser construido, e `scope["state"]` e
exatamente o dicionario que `Request.state` enxerga.
"""

from starlette.types import ASGIApp, Receive, Scope, Send


class RateLimitStateMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            # `setdefault` no scope inteiro porque o Starlette so cria a chave
            # quando alguem le `request.state` pela primeira vez.
            scope.setdefault("state", {})
            scope["state"].setdefault("view_rate_limit", None)
        await self.app(scope, receive, send)
