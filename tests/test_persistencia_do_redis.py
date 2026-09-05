"""O Redis continua sem persistência — é o que segura texto de cliente em claro.

`src/ai/services/chat_history.py:165-177` diz, no código, que o valor guardado
pelo histórico do Rapi é **texto em claro**: o modelo precisa ler o que a
pessoa escreveu, e não há digest possível para isso. E lista as três defesas em
volta, a segunda das quais é uma linha do `docker-compose.yml`:

> **Nenhuma persistência.** Conferido em produção em 04/09/2026:
> `aof_enabled=0` e RDB desligado. Se isso mudar, o texto do cliente passa a
> existir em disco além do TTL — e este backend precisa de outra conversa
> ANTES, não depois.

**"Precisa de outra conversa ANTES" é a descrição de um portão, e o portão não
existia.** Persistência é uma flag de compose: duas linhas, editáveis por quem
está resolvendo outro problema.

## Por que o TTL não basta sozinho

O TTL é do Redis em memória. Com `RDB` ou `AOF` ligado, um snapshot grava a
chave em disco **e o arquivo não expira sozinho** — "1 hora" vira "até alguém
apagar o dump", e dump costuma ir para backup, que é onde ele fica anos.

E não é só o chat. Com persistência ligada, vão para disco também o **IP do
cliente** (o contador do `slowapi` põe o IP na chave, por desenho da
biblioteca) e a **coordenada** da estimativa de entrega. Os três têm TTL, e o
TTL é a única coisa que os apaga.

## O comentário do compose dava o motivo errado

Ele existe desde antes do histórico ir para o Redis, e justificava por CACHE:
*"um dump em disco só traria contador velho de volta depois de um restart, e o
custo do fsync não compra nada aqui"*. Verdade, e é o argumento que perde:
quem for ligar AOF vai justamente porque quer o contador sobrevivendo ao
restart, e o comentário concorda que essa é a única coisa em jogo.

O motivo que manda é o outro, e agora está escrito lá.
"""

from pathlib import Path

import pytest


COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def comando_do_servico(nome: str) -> list[str]:
    """Os itens de `command:` de um serviço do compose.

    Feito à mão, e não com PyYAML: o PyYAML está no lock por dependência de
    terceiro, não porque este projeto o declare. Um teste que dependa dele
    quebra no dia em que quem o traz sumir — por um motivo que não tem nada a
    ver com o que ele vigia.
    """
    linhas = COMPOSE.read_text(encoding="utf-8").splitlines()
    dentro_do_servico = False
    dentro_do_comando = False
    itens: list[str] = []
    for linha in linhas:
        if linha.startswith(f"  {nome}:"):
            dentro_do_servico = True
            continue
        if dentro_do_servico and linha.startswith("  ") and not linha.startswith("   "):
            break  # começou outro serviço
        if not dentro_do_servico:
            continue
        if linha.strip() == "command:":
            dentro_do_comando = True
            continue
        if dentro_do_comando:
            despido = linha.strip()
            if despido.startswith("- "):
                itens.append(despido[2:].strip().strip('"'))
            elif despido and not despido.startswith("#"):
                dentro_do_comando = False
    return itens


def valor_da_flag(comando: list[str], flag: str) -> str | None:
    """`--save` e `--appendonly` são pares posicionais na lista."""
    if flag not in comando:
        return None
    indice = comando.index(flag)
    return comando[indice + 1] if indice + 1 < len(comando) else None


class TestOExtratorEnxerga:
    """Um extrator quebrado devolve lista vazia, e lista vazia passaria em
    qualquer asserção de ausência. As flags que ele TEM que achar primeiro."""

    def test_acha_o_comando_do_redis(self):
        comando = comando_do_servico("redis")

        assert comando[0] == "redis-server"
        assert "--maxmemory" in comando
        assert valor_da_flag(comando, "--maxmemory") == "256mb"

    def test_servico_que_nao_existe_devolve_vazio(self):
        assert comando_do_servico("servico-que-nao-existe") == []


class TestSemPersistencia:
    def test_o_rdb_esta_desligado(self):
        """`--save ""` é o desligamento do snapshot periódico. Sem ele valem os
        padrões do Redis, que salvam sozinhos — e o padrão é o caminho por onde
        isto voltaria sem ninguém decidir nada."""
        assert valor_da_flag(comando_do_servico("redis"), "--save") == ""

    def test_o_aof_esta_desligado(self):
        assert valor_da_flag(comando_do_servico("redis"), "--appendonly") == "no"

    @pytest.mark.parametrize("flag", ["--save", "--appendonly"])
    def test_a_flag_e_declarada_explicitamente(self, flag):
        """Não basta o valor estar certo por omissão: a flag precisa estar
        escrita, porque é ela que faz alguém parar para pensar antes de mudar.
        Herdar o padrão do Redis é o mesmo estado sem a decisão."""
        assert flag in comando_do_servico("redis")


class TestOMotivoEstaEscritoOndeSeEdita:
    def test_o_compose_diz_que_e_dado_pessoal_e_nao_so_cache(self):
        """A trava impede a mudança; o comentário é o que faz quem a leu
        entender por que ela existe. Sem o motivo certo ali, o portão vermelho
        vira obstáculo a contornar."""
        # `casefold` porque a frase está em caixa alta no arquivo, e o que
        # este teste vigia é o motivo estar lá — não a tipografia dele.
        texto = COMPOSE.read_text(encoding="utf-8").casefold()

        assert "chat_history.py" in texto
        assert "texto em claro" in texto
        assert "lgpd" in texto
