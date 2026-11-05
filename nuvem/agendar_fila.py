"""Agenda no Buffer o dia seguinte, a partir de fila/<data>.json, depois das 21h05 de Lisboa (o Buffer grátis só deixa
10 publicações em fila por canal). Corre no GitHub Actions, para funcionar com o PC do Gonçalo desligado.

Um dia só entra na fila com o sim do Gonçalo (`agendar_dia.py <data> --nuvem`). Sem ficheiro para amanhã, não faz nada.
Idempotente: salta o que já está em fila/<data>.feito.json e o que já está no Buffer à mesma hora. Uma publicação cujo
resultado é desconhecido nunca se repete: o dia fica parado, com o motivo escrito, até alguém olhar.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

FILA = Path('fila')
LIMITE_FILA = 10
SERVICOS = ('instagram', 'facebook')
CRIAR = '''mutation($i: CreatePostInput!) { createPost(input: $i) {
  ... on PostActionSuccess { post { id status dueAt } }
  ... on MutationError { message } } }'''


def q(query: str, variaveis: dict | None = None) -> dict:
    corpo = json.dumps({'query': query, 'variables': variaveis or {}}).encode()
    req = urllib.request.Request('https://api.buffer.com', data=corpo, method='POST', headers={
        'Authorization': f'Bearer {os.environ["BUFFER_API_KEY"].strip()}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode('utf-8'))
    if d.get('errors'):
        raise RuntimeError(str(d['errors'])[:300])
    return d['data']


def em_fila(org: str) -> tuple[dict[str, int], set[tuple[str, str]]]:
    r = q('''query($o: OrganizationId!) { posts(input: { organizationId: $o, filter: { status: [scheduled] } }, first: 100) {
              edges { node { channelService dueAt } } } }''', {'o': org})
    contagem: dict[str, int] = {}
    horas = set()
    for e in r['posts']['edges']:
        s = e['node']['channelService']
        contagem[s] = contagem.get(s, 0) + 1
        horas.add((s, e['node']['dueAt'][:16]))
    return contagem, horas


def main() -> int:
    agora = datetime.now(ZoneInfo('Europe/Lisbon'))
    if len(sys.argv) > 1 and sys.argv[1]:
        dia = sys.argv[1]
    else:
        if (agora.hour, agora.minute) < (21, 5):
            print(f'ainda não são 21h05 em Lisboa ({agora:%H:%M})')
            return 0
        dia = (agora + timedelta(days=1)).strftime('%Y-%m-%d')
    fila = FILA / f'{dia}.json'
    if not fila.exists():
        print(f'{dia}: não está na fila, não há nada a fazer')
        return 0
    registo = FILA / f'{dia}.feito.json'
    feito = json.loads(registo.read_text(encoding='utf-8')) if registo.exists() else {'posts': {}}
    if feito.get('parado'):
        print(f'{dia}: parado, ver à mão: {feito["parado"]}')
        return 1
    itens = json.loads(fila.read_text(encoding='utf-8'))['itens']
    org = q('query { account { organizations { id } } }')['account']['organizations'][0]['id']
    canais = {c['service']: c['id'] for c in q('query($o: OrganizationId!) { channels(input: { organizationId: $o }) { id service } }', {'o': org})['channels']}
    contagem, horas = em_fila(org)
    falta = [(it, s) for it in itens for s in SERVICOS if f'{dia}|{it["slot"]}|{s}' not in feito['posts']
             and (s, it['entradas'][s]['dueAt'][:16]) not in horas]
    for s in SERVICOS:
        novos = sum(1 for _, x in falta if x == s)
        if contagem.get(s, 0) + novos > LIMITE_FILA:
            print(f'{s}: {contagem.get(s, 0)} em fila + {novos} novos passa os {LIMITE_FILA}; tenta-se na próxima corrida')
            return 0
    limite = datetime.now(ZoneInfo('UTC')) + timedelta(minutes=15)
    for it, s in falta:
        e = {**it['entradas'][s], 'channelId': canais[s]}
        if datetime.strptime(e['dueAt'], '%Y-%m-%dT%H:%M:%S.000Z').replace(tzinfo=ZoneInfo('UTC')) < limite:
            feito['parado'] = f'{it["slot"]} {s}: a hora já passou ou está demasiado perto'
            break
        k = f'{dia}|{it["slot"]}|{s}'
        try:
            r = q(CRIAR, {'i': e})['createPost']
        except Exception as erro:  # noqa: BLE001  (não se sabe se ficou: nunca repetir às cegas)
            feito['parado'] = f'{k}: resultado desconhecido: {str(erro)[:200]}'
            break
        if 'message' in r:
            feito['parado'] = f'{k}: {r["message"]}'
            break
        feito['posts'][k] = {**r['post'], 'titulo': it['titulo'], 'pecas': it['pecas'], 'tipo': it['tipo']}
        print('agendado', k, r['post']['dueAt'])
    registo.write_text(json.dumps(feito, ensure_ascii=False, indent=1), encoding='utf-8')
    if feito.get('parado'):
        print('PARADO:', feito['parado'])
        return 1
    erros = []
    for k, v in feito['posts'].items():
        d = q('query($i: PostId!) { post(input: { id: $i }) { status dueAt } }', {'i': v['id']})['post']
        if d['status'] not in ('scheduled', 'sent') or d['dueAt'][:16] != v['dueAt'][:16]:
            erros.append((k, d))
    if erros:
        print('RELEITURA FALHOU:', erros)
        return 1
    print(f'{dia}: {len(feito["posts"])} entradas no Buffer, à hora certa')
    return 0


if __name__ == '__main__':
    sys.exit(main())
