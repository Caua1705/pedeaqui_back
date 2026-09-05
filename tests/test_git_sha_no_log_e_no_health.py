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
from src.core import config as config_module
from src.core.config import GIT_SHA_NAO_CARIMBADO, Settings, settings
from src.core.git_sha import sha_do_repositorio
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
        # Desde 05/09/2026 chegar aqui quer dizer que as DUAS fontes falharam
        # — nem o build arg, nem o `.git` que a imagem carrega. O conserto
        # acionavel deixou de ser o prefixo (que virou desnecessario) e passou
        # a ser o arg explicito, para quem constroi fora de um repositorio.
        assert "--build-arg GIT_SHA=" in mensagem
        assert ".git" in mensagem

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

    @pytest.fixture
    def sem_git_no_contexto(self, monkeypatch):
        """A imagem construida SEM o sliver de `.git` — nada a descobrir.

        Precisa ser dublado: a suite roda dentro do repositorio, entao o
        descobridor de verdade acharia um SHA e estes testes, que falam da
        SENTINELA, passariam a falar de outra coisa.
        """
        monkeypatch.setattr(config_module, "sha_do_repositorio", lambda _raiz: None)

    def test_variavel_vazia_no_ambiente_vira_nao_carimbado(self, monkeypatch, sem_git_no_contexto):
        monkeypatch.setenv("GIT_SHA", "")

        assert Settings().GIT_SHA == GIT_SHA_NAO_CARIMBADO

    def test_so_espaco_tambem_conta_como_ausente(self, monkeypatch, sem_git_no_contexto):
        """Um espaco sobrando na linha do `.env` nao pode virar "carimbado"."""
        monkeypatch.setenv("GIT_SHA", "   ")

        assert Settings().GIT_SHA == GIT_SHA_NAO_CARIMBADO

    def test_variavel_ausente_tambem_vira_nao_carimbado(self, monkeypatch, sem_git_no_contexto):
        """AUSENTE e VAZIA sao caminhos diferentes no pydantic, e os dois
        precisam chegar no mesmo lugar.

        Vazia passa pelo validator `mode="before"`; ausente cai no
        `default_factory`, que validator nenhum toca. Foi por essa fresta que a
        descoberta pelo `.git` quase entrou pela metade."""
        monkeypatch.delenv("GIT_SHA", raising=False)

        assert Settings().GIT_SHA == GIT_SHA_NAO_CARIMBADO

    def test_sha_de_verdade_atravessa_intacto(self, monkeypatch):
        """A contraprova: sem ela, um validador que devolvesse sempre a
        sentinela passaria nos dois testes acima e apagaria o carimbo real."""
        monkeypatch.setenv("GIT_SHA", SHA_FALSO)

        assert Settings().GIT_SHA == SHA_FALSO

    def test_o_ambiente_vazio_faz_o_boot_gritar(self, monkeypatch, caplog, sem_git_no_contexto):
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


