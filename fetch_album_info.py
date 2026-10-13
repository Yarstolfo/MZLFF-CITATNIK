#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_album_info.py — собирает короткую справку об альбомах/синглах с Genius
(дата выхода, число треков, описание) и вставляет её в index.html.

Токен берётся ТОЛЬКО из переменной окружения (не пиши его в файл):
    export GENIUS_TOKEN='...'      (Linux/Mac)
    set GENIUS_TOKEN=...           (cmd)
    $env:GENIUS_TOKEN='...'        (PowerShell)

Использование:
    python fetch_album_info.py index.html
    python fetch_album_info.py index.html --only ADHD,PHANTASMAGORIA
    python fetch_album_info.py index.html --refresh     # перезаписать уже найденное
    python fetch_album_info.py index.html --debug       # только показать, что прочиталось из HTML
    python fetch_album_info.py index.html --verbose
    python fetch_album_info.py index.html --song "нержавейка"   # что находит Genius по треку (✓ — подошло)
    python fetch_album_info.py index.html --page https://genius.com/albums/Mzlff/Adhd
                                                       # проверить вытаскивание текста с одной страницы

Что делает:
  * читает из HTML список альбомов (const albums) и треков (const quotes);
  * для каждого альбома ищет его на Genius (через песни, как в fetch_covers.py);
  * для синглов и фитов берёт данные самой песни;
  * пишет album_info.json и подставляет `const ALBUM_INFO = {...}` в HTML между
    маркерами /*ALBUMINFO:START*/ ... /*ALBUMINFO:END*/.

Ручные правки: в album_info.json добавь у записи "manual": true — скрипт её не тронет.
Описание (блок «About») API Genius для альбомов почти всегда отдаёт пустым, поэтому текст
берётся прямо со страницы альбома/песни (сначала из встроенного JSON страницы, потом из HTML).
Абзацы сохраняются. Если у записи уже есть url, но нет text, повторный запуск ходит только
на страницу — без API. Если Genius блокирует скрипт (403) — поставь `pip install cloudscraper`
или впиши текст вручную в album_info.json (поле "text" и "manual": true).
Если у альбома на Genius нет описания, останутся дата и число треков.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from html.parser import HTMLParser

import requests

API = 'https://api.genius.com'
# mzlff и его альтер эго «Илюха реп» (на Genius бывает кириллицей и латиницей)
ARTIST_RE = re.compile(r'mzlff|илюх\w*[\s-]*реп|ilyu\w*[\s-]*rep', re.I)
TIMEOUT = 20
PAUSE = 0.4
MAX_TEXT = 1000          # максимум символов в описании (по границе предложения)
SKIP_KEYS = {'secret'}   # у «вне дискографии» нет страницы на Genius

MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
          'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']

S = requests.Session()   # API (с токеном)
try:                      # необязательно: помогает, если сайт отдаёт 403
    import cloudscraper
    PAGE = cloudscraper.create_scraper()
except Exception:
    PAGE = requests.Session()
PAGE.headers.update({     # страницы genius.com — БЕЗ токена
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'ru,en;q=0.8',
})
VERBOSE = False


# ------------------------------------------------------------------ утилиты
def log(*a):
    if VERBOSE:
        print('   ', *a)


def get(url, **kw):
    kw.setdefault('timeout', TIMEOUT)
    time.sleep(PAUSE)
    r = S.get(url, **kw)
    if r.status_code == 429:
        log('429 — ждём 10 сек')
        time.sleep(10)
        r = S.get(url, **kw)
    r.raise_for_status()
    return r


def norm(x):
    return re.sub(r'[^\w]+', '', (x or '').lower())


def strip_parens(t):
    return re.sub(r'\s*[\(\[][^)\]]*[\)\]]', '', t or '').strip()


def clean_title(t):
    """Убирает (feat. ...) / [ft. ...] из названия."""
    return re.sub(
        r'\s*[\(\[]\s*(?:feat\.?|ft\.?|при уч\.?|совм\.? с)[^)\]]*[\)\]]\s*$',
        '', t or '', flags=re.I
    ).strip()


def title_variants(t):
    t = clean_title(t)
    return {v for v in (norm(t), norm(strip_parens(t))) if v}


EN_MONTHS = {m: i for i, m in enumerate(
    ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
     'september', 'october', 'november', 'december'], 1)}


