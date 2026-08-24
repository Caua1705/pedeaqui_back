"""Atendimento por voz em tempo real, com a Realtime API da OpenAI.

O desenho: a conversa de audio sai do NAVEGADOR direto para a OpenAI, e o
backend faz duas coisas — emite a credencial efemera e responde a ferramenta
de busca. O audio nao passa por aqui.

    src/api/voice.py                        as rotas, sob /voice
    src/ai/voice/voice_prompt.py            as instrucoes faladas
    src/ai/voice/realtime_client.py         a chave mestra e a OpenAI
    src/ai/voice/session_service.py         livro-razao, cotas e corte
    src/ai/voice/search_service.py          a ferramenta de busca
    src/repositories/voice_session_repository.py

Nao ha bancada dentro do pacote. A que existia (`page.html`, em `/voice/test`)
saiu em 24/08/2026; a viva e externa ao repositorio — ver `COMO-RODAR.md`.

Nasceu como experimento na branch `experimento/voz` e a leitura de origem
esta em `docs/auditoria-voz-vs-texto.md` — o que este agente compartilha com o
chat de texto, e onde os dois divergiram.
"""