class TestOSHaDescobertoNoGitDaImagem:
    """O carimbo sem prefixo — o `.git` que a imagem carrega.

    O prefixo `GIT_SHA=$(git rev-parse --short HEAD)` funcionava e era
    esquecido, e esquece-lo nao quebra nada: o deploy sobe uma API perfeita
    cujo `/health` responde `nao-carimbado`. A unica forma barata de saber qual
    commit esta no ar some justamente no deploy em que ela faz falta.

    O `.dockerignore` readmite `.git/HEAD`, `.git/refs` e `.git/packed-refs` —
    228 kB, sem objeto nenhum — e `sha_do_repositorio` le o SHA de la.
    """

    SHA_COMPLETO = "0123456789abcdef0123456789abcdef01234567"

    def _repo(self, raiz, head: str) -> None:
        (raiz / ".git").mkdir()
        (raiz / ".git" / "HEAD").write_text(head, encoding="utf-8")

    def test_head_destacado_e_o_proprio_sha(self, tmp_path):
        """`git checkout <sha>` no servidor para voltar uma versao. Caminho
        raro, e o que mais precisa de carimbo."""
        self._repo(tmp_path, self.SHA_COMPLETO)

        assert sha_do_repositorio(tmp_path) == self.SHA_COMPLETO[:7]

    def test_ref_solta(self, tmp_path):
        self._repo(tmp_path, "ref: refs/heads/main\n")
        destino = tmp_path / ".git" / "refs" / "heads"
        destino.mkdir(parents=True)
        (destino / "main").write_text(self.SHA_COMPLETO + "\n", encoding="utf-8")

        assert sha_do_repositorio(tmp_path) == self.SHA_COMPLETO[:7]

    def test_ref_empacotada_por_git_gc(self, tmp_path):
        """Depois de um `git gc` o arquivo solto SOME e o ref passa a viver so
        no `packed-refs`. Sem este caminho o carimbo sumiria num repositorio
        que rodou manutencao, com o sintoma "funcionava e parou"."""
        self._repo(tmp_path, "ref: refs/heads/main\n")
        (tmp_path / ".git" / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted \n"
            f"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa refs/heads/outra\n"
            f"{self.SHA_COMPLETO} refs/heads/main\n"
            "^bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n",
            encoding="utf-8",
        )

        assert sha_do_repositorio(tmp_path) == self.SHA_COMPLETO[:7]

    def test_sem_git_nenhum_e_None(self, tmp_path):
        assert sha_do_repositorio(tmp_path) is None

    def test_ref_que_nao_existe_em_lugar_nenhum_e_None(self, tmp_path):
        self._repo(tmp_path, "ref: refs/heads/main\n")

        assert sha_do_repositorio(tmp_path) is None

    def test_git_como_ARQUIVO_e_None(self, tmp_path):
        """Worktree e submodulo gravam `.git` como arquivo (`gitdir: ...`).
        Ler `HEAD` dentro dele levanta `NotADirectoryError`, que e `OSError`."""
        (tmp_path / ".git").write_text("gitdir: /outro/lugar\n", encoding="utf-8")

        assert sha_do_repositorio(tmp_path) is None

    @pytest.mark.parametrize(
        "conteudo",
        ["0123456", "", "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "nao e um sha"],
    )
    def test_lixo_no_lugar_do_sha_e_None_e_nao_um_carimbo_falso(self, tmp_path, conteudo):
        """Pior que nao carimbar: um `git_sha` de aparencia normal que nao casa
        com commit nenhum. Quem for conferir procura o commit antes de
        suspeitar do carimbo."""
        self._repo(tmp_path, conteudo)

        assert sha_do_repositorio(tmp_path) is None

    def test_o_build_arg_GANHA_do_que_foi_descoberto(self, monkeypatch):
        """Quem constroi passando o arg esta dizendo alguma coisa que o `.git`
        do contexto nao teria como saber. Inverter a ordem tiraria de quem
        constroi em CI a unica fonte que ele tem."""
        monkeypatch.setattr(config_module, "sha_do_repositorio", lambda _raiz: "ddddddd")
        monkeypatch.setenv("GIT_SHA", SHA_FALSO)

        assert Settings().GIT_SHA == SHA_FALSO

    def test_arg_vazio_cai_no_descoberto_e_nao_na_sentinela(self, monkeypatch):
        """O caminho do compose: `${GIT_SHA:-}` vira `--build-arg GIT_SHA=`, a
        imagem nasce com `ENV GIT_SHA=""`, e e aqui que o carimbo deixa de
        depender do prefixo."""
        monkeypatch.setattr(config_module, "sha_do_repositorio", lambda _raiz: "ddddddd")
        monkeypatch.setenv("GIT_SHA", "")

        assert Settings().GIT_SHA == "ddddddd"

    def test_variavel_ausente_tambem_cai_no_descoberto(self, monkeypatch):
        """A outra metade, que passa por outro mecanismo do pydantic: ausente
        cai no `default_factory`, que nenhum validator alcanca."""
        monkeypatch.setattr(config_module, "sha_do_repositorio", lambda _raiz: "ddddddd")
        monkeypatch.delenv("GIT_SHA", raising=False)

        assert Settings().GIT_SHA == "ddddddd"

    def test_a_raiz_consultada_e_a_do_repositorio(self):
        """A contraprova de que o caminho aponta para onde o `COPY . .` poe as
        coisas: `/app` na imagem, a raiz do repo aqui. Um `parents[]` errado
        deixaria todos os testes acima verdes e o carimbo mudo em producao."""
        assert (config_module.RAIZ_DO_REPOSITORIO / "Dockerfile").is_file()
        assert (config_module.RAIZ_DO_REPOSITORIO / "src" / "core" / "config.py").is_file()