def ru_display_date(text):
    """'November 10, 2022' -> '10 ноября 2022'; непонятное возвращает как есть."""
    m = re.match(r'^\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s*$', text or '')
    if m and m.group(1).lower() in EN_MONTHS:
        return f'{int(m.group(2))} {MONTHS[EN_MONTHS[m.group(1).lower()] - 1]} {m.group(3)}'
    return text or ''


def fmt_date(components, fallback=''):
    """{'year': 2024, 'month': 4, 'day': 12} -> '12 апреля 2024' (или '2024', 'апрель 2024')."""
    c = components or {}
    y, m, d = c.get('year'), c.get('month'), c.get('day')
    if y and m and d and 1 <= m <= 12:
        return f'{d} {MONTHS[m - 1]} {y}'
    if y and m and 1 <= m <= 12:
        return f'{MONTHS[m - 1]} {y}'
    if y:
        return str(y)
    return ru_display_date(fallback)


def dom_to_text(node):
    """Описание Genius в формате dom -> обычный текст (на случай, если plain не пришёл)."""
    if node is None:
        return ''
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return ''.join(dom_to_text(n) for n in node)
    if isinstance(node, dict):
        tag = node.get('tag')
        inner = dom_to_text(node.get('children'))
        if tag in ('p', 'br', 'li', 'h1', 'h2', 'h3', 'div'):
            return inner + '\n'
        return inner
    return ''


def description_text(obj):
    """Достаёт текст описания из поля description (plain / dom / строка)."""
    d = (obj or {}).get('description')
    if isinstance(d, str):
        text = d
    elif isinstance(d, dict):
        text = d.get('plain') or dom_to_text(d.get('dom')) or ''
    else:
        text = ''
    return text or (obj or {}).get('description_preview') or ''


def short_text(text, limit=MAX_TEXT):
    """Чистит описание (абзацы сохраняет) и обрезает по границе предложения."""
    paras = [re.sub(r'[ \t\r\f\v]+', ' ', p).strip() for p in (text or '').split('\n')]
    t = '\n'.join(p for p in paras if p)
    # на Genius пустое описание выглядит как «?»
    if len(re.sub(r'[\W_]+', '', t)) < 15:
        return ''
    # заглушки Genius вместо описания
    if re.match(r'^\s*(english\s+translations?|translations?|lyrics|tracklist)\s*[.:!]*\s*$', t, re.I):
        return ''
    if len(t) <= limit:
        return t
    cut = t[:limit]
    m = list(re.finditer(r'[.!?…](?=\s|$)', cut))
    if m and m[-1].end() >= limit * 0.5:
        return cut[:m[-1].end()].strip()
    return cut.rsplit(' ', 1)[0].rstrip(',;:—- ') + '…'


# ------------------------------------------------------------------ парсинг HTML
def unescape(s):
    return s.replace("\\'", "'").replace('\\"', '"').replace('\\\\', '\\')


def read_project(html):
    albums = {}
    for m in re.finditer(
        r"""^\s*['"]?(\w+)['"]?\s*:\s*\{\s*title\s*:\s*(['"])((?:\\.|(?!\2).)*)\2\s*,\s*url\s*:\s*(['"])([^'"]*)\4""",
        html, re.M | re.S
    ):
        albums[m.group(1)] = {'title': unescape(m.group(3)), 'url': m.group(5)}

    tracks = {}
    for m in re.finditer(
        r"""\{\s*track\s*:\s*(['"])((?:\\.|(?!\1).)*)\1\s*,\s*album\s*:\s*(['"])(\w+)\3""",
        html, re.S
    ):
        t = unescape(m.group(2))
        tracks.setdefault(m.group(4), [])
        if t not in tracks[m.group(4)]:
            tracks[m.group(4)].append(t)
    return albums, tracks


# ------------------------------------------------------------------ Genius API
_song_cache = {}


def genius_search(q):
    r = get(f'{API}/search', params={'q': q})
    return r.json().get('response', {}).get('hits', []) or []


def genius_song(song_id):
    if song_id not in _song_cache:
        r = get(f'{API}/songs/{song_id}', params={'text_format': 'plain'})
        _song_cache[song_id] = r.json().get('response', {}).get('song', {}) or {}
    return _song_cache[song_id]


def genius_album(album_id):
    r = get(f'{API}/albums/{album_id}', params={'text_format': 'plain'})
    return r.json().get('response', {}).get('album', {}) or {}


