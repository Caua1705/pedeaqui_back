"""A versao da imagem aparece nos tres lugares — e grita quando nao foi carimbada.

O QUE ISTO EXISTE PARA IMPEDIR. Em 24/08/2026, tres commits ficaram sem `push`
e producao seguiu rodando o codigo anterior. Uma bateria de medicao inteira foi
feita contra o build errado, e a unica forma de descobrir isso foi comparar
frases de log com `git log -S` — porque nenhuma linha dizia a versao.

A resposta e o carimbo em TRES lugares, e eles nao sao redundantes: respondem a
perguntas diferentes, feitas em momentos diferentes, por gente com acesso
diferente.

- **boot** (`main.py`): que imagem subiu neste deploy.
- **`[AI /chat] Nova requisicao`**: de que build veio ESTE turno que estou
  medindo. E o unico dos tres que sobrevive a um recorte do log por janela de
  tempo — que quase nunca alcanca o boot.
- **`GET /health`**: qual esta no ar agora, sem precisar de acesso ao log do
  container.

O QUARTO COMPORTAMENTO, E O QUE MAIS FACIL DE PERDER: sem o build arg, o boot
sai como **WARNING com o comando do conserto junto**. Um `git_sha=nao-carimbado`
discreto seria pior que campo nenhum — daria a impressao de que a versao esta
sendo registrada quando nao esta. Se alguem trocar esse `warning` por `info`
algum dia, o carimbo volta a ser decorativo em silencio, e e este arquivo que
recusa.
"""

import logging
from types import SimpleNamespace

import pytest

import main as main_module
from src.core.config import GIT_SHA_NAO_CARIMBADO, Settings, settings
from src.services import chat_service as chat_module
from src.ai.services.chat_history import historico
from src.services.chat_service import ChatService
from tests import fabricas


SHA_FALSO = "a1b2c3d"


@pytest.fixture
def client():
    """`TestClient` SEM o `with`, de proposito: assim o lifespan nao roda.

    `GET /health` nao toca banco, Redis nem OpenAI — e a rota existe
    justamente para responder sem depender de nada. Subir o lifespan aqui
    traria `validate_settings` e `warm_up` para dentro de um teste que nao
    tem nada a ver com nenhum dos dois, e devolveria os erros de setup que o
    `CLAUDE.md` descreve.
    """
    from fastapi.testclient import TestClient

    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def sessao_limpa():
    historico.esquecer_tudo()
    yield
    historico.esquecer_tudo()


class TestOBoot:
    def test_a_versao_sai_no_boot(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "GIT_SHA", SHA_FALSO)

        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            main_module._log_versao_da_imagem()

        assert f"git_sha={SHA_FALSO}" in caplog.text

    def test_imagem_sem_carimbo_sai_como_warning_com_o_comando(self, monkeypatch, caplog):
        """O aviso tem que ser acionavel sozinho: quem le nao vai abrir o Dockerfile."""
        monkeypatch.setattr(settings, "GIT_SHA", GIT_SHA_NAO_CARIMBADO)

        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            main_module._log_versao_da_imagem()

        avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(avisos) == 1
        mensagem = avisos[0].getMessage()
        assert GIT_SHA_NAO_CARIMBADO in mensagem
        assert "GIT_SHA=$(git rev-parse --short HEAD)" in mensagem
        assert "docker compose up -d --build" in mensagem

    def test_imagem_carimbada_nao_gera_aviso(self, monkeypatch, caplog):
        """A contraprova: sem ela, um `warning` incondicional passaria no teste acima."""
        monkeypatch.setattr(settings, "GIT_SHA", SHA_FALSO)

        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            main_module._log_versao_da_imagem()

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


