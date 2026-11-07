#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
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
WIDTH, HEIGHT = 840, 450
TILE_W = (14.0, 3.9)
TILE_D = (8.3, -5.4)
ORIGIN = (24.0, 150.0)
LEGEND_Y = 380
FONT = 'Segoe UI,Helvetica,Arial,sans-serif'
NEUTRAL = '#6E7F8F'
MONTHS = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez']
FALLBACK_COLORS = ['#58A6FF', '#D2A8FF', '#F5C475', '#FF9B8A', '#79DDEA', '#8B949E']


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


def graphql(token: str, query: str, variables: dict) -> dict:
    if not token:
        raise RuntimeError('Token ausente para o GraphQL.')
    result = gh_json(GRAPHQL, token, {'query': query, 'variables': variables})
    if not isinstance(result, dict) or result.get('errors') or not result.get('data'):
        raise RuntimeError(f'GraphQL: {json.dumps(result, ensure_ascii=False)[:400]}')
    return result['data']


def get_days() -> list[dict]:
    query = '''query($username:String!) {
      user(login:$username) {
        contributionsCollection {
          contributionCalendar { weeks { contributionDays { date contributionCount } } }
        }
      }
    }'''
    data = graphql(CONTRIB_TOKEN, query, {'username': USERNAME})
    try:
        weeks = data['user']['contributionsCollection']['contributionCalendar']['weeks']
        days = [
            {'date': date.fromisoformat(d['date']), 'count': max(0, int(d['contributionCount']))}
            for week in weeks for d in week['contributionDays']
        ]
    except (KeyError, ValueError, TypeError):
        raise RuntimeError('Formato inesperado do calendário.') from None
    if not days:
        raise RuntimeError('Calendário vazio.')
    return days


COMMITS_QUERY = '''query($username:String!, $from:DateTime!, $to:DateTime!) {
  user(login:$username) {
    contributionsCollection(from:$from, to:$to) {
      commitContributionsByRepository(maxRepositories:100) {
        repository { nameWithOwner primaryLanguage { name color } }
        contributions(first:100) { nodes { occurredAt commitCount } }
      }
    }
  }
}'''


def notice(message: str) -> None:
    print(f'::notice::{message}', file=sys.stderr)


COLORS_QUERY = '''query($username:String!, $privacy:RepositoryPrivacy) {
  user(login:$username) {
    repositories(first:100, privacy:$privacy, ownerAffiliations:[OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      nodes { languages(first:20) { nodes { name color } } }
    }
  }
}'''


def collect_colors(token: str, privacy: str | None, colors: dict) -> None:
    data = graphql(token, COLORS_QUERY, {'username': USERNAME, 'privacy': privacy})
    for repo in data['user']['repositories']['nodes']:
        for lang in repo['languages']['nodes']:
            if lang.get('name') and lang.get('color'):
                colors.setdefault(lang['name'], lang['color'])


def collect_commits(token: str, first: date, last: date, by_day: dict, colors: dict, label: str) -> None:
    repos: set[str] = set()
    days: set[date] = set()
    start = first
    while start <= last:
        end = min(start + timedelta(days=120), last)
        data = graphql(token, COMMITS_QUERY, {
            'username': USERNAME,
            'from': datetime(start.year, start.month, start.day, tzinfo=timezone.utc).isoformat(),
            'to': datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).isoformat(),
        })
        for entry in data['user']['contributionsCollection']['commitContributionsByRepository']:
            lang = entry['repository'].get('primaryLanguage') or {}
            name, color = lang.get('name'), lang.get('color')
            if not name:
                continue
            if color:
                colors[name] = color
            repos.add(entry['repository']['nameWithOwner'])
            for node in entry['contributions']['nodes']:
                day = date.fromisoformat(node['occurredAt'][:10])
                by_day[day][name] += int(node['commitCount'])
                days.add(day)
        start = end + timedelta(days=1)
    notice(f'Commits mapeados ({label}): {len(repos)} repositórios, {len(days)} dias.')


def get_day_languages(first: date, last: date) -> tuple[dict[date, str], dict[str, str]]:
    by_day: dict[date, Counter] = defaultdict(Counter)
    colors: dict[str, str] = {}
    collect_commits(CONTRIB_TOKEN, first, last, by_day, colors, 'token do workflow')
    try:
        collect_colors(CONTRIB_TOKEN, 'PUBLIC', colors)
    except RuntimeError as exc:
        warn(f'Cores das linguagens públicas indisponíveis: {exc}')
    if PRIVATE_REPOS_TOKEN:
        try:
            collect_commits(PRIVATE_REPOS_TOKEN, first, last, by_day, colors, 'token privado')
            collect_colors(PRIVATE_REPOS_TOKEN, 'PRIVATE', colors)
        except RuntimeError as exc:
            warn(f'Commits privados sem cor por linguagem: {exc}')
    return {day: langs.most_common(1)[0][0] for day, langs in by_day.items() if langs}, colors


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


def shade(hex_color: str, factor: float) -> str:
    h = hex_color.lstrip('#')
    if len(h) != 6:
        return hex_color
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return '#{:02X}{:02X}{:02X}'.format(*(max(0, min(255, round(c * factor))) for c in (r, g, b)))