def genius_album_track_count(album_id):
    n, page = 0, 1
    while page:
        r = get(f'{API}/albums/{album_id}/tracks', params={'per_page': 50, 'page': page})
        resp = r.json().get('response', {})
        n += len([t for t in (resp.get('tracks') or []) if t.get('song')])
        page = resp.get('next_page')
    return n


# ------------------------------------------------------------------ текст со страницы Genius
def fetch_page(url):
    time.sleep(PAUSE)
    r = PAGE.get(url, timeout=TIMEOUT)
    if r.status_code in (403, 429, 503):
        raise RuntimeError(f'Genius не отдал страницу (HTTP {r.status_code}); '
                           'попробуй `pip install cloudscraper` или впиши текст вручную')
    r.raise_for_status()
    return r.text


def preloaded_state(html):
    """Достаёт JSON из window.__PRELOADED_STATE__ = JSON.parse('...')."""
    m = re.search(r"__PRELOADED_STATE__\s*=\s*JSON\.parse\('((?:\\.|[^'\\])*)'\)", html, re.S)
    if not m:
        return None
    raw = m.group(1).replace("\\'", "'")
    raw = re.sub(r'\\x([0-9a-fA-F]{2})', r'\\u00\1', raw)
    for attempt in (raw, re.sub(r'(?<!\\)"', r'\\"', raw)):
        try:
            return json.loads(json.loads('"' + attempt + '"'))
        except Exception:
            continue
    return None


def state_description(state, page_url=''):
    """Ищет описание нужной сущности (альбом/песня) в состоянии страницы."""
    ents = (state or {}).get('entities') or {}
    cands = [(kind, e) for kind in ('albums', 'songs')
             for e in (ents.get(kind) or {}).values() if isinstance(e, dict)]

    def nu(u):
        return (u or '').rstrip('/').lower()

    want = nu(page_url)
    ordered = [c for c in cands if want and nu(c[1].get('url')) == want]
    kind = 'albums' if '/albums/' in page_url else 'songs'
    ordered += [c for c in cands if c[0] == kind]
    for _, e in ordered:
        text = description_text(e)
        if short_text(text):
            return text
    return ''


class RichTextGrabber(HTMLParser):
    """Запасной вариант: собирает текст блоков RichText__Container / rich_text_formatting."""
    VOID = {'br', 'img', 'hr', 'input', 'meta', 'link', 'source', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self._buf, self._depth = [], None, 0

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get('class') or ''
        if self._buf is None:
            if 'RichText__Container' in cls or 'rich_text_formatting' in cls:
                self._buf, self._depth = [], 1
            return
        if tag in ('br', 'p', 'li'):
            self._buf.append('\n')
        if tag not in self.VOID:
            self._depth += 1

    def handle_endtag(self, tag):
        if self._buf is None or tag in self.VOID:
            return
        self._depth -= 1
        if tag == 'p':
            self._buf.append('\n')
        if self._depth <= 0:
            self.blocks.append(''.join(self._buf))
            self._buf = None

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)


def html_description(html):
    g = RichTextGrabber()
    try:
        g.feed(html)
    except Exception:
        pass
    for b in g.blocks:
        if short_text(b):
            return b
    return ''


def page_description(url, html=None):
    """Текст «About» со страницы Genius (пустая строка, если описания нет)."""
    html = html or fetch_page(url)
    state = preloaded_state(html)
    text = state_description(state, url) if state else ''
    if not short_text(text):
        log('в JSON страницы описания нет — пробую HTML')
        text = html_description(html)
    return text


def hit_is_mzlff(r):
    blob = ' '.join([
        (r.get('primary_artist') or {}).get('name', '') or '',
        r.get('artist_names', '') or '',
        r.get('full_title', '') or '',
    ])
    return bool(ARTIST_RE.search(blob))


def find_song(raw_title):
    """Ищем песню по точному названию (с подзаголовком в скобках и без)."""
    clean = clean_title(raw_title)
    want = title_variants(clean)
    # если песня числится за «Илюха реп», запрос с «mzlff» её может не вернуть — пробуем и так
    for q in (f'mzlff {clean}', f'илюха реп {clean}', clean):
        try:
            hits = genius_search(q)
        except Exception as e:
            log(f'ошибка поиска "{q}": {e}')
            continue
        for h in hits:
            r = h.get('result', {})
            if not hit_is_mzlff(r):
                continue
            if not (want & title_variants(r.get('title', ''))):
                continue
            try:
                return genius_song(r['id'])
            except Exception as e:
                log(f'ошибка songs/{r.get("id")}: {e}')
        log(f'по запросу "{q}" подходящей песни нет')
    return None


