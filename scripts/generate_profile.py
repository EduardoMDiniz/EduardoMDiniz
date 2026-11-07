#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter
from datetime import date, timedelta
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

USERNAME = os.environ.get('PROFILE_USERNAME', 'EduardoMDiniz')
GH_TOKEN = os.environ.get('GH_TOKEN', '')
PRIVATE_REPOS_TOKEN = os.environ.get('PRIVATE_REPOS_TOKEN', '')
CONTRIB_TOKEN = os.environ.get('PROFILE_CONTRIB_TOKEN') or GH_TOKEN
OUTPUT = Path(os.environ.get('OUTPUT_SVG', 'contributions-3d.svg'))

API = 'https://api.github.com'
GRAPHQL = API + '/graphql'
WIDTH, HEIGHT = 770, 357
TILE_W = (11.6, 3.12)
TILE_D = (6.9, -4.4)
ORIGIN = (30.0, 120.0)
FONT = 'Segoe UI,Helvetica,Arial,sans-serif'
LANG_COLORS = ['#58A6FF', '#D2A8FF', '#F5C475', '#FF9B8A', '#79DDEA', '#8B949E']


def warn(message: str) -> None:
    print(f'::warning::{message}', file=sys.stderr)


def gh_json(url: str, token: str, payload: dict | None = None) -> object:
    headers = {
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'profile-contributions-3d',
    }
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode('utf-8')
    try:
        with urlopen(Request(url, headers=headers, data=data), timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code} em {url.split("?")[0]}') from None
    except URLError as exc:
        raise RuntimeError(f'Erro de rede: {exc.reason}') from None


def get_days() -> list[dict]:
    query = '''query($username:String!) {
      user(login:$username) {
        contributionsCollection {
          contributionCalendar { weeks { contributionDays { date contributionCount } } }
        }
      }
    }'''
    if not CONTRIB_TOKEN:
        raise RuntimeError('Defina GH_TOKEN ou PROFILE_CONTRIB_TOKEN.')
    result = gh_json(GRAPHQL, CONTRIB_TOKEN, {'query': query, 'variables': {'username': USERNAME}})
    if not isinstance(result, dict) or result.get('errors'):
        raise RuntimeError(f'GraphQL: {json.dumps(result, ensure_ascii=False)[:500]}')
    try:
        weeks = result['data']['user']['contributionsCollection']['contributionCalendar']['weeks']
        days = [
            {'date': date.fromisoformat(d['date']), 'count': max(0, int(d['contributionCount']))}
            for week in weeks for d in week['contributionDays']
        ]
    except (KeyError, ValueError, TypeError):
        raise RuntimeError('Formato inesperado do calendário.') from None
    if not days:
        raise RuntimeError('Calendário vazio.')
    return days


def get_repos(base_url: str, token: str) -> list[dict]:
    repos: list[dict] = []
    for page in range(1, 21):
        sep = '&' if '?' in base_url else '?'
        batch = gh_json(f'{base_url}{sep}per_page=100&page={page}', token)
        if not isinstance(batch, list):
            raise RuntimeError('Resposta inesperada ao listar repositórios.')
        repos.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return repos
    raise RuntimeError('Mais de 2000 repositórios.')


def get_languages() -> tuple[Counter, int]:
    accessible = [(r, GH_TOKEN) for r in get_repos(f'{API}/users/{quote(USERNAME)}/repos?type=owner', GH_TOKEN)]
    if PRIVATE_REPOS_TOKEN:
        try:
            private = get_repos(
                f'{API}/user/repos?visibility=private&affiliation=owner,collaborator,organization_member',
                PRIVATE_REPOS_TOKEN,
            )
            accessible.extend((r, PRIVATE_REPOS_TOKEN) for r in private if r.get('private'))
        except RuntimeError as exc:
            warn(f'Repositórios privados ignorados: {exc}')

    totals: Counter = Counter()
    seen: set[str] = set()
    private_repos = 0
    own_profile = f'{USERNAME}/{USERNAME}'.lower()
    for repo, token in accessible:
        name = str(repo.get('full_name') or '')
        key = name.lower()
        if repo.get('fork') or not name or key == own_profile or key in seen:
            continue
        seen.add(key)
        try:
            sizes = gh_json(f'{API}/repos/{quote(name, safe="/")}/languages', token)
        except RuntimeError as exc:
            warn(f'Linguagens ignoradas: {exc}')
            continue
        if not isinstance(sizes, dict):
            continue
        counted = False
        for lang, n in sizes.items():
            if isinstance(lang, str) and isinstance(n, int) and n > 0:
                totals[lang] += n
                counted = True
        if counted and repo.get('private'):
            private_repos += 1
    return totals, private_repos


def poly(points: list[tuple[float, float]], fill: str, stroke: str = 'none') -> str:
    coords = ' '.join(f'{x:.2f},{y:.2f}' for x, y in points)
    return f'<polygon points="{coords}" fill="{fill}" stroke="{stroke}" stroke-width="0.65"/>'