def poly(points: list[tuple[float, float]], fill: str, stroke: str = 'none') -> str:
    coords = ' '.join(f'{x:.2f},{y:.2f}' for x, y in points)
    return f'<polygon points="{coords}" fill="{fill}" stroke="{stroke}" stroke-width="0.7"/>'


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
    return min(68.0, 16.5 + 11.0 * math.log1p(count))


def text(x: float, y: float, content: str, size: float, fill: str, **attrs: str) -> str:
    extra = ''.join(f' {k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return (f'<text x="{x:.2f}" y="{y:.2f}" font-family="{FONT}" font-size="{size}" '
            f'fill="{fill}"{extra}>{escape(content)}</text>')


def language_color(name: str, colors: dict[str, str], index: int) -> str:
    return colors.get(name) or FALLBACK_COLORS[index % len(FALLBACK_COLORS)]


def language_legend(languages: Counter, private_repos: int, colors: dict[str, str]) -> list[str]:
    y = LEGEND_Y
    out = [
        f'<line x1="26" y1="{y}" x2="{WIDTH - 26}" y2="{y}" stroke="#263039" stroke-width="1"/>',
        text(30, y + 18, 'LINGUAGENS', 11, '#B8C7CB', font_weight='bold', letter_spacing='1'),
    ]
    scope = 'públicos + privados' if private_repos else 'repositórios públicos'
    out.append(text(WIDTH - 28, y + 18, scope, 10, '#6E8491', text_anchor='end'))
    total = sum(languages.values())
    if not total:
        out.append(text(30, y + 46, 'Sem linguagens identificadas nos repositórios acessíveis', 11, '#93A2AC'))
        return out
    bar_w = WIDTH - 60
    top = languages.most_common(6)
    out.append(f'<rect x="30" y="{y + 31}" width="{bar_w}" height="7" rx="3.5" fill="#263039"/>')
    px = 30.0
    for i, (name, n) in enumerate(top):
        length = bar_w * n / total
        out.append(f'<rect x="{px:.2f}" y="{y + 31}" width="{length:.2f}" height="7" fill="{language_color(name, colors, i)}"/>')
        px += length
    label_x = 30.0
    for i, (name, n) in enumerate(top):
        label = f'{name} {100 * n / total:.1f}%'
        estimated = 20.0 + len(label) * 6.4
        if label_x + estimated > WIDTH - 24:
            break
        out.append(f'<rect x="{label_x:.2f}" y="{y + 51}" width="7" height="7" rx="1.5" fill="{language_color(name, colors, i)}"/>')
        out.append(text(label_x + 11, y + 58, label, 11, '#B3C6C9'))
        label_x += estimated
    return out


def make_svg(days: list[dict], languages: Counter, private_repos: int,
             day_lang: dict[date, str], colors: dict[str, str]) -> str:
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
        f'<rect width="{WIDTH}" height="{HEIGHT}" rx="10" fill="#0D1117"/>',
        text(30, 30, 'ATIVIDADE NO GITHUB', 13, '#D2E6E8', font_weight='bold', letter_spacing='1.4'),
        text(WIDTH - 28, 30, f'{total} {plural} · últimos 12 meses', 11, '#80949E', text_anchor='end'),
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
    angle = math.degrees(math.atan2(TILE_W[1], TILE_W[0]))
    previous_month = None
    for w in range(n_weeks):
        sunday = first_sunday + timedelta(days=w * 7)
        month = max(first, sunday).month
        if month != previous_month and w > 0 or (w == 0 and sunday >= first):
            x = ORIGIN[0] + w * TILE_W[0] + 2
            y = ORIGIN[1] + w * TILE_W[1] + 16
            result.append(text(x, y, MONTHS[month - 1], 10, '#7D8C97', transform=f'rotate({angle:.2f} {x:.2f} {y:.2f})'))
        previous_month = month
    bars.sort(key=lambda entry: entry[0])
    for _, count, when, height, (a, b, c, e) in bars:
        aa, bb, cc, ee = ((x, y - height) for x, y in (a, b, c, e))
        lang = day_lang.get(when)
        color = colors.get(lang or '', NEUTRAL) if lang else NEUTRAL
        label = f'{when.isoformat()}: {count} ' + ('contribuição' if count == 1 else 'contribuições')
        if lang:
            label += f' · {lang}'
        result.append(f'<g><title>{escape(label)}</title>')
        result.append(poly([a, b, bb, aa], shade(color, 0.55)))
        result.append(poly([b, c, cc, bb], shade(color, 0.75)))
        result.append(poly([aa, bb, cc, ee], color, shade(color, 0.85)))
        result.append('</g>')
    result.extend(language_legend(languages, private_repos, colors))
    result.append('</svg>')
    return '\n'.join(result) + '\n'


def main() -> None:
    days = get_days()
    first, last = min(d['date'] for d in days), max(d['date'] for d in days)
    try:
        day_lang, colors = get_day_languages(first, last)
    except RuntimeError as exc:
        warn(f'Cor por linguagem indisponível: {exc}')
        day_lang, colors = {}, {}
    languages, private_repos = get_languages()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(make_svg(days, languages, private_repos, day_lang, colors), encoding='utf-8')
    print(f'{len(days)} dias, {sum(d["count"] for d in days)} contribuições, {len(day_lang)} dias com linguagem, '
          f'{len(languages)} linguagens, {private_repos} privados. Saída: {OUTPUT}')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(f'::error::{exc}', file=sys.stderr)
        sys.exit(1)