def find_album_id(album_title, titles):
    """Альбом ищем через песни: песня -> song.album.id."""
    want = norm(album_title)
    want_np = norm(strip_parens(album_title))

    def ok(name):
        g, g_np = norm(name), norm(strip_parens(name))
        return bool(g) and (want in g or g in want or
                            (want_np and (want_np in g_np or g_np in want_np)))

    for t in titles[:3]:
        al = (find_song(t) or {}).get('album') or {}
        if al.get('id') and ok(al.get('name', '')):
            log(f'альбом найден через трек "{t}": {al.get("name")}')
            return al['id']

    try:
        hits = genius_search(f'mzlff {album_title}')
    except Exception as e:
        log(f'ошибка поиска альбома: {e}')
        return None
    checked = 0
    for h in hits:
        r = h.get('result', {})
        if not hit_is_mzlff(r):
            continue
        checked += 1
        if checked > 4:
            break
        try:
            al = genius_song(r['id']).get('album') or {}
        except Exception:
            continue
        if al.get('id') and ok(al.get('name', '')):
            return al['id']
    return None


# ------------------------------------------------------------------ сбор справки
def info_for_album(album_title, titles):
    album_id = find_album_id(album_title, titles)
    if not album_id:
        return {}
    al = genius_album(album_id)
    out = {
        'date': fmt_date(al.get('release_date_components'),
                         al.get('release_date_for_display') or ''),
        'tracks': 0,
        'text': short_text(description_text(al)),
        'url': al.get('url') or '',
    }
    try:
        out['tracks'] = genius_album_track_count(album_id)
    except Exception as e:
        log(f'ошибка albums/{album_id}/tracks: {e}')
    return out


def info_for_single(raw_title):
    song = find_song(raw_title)
    if not song:
        return {}
    return {
        'date': fmt_date(song.get('release_date_components'),
                         song.get('release_date_for_display') or ''),
        'tracks': 0,  # у сингла число треков не показываем
        'text': short_text(description_text(song)),
        'url': song.get('url') or '',
    }


def add_page_text(info, refresh):
    """Дописывает text со страницы Genius (если ещё нет или просили --refresh)."""
    if info.get('text') and not refresh:
        return
    url = info.get('url')
    if not url:
        return
    try:
        text = short_text(page_description(url), MAX_TEXT)
    except Exception as e:
        print(f'   страница Genius не открылась: {e}')
        return
    if text:
        info['text'] = text
    else:
        print('   на странице Genius описания нет')


def collect(albums, tracks, existing, only=None, refresh=False):
    result = dict(existing)
    todo = [k for k in albums if k not in SKIP_KEYS and (not only or k in only)]
    for n, key in enumerate(todo, 1):
        album = albums[key]
        titles = tracks.get(key, [])
        label = album['title'] or (titles[0] if titles else 'без названия')
        print(f'\n[{n}/{len(todo)}] == {key}  ({label})')

        old = existing.get(key) or {}
        if old.get('manual'):
            print('   пропускаю (manual)')
            continue
        if old.get('text') and not refresh:
            print('   уже есть (используй --refresh, чтобы обновить)')
            continue

        if old.get('url') and not refresh:
            info = dict(old)          # страницу уже знаем — API не нужен
        else:
            is_single = key.startswith('singl_') or key.startswith('fit_')
            try:
                if is_single or not album['title'].strip():
                    info = {}
                    for raw in titles[:2]:
                        info = info_for_single(raw)
                        if info:
                            break
                else:
                    info = info_for_album(album['title'], titles)
            except Exception as e:
                print(f'   ошибка: {e}')
                continue
            if info and old.get('text'):
                info.setdefault('text', old['text'])   # --refresh не должен терять найденное

        if not info:
            print('   ничего не нашёл')
            continue

        add_page_text(info, refresh)

        if not (info.get('date') or info.get('text') or info.get('tracks')):
            print('   ничего не нашёл')
            continue

        # пустые поля не пишем — страница просто их не покажет
        result[key] = {k: v for k, v in info.items() if v}
        print('   ' + ', '.join(f'{k}: {str(v)[:50]!r}' for k, v in result[key].items()))
    return result


