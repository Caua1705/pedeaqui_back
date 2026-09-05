"""De que commit este codigo e, descoberto de DENTRO da propria imagem.

## Por que existe

O carimbo dependia de alguem lembrar do prefixo:

    GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build

Esquecer o prefixo nao quebra nada — e esse e o problema. O `docker compose up
-d --build` pelado sobe uma imagem perfeitamente funcional cujo `/health`
responde `git_sha=nao-carimbado`, e a unica forma barata de saber qual commit
esta no ar some no deploy em que ela mais faz falta. Aconteceu.

O `ARG GIT_SHA` do `Dockerfile` continua existindo e continua GANHANDO: e o
caminho de quem constroi fora de um repositorio (CI, registry, tarball). O que
esta funcao acrescenta e o segundo caminho, que nao depende de ninguem lembrar
de nada — o contexto de build sabe em que commit esta, e a imagem passa a
carregar a resposta.

## Como

O `.dockerignore` exclui `.git` inteiro (o object store sao dezenas de MB) e
readmite tres coisas: `.git/HEAD`, `.git/refs` e `.git/packed-refs`. Sao 228 kB
medidos, sem um unico objeto — o suficiente para responder "qual e o SHA do
HEAD" e nada mais. Nem `git log`, nem `git diff`, nem os fontes de commit
nenhum.

Ler isso a mao, e nao chamar `git`: **nao ha git dentro da imagem**, e instalar
um binario inteiro para ler dois arquivos de texto seria trocar o problema por
um maior.

## O que ele NAO sabe

**Se a arvore estava suja.** Um `docker compose up --build` com edicao nao
commitada produz uma imagem que contem a edicao e um SHA que nao a contem. Isso
ja valia com o prefixo manual — `git rev-parse` responde o mesmo — e nao piora
aqui; saber exigiria o object store, que e justamente o que nao entra.

**Se o commit foi empurrado.** O incidente de 24/08/2026 foi tres commits sem
`push`: o carimbo teria dito a verdade (o codigo no ar ERA aquele), e quem
procurasse o SHA no GitHub nao o acharia. Continua sendo assim.
"""

from pathlib import Path


_TAMANHO_DO_SHA_COMPLETO = 40
_TAMANHO_DO_SHA_CURTO = 7


def sha_do_repositorio(raiz: Path) -> str | None:
    """O SHA curto do HEAD, lido de `<raiz>/.git`. `None` quando nao da para saber.

    `None` e nao a sentinela: quem decide o que "nao da para saber" significa e
    `Settings.GIT_SHA`, que tem tambem o build arg para consultar antes de
    desistir.
    """
    git = raiz / ".git"

    head = _conteudo(git / "HEAD")
    if head is None:
        return None

    # HEAD destacado: o proprio arquivo ja e o SHA. E o estado de quem deu
    # `git checkout <sha>` no servidor para voltar uma versao — caminho raro e
    # justamente o que mais precisa de carimbo.
    if not head.startswith("ref:"):
        return _curto(head)

    referencia = head.split(":", 1)[1].strip()
    solta = _conteudo(git / referencia)
    if solta is not None:
        return _curto(solta)

    # Ref empacotada. `git gc` move os refs de arquivos soltos para um arquivo
    # unico, e depois disso `.git/refs/heads/main` simplesmente NAO EXISTE. Sem
    # este caminho, o carimbo sumiria num repositorio que rodou manutencao — e
    # o sintoma seria "funcionava e parou", sem nada tendo mudado no deploy.
    return _curto(_do_packed_refs(git, referencia))


def _conteudo(caminho: Path) -> str | None:
    """O texto do arquivo, ou `None` para qualquer motivo de nao conseguir ler.

    `OSError` cobre os tres casos reais de uma vez: nao existe (imagem
    construida sem o sliver de `.git`), nao e diretorio (`.git` como ARQUIVO,
    que e o formato de worktree e de submodulo) e sem permissao.
    """
    try:
        return caminho.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _do_packed_refs(git: Path, referencia: str) -> str | None:
    conteudo = _conteudo(git / "packed-refs")
    if conteudo is None:
        return None

    for linha in conteudo.splitlines():
        # `#` e o cabecalho do arquivo; `^` e o commit para o qual uma tag
        # anotada aponta, e nunca e o alvo da linha anterior.
        if linha.startswith(("#", "^")):
            continue
        sha, _, nome = linha.partition(" ")
        if nome.strip() == referencia:
            return sha

    return None


def _curto(sha: str | None) -> str | None:
    """Os 7 primeiros caracteres, e so se o que veio for mesmo um SHA.

    A conferencia existe porque a alternativa e pior que nao carimbar: um
    arquivo truncado ou com lixo viraria um `git_sha` de aparencia normal que
    nao casa com commit nenhum, e quem for conferir vai procurar o commit antes
    de suspeitar do carimbo.
    """
    if sha is None or len(sha) != _TAMANHO_DO_SHA_COMPLETO:
        return None
    try:
        int(sha, 16)
    except ValueError:
        return None
    return sha[:_TAMANHO_DO_SHA_CURTO]