class TestOTurnoDoChat:
    def test_a_versao_sai_em_toda_requisicao(self, monkeypatch, caplog):
        """O carimbo que sobrevive a um recorte do log por janela de tempo."""
        monkeypatch.setattr(settings, "GIT_SHA", SHA_FALSO)
        restaurante = fabricas.restaurante(name="Junior da Picanha")
        filial = fabricas.filial()

        service = ChatService(db=SimpleNamespace())
        service.restaurant_repository = SimpleNamespace(
            get_active_by_id=lambda _id: restaurante
        )
        service.branch_repository = SimpleNamespace(
            get_active_by_id_and_restaurant=lambda _b, _r: filial
        )
        # Saudacao: o turno inteiro sem busca e sem modelo, entao o teste nao
        # precisa dublar nenhum dos dois para chegar a linha de log.
        monkeypatch.setattr(chat_module, "ChatLLMService", None)

        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            service.chat(
                restaurant_id=restaurante.id,
                branch_id=filial.id,
                session_id="sessao-1",
                message="oi",
            )

        assert "Nova requisicao" in caplog.text
        assert f"git_sha={SHA_FALSO}" in caplog.text

    def test_a_mensagem_do_cliente_continua_fora_do_log(self, monkeypatch, caplog):
        """O carimbo entrou na mesma linha do digest — e o digest continua sendo digest.

        A linha ganhou um campo; ela nao pode ter ganhado o texto da pessoa.
        """
        monkeypatch.setattr(settings, "GIT_SHA", SHA_FALSO)
        restaurante = fabricas.restaurante(name="Junior da Picanha")
        filial = fabricas.filial()

        service = ChatService(db=SimpleNamespace())
        service.restaurant_repository = SimpleNamespace(
            get_active_by_id=lambda _id: restaurante
        )
        service.branch_repository = SimpleNamespace(
            get_active_by_id_and_restaurant=lambda _b, _r: filial
        )
        monkeypatch.setattr(chat_module, "ChatLLMService", None)

        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            service.chat(
                restaurant_id=restaurante.id,
                branch_id=filial.id,
                session_id="sessao-1",
                message="bom dia",
            )

        linha = next(
            linha for linha in caplog.text.splitlines() if "Nova requisicao" in linha
        )
        assert "bom dia" not in linha
        assert "message_digest=" in linha


class TestOHealth:
    def test_health_devolve_a_versao(self, client, monkeypatch):
        monkeypatch.setattr(settings, "GIT_SHA", SHA_FALSO)

        corpo = client.get("/health").json()

        assert corpo["status"] == "ok"
        assert corpo["git_sha"] == SHA_FALSO

    def test_health_admite_a_imagem_sem_carimbo(self, client, monkeypatch):
        """Nao esconde: `nao-carimbado` e uma resposta, string vazia nao seria."""
        monkeypatch.setattr(settings, "GIT_SHA", GIT_SHA_NAO_CARIMBADO)

        assert client.get("/health").json()["git_sha"] == GIT_SHA_NAO_CARIMBADO


class TestOSHaVindoDoAMBIENTE:
    """O buraco por onde o carimbo escapou — e ele estava ANTES de tudo acima.

    Em 24/08/2026 um deploy saiu com `git_sha=` VAZIO no boot e **sem** o
    WARNING. Nenhum teste deste arquivo pegou, e o motivo e estrutural: todos
    eles fazem `monkeypatch.setattr(settings, "GIT_SHA", ...)`, quer dizer,
    comecam DEPOIS da unica etapa que falhou — a leitura do ambiente.

    O caminho real: `docker-compose.yml` manda `GIT_SHA: "${GIT_SHA:-}"`, que
    com a variavel ausente vira `--build-arg GIT_SHA=` (string vazia, nao
    ausencia). Valor explicito sobrepoe o `ARG GIT_SHA=nao-carimbado` do
    `Dockerfile`, a imagem nasce com `ENV GIT_SHA=""`, e o default do campo
    nunca vale porque para o pydantic o campo estava PRESENTE.

    A licao que fica, e que vale para qualquer campo de `Settings` com
    sentinela de default: **dublar o valor final testa a metade que nao
    quebrou.** O que precisa ser testado e o ambiente entrando.
    """

    def test_variavel_vazia_no_ambiente_vira_nao_carimbado(self, monkeypatch):
        monkeypatch.setenv("GIT_SHA", "")

        assert Settings().GIT_SHA == GIT_SHA_NAO_CARIMBADO

    def test_so_espaco_tambem_conta_como_ausente(self, monkeypatch):
        """Um espaco sobrando na linha do `.env` nao pode virar "carimbado"."""
        monkeypatch.setenv("GIT_SHA", "   ")

        assert Settings().GIT_SHA == GIT_SHA_NAO_CARIMBADO

    def test_sha_de_verdade_atravessa_intacto(self, monkeypatch):
        """A contraprova: sem ela, um validador que devolvesse sempre a
        sentinela passaria nos dois testes acima e apagaria o carimbo real."""
        monkeypatch.setenv("GIT_SHA", SHA_FALSO)

        assert Settings().GIT_SHA == SHA_FALSO

    def test_o_ambiente_vazio_faz_o_boot_gritar(self, monkeypatch, caplog):
        """As duas metades ligadas: e o WARNING que faltou no deploy real.

        Os testes de `TestOBoot` provam que a sentinela gera o aviso; estes
        provam que o ambiente vazio gera a sentinela. Este aqui e o unico que
        cobre o percurso inteiro, que e onde o defeito morava.
        """
        monkeypatch.setenv("GIT_SHA", "")
        monkeypatch.setattr(main_module, "settings", Settings())

        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            main_module._log_versao_da_imagem()

        avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(avisos) == 1
        assert f"git_sha={GIT_SHA_NAO_CARIMBADO}" in avisos[0].getMessage()
