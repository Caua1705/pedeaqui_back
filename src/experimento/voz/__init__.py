"""Prova de conceito: voz em tempo real com a OpenAI Realtime API.

A pergunta que este experimento existe para responder: **o cliente consegue
pedir informação do cardápio falando, e o que já está construído aguenta
servir isso?**

O desenho é o da proposta: a conversa de áudio sai do NAVEGADOR direto para a
OpenAI, e o backend só faz duas coisas — emite a credencial efêmera e responde
à ferramenta de busca. O áudio não passa por aqui.

O que este experimento NÃO tem, e que faltaria para virar produto: login,
cota por cliente e por restaurante, teto de duração de sessão, registro de
consumo e conciliação de fatura. Ver `sessao_service.py`.
"""