# ------------------------------------------------------------------ вставка в HTML
def patch_html(html, info):
    block = ('/*ALBUMINFO:START*/\n'
             'const ALBUM_INFO = ' + json.dumps(info, ensure_ascii=False, indent=4) + ';\n'
             '/*ALBUMINFO:END*/')
    new, n = re.subn(r'/\*ALBUMINFO:START\*/.*?/\*ALBUMINFO:END\*/',
                     lambda m: block, html, flags=re.S)
    if n != 1:
        sys.exit('ERROR: не нашёл маркеры /*ALBUMINFO:START*/ ... /*ALBUMINFO:END*/ в HTML')
    return new


# ------------------------------------------------------------------ main
def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument('html')
    ap.add_argument('--only', help='ключи альбомов через запятую')
    ap.add_argument('--refresh', action='store_true', help='перезаписать уже найденное')
    ap.add_argument('--page', help='проверить вытаскивание текста с одной страницы Genius и выйти')
    ap.add_argument('--song', help='показать, что Genius находит по названию трека, и выйти')
    ap.add_argument('--debug', action='store_true', help='показать парсинг HTML и выйти')
    ap.add_argument('--verbose', action='store_true', help='подробный лог')
    a = ap.parse_args()
    VERBOSE = a.verbose

    if a.page:
        try:
            html = fetch_page(a.page)
        except Exception as e:
            sys.exit(f'ERROR: {e}')
        Path('page_debug.html').write_text(html, 'utf-8')
        text = short_text(page_description(a.page, html), MAX_TEXT)
        print(f'Страница сохранена в page_debug.html ({len(html) // 1024} KB)')
        print('--- текст ---' if text else 'Описание не найдено — пришли page_debug.html, подстрою селекторы')
        print(text)
        return

    html_path = Path(a.html)
    if not html_path.exists():
        sys.exit(f'ERROR: файл не найден: {a.html}')
    html = html_path.read_text('utf-8')
    albums, tracks = read_project(html)

    if a.debug:
        print(f'Альбомов: {len(albums)}')
        for i, (key, al) in enumerate(albums.items(), 1):
            print(f'[{i}] {key!r}  title={al["title"]!r}  треков в цитатах: {len(tracks.get(key, []))}')
        return

    if not albums:
        sys.exit('ERROR: не нашёл ни одного альбома в HTML — запусти с --debug')

    token = os.environ.get('GENIUS_TOKEN', '').strip()
    if not token:
        sys.exit('ERROR: не задан GENIUS_TOKEN (export GENIUS_TOKEN=...)')
    S.headers.update({
        'Authorization': 'Bearer ' + token,
        'User-Agent': 'Mozilla/5.0 (mzlff-quotes/album-info)',
    })

    if a.song:
        want = title_variants(a.song)
        for q in (f'mzlff {a.song}', f'илюха реп {a.song}', a.song):
            print(f'\nзапрос: {q}')
            try:
                for h in genius_search(q):
                    r = h.get('result', {})
                    ok = hit_is_mzlff(r) and bool(want & title_variants(r.get('title', '')))
                    print(f'  {"✓" if ok else "·"} {r.get("full_title")}  [{r.get("artist_names")}]')
            except Exception as e:
                print(f'  ошибка: {e}')
        return

    try:
        hits = genius_search('mzlff')
        print(f'Genius OK, хитов по "mzlff": {len(hits)}')
    except requests.HTTPError as e:
        sys.exit(f'ERROR: Genius вернул {e.response.status_code} — проверь токен')
    except Exception as e:
        sys.exit(f'ERROR: сеть недоступна: {e}')

    json_path = Path('album_info.json')
    existing = {}
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text('utf-8'))
        except Exception:
            print('album_info.json повреждён — начинаю с нуля')

    only = set(a.only.split(',')) if a.only else None
    info = collect(albums, tracks, existing, only, a.refresh)

    json_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), 'utf-8')
    html_path.write_text(patch_html(html, info), 'utf-8')

    print('\n✅ ГОТОВО')
    print(f'   справок: {len(info)}')
    print('   отчёт: album_info.json')


if __name__ == '__main__':
    main()
