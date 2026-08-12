"""Conversa com a API.

Tres chamadas para o caminho principal, nesta ordem, e o motivo de serem tres:

1. `POST /admin/auth/login` — devolve o token de acesso (vale 12h).
2. `POST /admin/orders/stream-ticket` — devolve um ticket de 30 SEGUNDOS.
3. `GET  /admin/orders/stream?ticket=...` — o stream em si.

Mais `GET /admin/orders/{id}/print-jobs`, que busca as vias de um pedido, e
duas rotas que so contam o que esta acontecendo nesta maquina — o heartbeat
e a lista de impressoras. Essas duas nao imprimem nada: elas existem para o
painel poder responder "o agente do Centro esta no ar?" sem alguem ligar
para a loja.

O passo 2 parece burocracia e nao e. A rota do stream so aceita ticket na
querystring, porque foi desenhada para o `EventSource` do navegador, que nao
manda cabecalho. Mandar o token de 12h na URL o deixaria no log de acesso do
proxy e no Referer; o ticket vale meio minuto e so serve para abrir stream.
O agente segue a mesma porta que o painel — nao ha uma entrada separada para
ele, e nao deveria haver.

Como a conexao e reaberta a cada 15 minutos (o servidor fecha de proposito),
o passo 2 se repete a cada reconexao. O passo 1 so quando o token expira.
"""

import logging
import re
from collections.abc import Iterator

import requests


logger = logging.getLogger(__name__)

# Tempo para estabelecer conexao e para receber CADA pedaco do stream. O de
# leitura precisa ser maior que o heartbeat da API (20s) e menor que uma
# eternidade: e ele que detecta o cabo arrancado, situacao em que o socket
# fica aberto sem nunca mais entregar byte.
CONNECT_TIMEOUT_SECONDS = 10.0
STREAM_READ_TIMEOUT_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 30.0


class ApiError(Exception):
    """Falha de rede ou resposta inesperada da API."""


class AuthError(ApiError):
    """Credencial recusada, mas ainda ha o que tentar.

    O caso normal: o token de 12h venceu durante a noite e existe
    email/password no config.ini para refazer o login. O agente reconecta e
    segue.
    """


class AuthFatalError(AuthError):
    """Credencial recusada e NAO ha como recuperar.

    Dois casos, e os dois so se resolvem com alguem editando o config.ini:

    - email/senha errados (o login respondeu 401);
    - token estatico vencido ou invalido, sem email/password ao lado para
      refazer o login.

    Separada de `AuthError` porque o tratamento e oposto: aqui insistir e
    inutil. O agente encerra com mensagem em vez de tentar para sempre — um
    processo vivo que nunca imprime e pior que um processo morto, porque
    ninguem percebe.

    Subclasse de `AuthError` de proposito: quem so quer saber "foi problema
    de credencial?" continua pegando as duas com um `except AuthError`.
    """


