"""Mostra a similaridade de cada produto que a busca do Rapi devolveria.

EXISTE PORQUE `AI_SEARCH_MIN_SIMILARITY` E UM NUMERO QUE NAO DA PARA CHUTAR.
Ele decide o que entra no prompt, e errar para os dois lados custa caro em
sentidos opostos:

- **baixo demais** — a saudacao "oi" volta com uma agua mineral anexada, e o
  Rapi oferece produto aleatorio a quem so cumprimentou;
- **alto demais** — a pergunta legitima sobre um produto que existe volta
  vazia, e o Rapi diz "nao temos" sobre um item do cardapio. Sem erro, sem
  log, sem nada. E o modo de falha que `docs/busca-vetorial-e-indice-ann.md`
  descreve na secao do ANN, e o mais caro dos dois.

E a faixa real de similaridade deste cardapio, registrada naquele documento,
e de **0,17 a 0,66** — quer dizer, o piso mora no meio dos acertos
legitimos, nao numa terra de ninguem. Mover o numero as cegas e mover a
fronteira sem enxergar os dois lados dela.

    python scripts/afere_limiar_de_similaridade.py \\
        --restaurant-id <uuid> --branch-id <uuid> \\
        --pergunta "oi" --pergunta "quanto custa a picanha?"

    # ou o arquivo com uma pergunta por linha (o jeito de reconferir uma sessao)
    python scripts/afere_limiar_de_similaridade.py \\
        --restaurant-id <uuid> --branch-id <uuid> --perguntas sessao.txt

    docker exec pedeaqui-api python scripts/afere_limiar_de_similaridade.py ...

**Le producao e nao escreve nada.** Usa `DATABASE_URL` e `OPENAI_API_KEY` do
ambiente, e a busca roda com `--piso 0`, de proposito: o objetivo e ver o que
o piso de hoje esta CORTANDO, e um script que aplicasse o piso vigente nunca
mostraria isso. O custo e uma chamada de embedding por pergunta.

O cache do `chat_cache` NAO e usado aqui: ele e por processo, este processo
morre no fim, e servir do cache esconderia justamente a pergunta que se
quer medir.
"""

import argparse
import sys
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.ai.services.embedding_service import EmbeddingService
from src.core.config import settings
from src.db.session import SessionLocal
from src.repositories.ai_repository import AIRepository


# Os candidatos que a tabela compara. O primeiro e o piso de hoje.
LIMIARES = (0.30, 0.35, 0.40, 0.45, 0.50)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Similaridade de cada produto que a busca do Rapi devolveria.",
    )
    parser.add_argument("--restaurant-id", required=True, type=uuid.UUID)
    parser.add_argument("--branch-id", required=True, type=uuid.UUID)
    parser.add_argument(
        "--pergunta",
        action="append",
        default=[],
        help="Pode repetir. Ou use --perguntas com um arquivo.",
    )
    parser.add_argument(
        "--perguntas",
        type=Path,
        help="Arquivo com uma pergunta por linha.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="O mesmo top_k do Rapi (default 5).",
    )
    parser.add_argument(
        "--piso",
        type=float,
        default=0.0,
        help="Piso da consulta. 0 de proposito: mostra o que o piso de hoje corta.",
    )
    args = parser.parse_args()

    perguntas = _perguntas(args)
    if not perguntas:
        print("Nenhuma pergunta. Use --pergunta ou --perguntas.")
        return 1

    print(f"piso vigente em settings: AI_SEARCH_MIN_SIMILARITY={settings.AI_SEARCH_MIN_SIMILARITY}")
    print(f"consultando com piso={args.piso} e top_k={args.top_k}\n")

    embedding_service = EmbeddingService()
    with SessionLocal() as db:
        repositorio = AIRepository(db)
        resumo = {limiar: 0 for limiar in LIMIARES}

        for pergunta in perguntas:
            produtos = repositorio.similarity_search(
                restaurant_id=args.restaurant_id,
                branch_id=args.branch_id,
                embedding=embedding_service.generate_embedding(pergunta),
                top_k=args.top_k,
                min_similarity=args.piso,
            )
            _imprime_pergunta(pergunta, produtos)
            for limiar in LIMIARES:
                if any(produto["similarity"] >= limiar for produto in produtos):
                    resumo[limiar] += 1

    _imprime_resumo(resumo, len(perguntas))
    return 0


def _perguntas(args) -> list[str]:
    perguntas = list(args.pergunta)
    if args.perguntas:
        linhas = args.perguntas.read_text(encoding="utf-8").splitlines()
        perguntas.extend(linha.strip() for linha in linhas if linha.strip())
    return perguntas


def _imprime_pergunta(pergunta: str, produtos: list[dict]) -> None:
    print(f"» {pergunta}")
    if not produtos:
        print("    (a busca nao devolveu nada nem sem piso)\n")
        return

    for produto in produtos:
        similaridade = produto["similarity"]
        # A marca diz de um relance qual piso ainda deixa este produto passar.
        cortes = "".join(
            "x" if similaridade >= limiar else "." for limiar in LIMIARES
        )
        print(f"    {similaridade:.3f}  [{cortes}]  {produto['name']}")
    print()


def _imprime_resumo(resumo: dict[float, int], total: int) -> None:
    print("-" * 60)
    print(f"marca [{''.join(str(l).replace('0.', '') for l in LIMIARES)}] = "
          f"limiares {', '.join(str(l) for l in LIMIARES)}")
    print()
    print("perguntas que AINDA trazem ao menos um produto, por limiar:")
    for limiar in LIMIARES:
        print(f"    {limiar:.2f}  ->  {resumo[limiar]:>3} de {total}")
    print()
    print("Ler assim: o numero cair e o objetivo em saudacao e conversa fora do")
    print("assunto, e e o PREJUIZO em pergunta de cardapio. Confira pergunta a")
    print("pergunta acima antes de escolher — o agregado sozinho nao distingue.")


if __name__ == "__main__":
    raise SystemExit(main())