def tile_points(week: int, weekday: int) -> list[tuple[float, float]]:
    x = ORIGIN[0] + week * TILE_W[0] + weekday * TILE_D[0]
    y = ORIGIN[1] + week * TILE_W[1] + weekday * TILE_D[1]
    a = (x, y)
    b = (x + TILE_W[0], y + TILE_W[1])
    c = (b[0] + TILE_D[0], b[1] + TILE_D[1])
    d = (x + TILE_D[0], y + TILE_D[1])
    return [a, b, c, d]


def bar_height(count: int) -> float:
    if count <= 0:
        return 0.0
    return min(49.0, 12.5 + 8.5 * math.log1p(count))


def text(x: float, y: float, content: str, size: float, fill: str, **attrs: str) -> str:
    extra = ''.join(f' {k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return (f'<text x="{x:.2f}" y="{y:.2f}" font-family="{FONT}" font-size="{size}" '
            f'fill="{fill}"{extra}>{escape(content)}</text>')


def language_legend(languages: Counter, private_repos: int) -> list[str]:
    out = [
        '<line x1="26" y1="292" x2="744" y2="292" stroke="#263039" stroke-width="1"/>',
        text(30, 309, 'LINGUAGENS', 10.5, '#B8C7CB', font_weight='bold', letter_spacing='1'),
    ]
    scope = 'públicos + privados autorizados (agregado)' if private_repos else 'repositórios públicos'
    out.append(text(742, 309, scope, 9.5, '#6E8491', text_anchor='end'))
    total = sum(languages.values())
    if not total:
        out.append(text(30, 336, 'Sem linguagens identificadas nos repositórios acessíveis', 11, '#93A2AC'))
        return out
    top = languages.most_common(len(LANG_COLORS))
    out.append('<rect x="30" y="321" width="712" height="6" rx="3" fill="#263039"/>')
    px = 30.0
    for i, (_, n) in enumerate(top):
        length = 712.0 * n / total
        out.append(f'<rect x="{px:.2f}" y="321" width="{length:.2f}" height="6" fill="{LANG_COLORS[i]}"/>')
        px += length
    label_x = 30.0
    for i, (name, n) in enumerate(top):
        label = f'{name} {100 * n / total:.1f}%'
        estimated = 18.0 + len(label) * 6.1
        if label_x + estimated > 748:
            break
        out.append(f'<rect x="{label_x:.2f}" y="340" width="6" height="6" rx="1" fill="{LANG_COLORS[i]}"/>')
        out.append(text(label_x + 10, 346, label, 10.5, '#B3C6C9'))
        label_x += estimated
    return out


def make_svg(days: list[dict], languages: Counter, private_repos: int) -> str:
    day_lookup = {d['date']: int(d['count']) for d in days}
    first = min(day_lookup)
    last = max(day_lookup)
    first_sunday = first - timedelta(days=(first.weekday() + 1) % 7)
    n_weeks = min(53, (last - first_sunday).days // 7 + 1)
    total = sum(day_lookup.values())
    plural = 'contribuições' if total != 1 else 'contribuição'

    result = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'role="img" aria-label="Trilha 3D com {total} {plural} no GitHub nos últimos 12 meses e linguagens usadas">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" rx="9" fill="#0D1117"/>',
        text(30, 26, 'ATIVIDADE NO GITHUB', 12, '#D2E6E8', font_weight='bold', letter_spacing='1.4'),
        text(743, 26, f'{total} {plural} · últimos 12 meses', 10, '#80949E', text_anchor='end'),
    ]
    tiles: list[str] = []
    bars: list[tuple] = []
    for w in range(n_weeks):
        for d in range(7):
            when = first_sunday + timedelta(days=w * 7 + d)
            if when > last or when < first:
                continue
            count = day_lookup.get(when, 0)
            points = tile_points(w, d)
            base = '#33393E' if (w + d) % 2 == 0 else '#2C3237'
            tiles.append(poly(points, base, '#1D252B'))
            if count:
                depth = sum(p[1] for p in points) / 4
                bars.append((depth, count, when, bar_height(count), points))
    result.extend(tiles)
    bars.sort(key=lambda entry: entry[0])
    for _, count, when, height, (a, b, c, e) in bars:
        aa, bb, cc, ee = ((x, y - height) for x, y in (a, b, c, e))
        top_shade = '#47EAA4' if count >= 5 else '#36D68B' if count >= 2 else '#2BC17D'
        face_a = '#127A55' if count >= 2 else '#126549'
        face_b = '#209A68' if count >= 2 else '#1D855D'
        label = f'{when.isoformat()}: {count} ' + ('contribuição' if count == 1 else 'contribuições')
        result.append(f'<g><title>{escape(label)}</title>')
        result.append(poly([a, b, bb, aa], face_a))
        result.append(poly([b, c, cc, bb], face_b))
        result.append(poly([aa, bb, cc, ee], top_shade, '#168959'))
        result.append('</g>')
    result.extend(language_legend(languages, private_repos))
    result.append('</svg>')
    return '\n'.join(result) + '\n'


def main() -> None:
    days = get_days()
    languages, private_repos = get_languages()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(make_svg(days, languages, private_repos), encoding='utf-8')
    print(f'{len(days)} dias, {sum(d["count"] for d in days)} contribuições, '
          f'{len(languages)} linguagens, {private_repos} privados. Saída: {OUTPUT}')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(f'::error::{exc}', file=sys.stderr)
        sys.exit(1)