class ApiClient:
    def __init__(self, base_url: str, token=None, email=None, password=None, session=None):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        # Token do config.ini, quando houver. Vira `None` assim que a API o
        # recusar, para o proximo `_authorize` refazer o login.
        self._token = token
        self.session = session or requests.Session()

    def print_jobs(self, order_id: str) -> list[dict]:
        """As vias de um pedido, ja formatadas pela API."""
        payload = self._request("GET", f"/admin/orders/{order_id}/print-jobs")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise ApiError(f"resposta sem lista de tarefas para o pedido {order_id}")
        return jobs

    def stream_ticket(self) -> str:
        payload = self._request("POST", "/admin/orders/stream-ticket")
        ticket = payload.get("ticket")
        if not ticket:
            raise ApiError("resposta de stream-ticket sem ticket")
        return ticket

    def heartbeat(self, agent_version: str) -> dict:
        """Diz a API que este agente esta vivo, e em qual versao.

        E o unico jeito de o painel responder "o agente do Centro esta no
        ar?" sem alguem ligar para a loja. Antes disso, a resposta so
        existia no log da maquina do balcao.
        """
        return self._request(
            "POST", "/admin/print-agent/heartbeat", body={"agent_version": agent_version}
        )

    def report_printers(self, printers: list[tuple[str, bool]]) -> dict:
        """Manda a lista de impressoras desta maquina para o painel.

        Substitui a anterior inteira do lado da API: impressora desinstalada
        precisa sumir do seletor, senao o lojista escolhe uma que nao existe
        mais e a via nao sai.
        """
        return self._request(
            "POST",
            "/admin/print-agent/printers",
            body={
                "printers": [
                    {"name": name, "is_default": is_default} for name, is_default in printers
                ]
            },
        )

    def open_stream(self, last_event_id: str | None = None) -> Iterator[str]:
        """Abre o SSE e devolve as linhas, uma a uma.

        `last_event_id` e o cursor da reconexao: com ele o servidor repete o
        que aconteceu enquanto o agente esteve fora. Sem ele, tudo o que
        aconteceu na queda estaria perdido — e uma queda de 40 segundos no
        almoco e um pedido que nao imprimiu.
        """
        ticket = self.stream_ticket()
        headers = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id

        try:
            response = self.session.get(
                f"{self.base_url}/admin/orders/stream",
                params={"ticket": ticket},
                headers=headers,
                stream=True,
                timeout=(CONNECT_TIMEOUT_SECONDS, STREAM_READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            # `exc` costuma trazer a URL inteira, e a URL leva o ticket. Ele
            # vale 30 segundos, mas e credencial do mesmo jeito e o log vai
            # parar no WhatsApp de alguem.
            raise ApiError(
                f"nao foi possivel abrir o stream: {_hide_ticket(exc)}"
            ) from exc

        if response.status_code in (401, 403):
            response.close()
            self._token = None
            raise AuthError(f"stream recusado ({response.status_code})")
        if response.status_code >= 400:
            response.close()
            raise ApiError(f"stream respondeu {response.status_code}")

        # Cinto de seguranca, nao correcao de defeito: a API HOJE responde
        # `text/event-stream; charset=utf-8` (o Starlette poe o charset
        # sozinho em todo media_type `text/*`), entao o `requests` ja
        # decodifica certo.
        #
        # O que esta linha cobre e o cabecalho chegar SEM o charset — proxy
        # que reescreve content-type, ou a API trocando de framework. Ai o
        # `requests` aplica a regra default do HTTP para `text/*`, que e
        # ISO-8859-1, e todo acento do stream vira mojibake ("Praça" ->
        # "PraÃ§a") sem erro em lugar nenhum. Forcar e barato e o SSE e UTF-8
        # por especificacao: nao existe caso em que este `utf-8` esteja
        # errado.
        response.encoding = "utf-8"
        return _iter_lines(response)

    def _request(
        self,
        method: str,
        path: str,
        retry_on_auth: bool = True,
        body: dict | None = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self._authorize()}"}
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ApiError(f"{method} {path} falhou: {exc}") from exc

        if response.status_code in (401, 403) and retry_on_auth:
            # Token de 12h vencido durante a noite. Descarta e tenta uma vez
            # com credencial nova; e o caminho normal de um agente que roda
            # como servico por semanas.
            logger.info("credencial recusada em %s, refazendo login", path)
            self._token = None
            return self._request(method, path, retry_on_auth=False, body=body)
        if response.status_code in (401, 403):
            raise AuthError(f"{method} {path} recusado ({response.status_code})")
        if response.status_code >= 400:
            raise ApiError(f"{method} {path} respondeu {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"{method} {path} respondeu algo que nao e JSON") from exc

    def _authorize(self) -> str:
        if self._token:
            return self._token
        if not (self.email and self.password):
            raise AuthFatalError(
                "o token do config.ini foi recusado e nao ha email/password "
                "ao lado para refazer o login. O token de acesso vale 12 "
                "horas: para o agente rodar sozinho, preencha email e "
                "password no config.ini."
            )
        self._token = self._login()
        return self._token

    def _login(self) -> str:
        try:
            response = self.session.post(
                f"{self.base_url}/admin/auth/login",
                json={"email": self.email, "password": self.password},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ApiError(f"login falhou: {exc}") from exc

        if response.status_code in (401, 403):
            # Fatal: tentar de novo com a MESMA senha errada da no mesmo, e
            # so alguem editando o config.ini resolve.
            raise AuthFatalError(
                "email ou senha invalidos no config.ini (a API recusou o "
                f"login de {self.email})"
            )
        if response.status_code >= 400:
            raise ApiError(f"login respondeu {response.status_code}")

        try:
            token = response.json().get("access_token")
        except ValueError as exc:
            raise ApiError("login respondeu algo que nao e JSON") from exc
        if not token:
            raise ApiError("login respondeu sem access_token")

        logger.info("login refeito como %s", self.email)
        return token


def _hide_ticket(value) -> str:
    """Troca o valor do `ticket=` por reticencias.

    O ticket entra na URL do stream porque a rota so aceita credencial ali
    (o EventSource do navegador nao manda cabecalho). Ele vale 30 segundos,
    mas o log e um arquivo que o lojista manda por WhatsApp quando alguma
    coisa nao imprime — e credencial nenhuma tem que estar la dentro.
    """
    return _TICKET_RE.sub(r"\1...", str(value))


_TICKET_RE = re.compile(r"(ticket=)[^&\s'\"]+")


def _iter_lines(response) -> Iterator[str]:
    """Linhas do stream, com a conexao fechada no fim aconteca o que acontecer.

    O `finally` importa: sem ele, uma excecao no meio do laco deixaria o
    socket aberto e, depois de algumas reconexoes, o agente estaria segurando
    conexoes mortas contra a API.
    """
    try:
        for line in response.iter_lines(decode_unicode=True):
            # `iter_lines` devolve None para a linha em branco quando o
            # decode esta ligado; o parser espera string.
            yield line if line is not None else ""
    except requests.RequestException as exc:
        raise ApiError(f"stream interrompido: {_hide_ticket(exc)}") from exc
    finally:
        response.close()
