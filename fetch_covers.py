#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_covers.py — собирает обложки и фиты для mzlff_quotes.html через Genius API

Токен (НЕ хардкодь в файл):
    export GENIUS_TOKEN='...'   (Linux/Mac)
    set GENIUS_TOKEN=...        (cmd)
    $env:GENIUS_TOKEN='...'     (PowerShell)

Использование:
    python fetch_covers.py mzlff_quotes.html --download
    python fetch_covers.py mzlff_quotes.html --only ADHD,PHANTASMAGORIA --download
    python fetch_covers.py mzlff_quotes.html --debug
    python fetch_covers.py mzlff_quotes.html --verbose
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

GENIUS_TOKEN = '5rCo1yZ4zxNrBpkH049dnE83aKux0vdSnD3qW0w0809rsQm9pE1nEX27sKqoE9y1'
if not GENIUS_TOKEN:
    sys.exit('ERROR: не задан GENIUS_TOKEN (export GENIUS_TOKEN=...)')

API = 'https://api.genius.com'
HEADERS = {
    'Authorization': 'Bearer ' + GENIUS_TOKEN,
    'User-Agent': 'Mozilla/5.0 (mzlff-quotes/5.0)',
}

ARTIST_RE = re.compile(r'mzlff', re.I)
TIMEOUT = 20
PAUSE = 0.4

S = requests.Session()
S.headers.update(HEADERS)

COVERS_DIR = Path('covers')
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


def norm(x: str) -> str:
    return re.sub(r'[^\w]+', '', (x or '').lower())


def strip_parens(t: str) -> str:
    return re.sub(r'\s*[\(\[][^)\]]*[\)\]]', '', t or '').strip()


def clean_title(t: str) -> str:
    """Убирает (feat. ...) / [ft. ...] из названия."""
    return re.sub(
        r'\s*[\(\[]\s*(?:feat\.?|ft\.?|при уч\.?|совм\.? с)[^)\]]*[\)\]]\s*$',
        '', t or '', flags=re.I
    ).strip()


def title_variants(t: str) -> set:
    """Варианты нормализованного названия: с подзаголовком в скобках и без."""
    t = clean_title(t)
    return {v for v in (norm(t), norm(strip_parens(t))) if v}


def upscale(url: str) -> str:
    """Genius-картинки: ...abc.300x300x1.jpg -> ...abc.1000x1000x1.jpg"""
    if not url:
        return ''
    return re.sub(r'\.\d+x\d+x\d+\.', '.1000x1000x1.', url)


def is_placeholder(url: str) -> bool:
    return (not url) or 'default_cover_image' in url


def split_artists(names):
    if not names:
        return '', []
    others = [n for n in names if not ARTIST_RE.search(n)]
    if ARTIST_RE.search(names[0]):
        return '', others
    return (others[0] if others else ''), others[1:]


def song_artists(song: dict):
    names = []
    for a in song.get('primary_artists', []) or []:
        if a.get('name') and a['name'] not in names:
            names.append(a['name'])
    for a in song.get('featured_artists', []) or []:
        if a.get('name') and a['name'] not in names:
            names.append(a['name'])
    if not names:
        pa = (song.get('primary_artist') or {}).get('name')
        if pa:
            names.append(pa)
    return names


def song_cover(song: dict) -> str:
    # у песен поле называется song_art_image_url (cover_art_url — только у альбомов)
    url = song.get('song_art_image_url') or song.get('header_image_url') or ''
    return '' if is_placeholder(url) else upscale(url)


def album_cover(album: dict) -> str:
    url = album.get('cover_art_url') or ''
    return '' if is_placeholder(url) else upscale(url)


# ------------------------------------------------------------------ парсинг HTML
def unescape(s: str) -> str:
    return s.replace("\\'", "'").replace('\\"', '"').replace('\\\\', '\\')


def read_project(html: str):
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


def genius_search(q: str):
    """ВАЖНО: /search у Genius возвращает только ПЕСНИ, альбомов там нет."""
    r = get(f'{API}/search', params={'q': q})
    return r.json().get('response', {}).get('hits', []) or []


def genius_song(song_id: int):
    if song_id not in _song_cache:
        r = get(f'{API}/songs/{song_id}', params={'text_format': 'plain'})
        _song_cache[song_id] = r.json().get('response', {}).get('song', {}) or {}
    return _song_cache[song_id]


def genius_album(album_id: int):
    r = get(f'{API}/albums/{album_id}')
    return r.json().get('response', {}).get('album', {}) or {}


def genius_album_tracks(album_id: int):
    """/albums/{id} не отдаёт песни — их надо брать отдельным эндпоинтом."""
    songs, page = [], 1
    while page:
        r = get(f'{API}/albums/{album_id}/tracks',
                params={'per_page': 50, 'page': page})
        resp = r.json().get('response', {})
        for t in resp.get('tracks', []) or []:
            if t.get('song'):
                songs.append(t['song'])
        page = resp.get('next_page')
    return songs


def hit_is_mzlff(r: dict) -> bool:
    blob = ' '.join([
        (r.get('primary_artist') or {}).get('name', '') or '',
        r.get('artist_names', '') or '',
        r.get('full_title', '') or '',
    ])
    return bool(ARTIST_RE.search(blob))


# ------------------------------------------------------------------ логика поиска
def find_song(raw_title: str):
    """Ищем песню по ТОЧНОМУ названию (с учётом/без подзаголовка в скобках)."""
    clean = clean_title(raw_title)
    want = title_variants(clean)
    try:
        hits = genius_search(f'mzlff {clean}')
    except Exception as e:
        log(f'ошибка поиска "{clean}": {e}')
        return None
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
    return None


def find_album_id(album_title: str, titles):
    """Альбом ищем через песни: песня -> song.album.id."""
    want = norm(album_title)
    want_np = norm(strip_parens(album_title))

    def ok(name):
        g = norm(name)
        g_np = norm(strip_parens(name))
        return bool(g) and (want in g or g in want or
                            (want_np and (want_np in g_np or g_np in want_np)))

    # 1) по трекам из HTML
    for t in titles[:3]:
        song = find_song(t)
        al = (song or {}).get('album') or {}
        if al.get('id') and ok(al.get('name', '')):
            log(f'альбом найден через трек "{t}": {al.get("name")}')
            return al['id']

    # 2) по названию альбома — берём первые mzlff-песни
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


def search_album(album_title: str, titles):
    album_id = find_album_id(album_title, titles)
    if not album_id:
        return {}
    try:
        al = genius_album(album_id)
        songs = genius_album_tracks(album_id)
    except Exception as e:
        log(f'ошибка albums/{album_id}: {e}')
        return {}

    out = {'cover': album_cover(al), 'tracks': {}}
    for s in songs:
        try:
            full = genius_song(s['id'])   # только тут есть featured_artists
        except Exception:
            full = s
        names = song_artists(full)
        if names:
            out['tracks'][clean_title(s.get('title', ''))] = names
    return out


def search_song_for_single(raw_title: str):
    song = find_song(raw_title)
    if not song:
        return {}
    cover = song_cover(song)
    if not cover:
        al = song.get('album') or {}
        if al.get('id') and norm(clean_title(raw_title)) in norm(al.get('name', '')):
            try:
                cover = album_cover(genius_album(al['id']))
            except Exception as e:
                log(f'ошибка albums/{al["id"]}: {e}')
    out = {'cover': cover, 'tracks': {}}
    names = song_artists(song)
    if names:
        out['tracks'][raw_title] = names
    return out


def src_genius(key, album, titles):
    album_title = (album.get('title') or '').strip()
    is_single = key.startswith('singl_') or key.startswith('fit_')

    if is_single or not album_title:
        for raw in titles[:2]:
            res = search_song_for_single(raw)
            if res:
                return res
        return {}
    return search_album(album_title, titles)


def map_to_html_title(genius_title, html_titles):
    """Сопоставляет название с Genius с названием из HTML (чтобы ключи совпали)."""
    gv = title_variants(genius_title)
    for ht in html_titles:
        if gv & title_variants(ht):
            return ht
    return genius_title


# ------------------------------------------------------------------ сборка
def collect(albums, tracks, only=None):
    covers, info = {}, {}
    total = len(albums)
    n = 0

    for key, album in albums.items():
        if only and key not in only:
            continue
        n += 1
        titles = tracks.get(key, [])
        label = album['title'] or (titles[0] if titles else 'без названия')
        print(f'\n[{n}/{total}] == {key}  ({label})')

        try:
            res = src_genius(key, album, titles) or {}
        except Exception as e:
            print(f'   genius   ошибка: {e}')
            continue

        if not res:
            print('   genius   ничего не нашёл')
            continue

        got = []
        if res.get('cover') and key not in covers:
            covers[key] = res['cover']
            got.append('обложка')

        is_fit = key.startswith('fit_')
        for t, names in (res.get('tracks') or {}).items():
            t = map_to_html_title(t, titles)
            k = f'{key}|{t}'
            if k in info or not names:
                continue
            main, feats = split_artists(names)
            if is_fit and not main and feats:
                main, feats = feats[0], feats[1:]
            if feats or (is_fit and main):
                info[k] = {'main': main if is_fit else '', 'feats': feats}
                got.append(f'фиты: {t}')

        print('   genius   ' + (', '.join(got) if got else 'ничего нового'))

    return covers, info


# ------------------------------------------------------------------ скачивание
def download_covers(covers: dict):
    COVERS_DIR.mkdir(exist_ok=True)
    out = {}
    for key, url in covers.items():
        if not url or url.startswith('covers/'):
            out[key] = url
            continue
        try:
            r = requests.get(url, timeout=TIMEOUT,
                             headers={'User-Agent': HEADERS['User-Agent']})
            if r.status_code == 404 and '.1000x1000x1.' in url:
                # нет 1000x1000 — пробуем оригинальный размер
                r = requests.get(url.replace('.1000x1000x1.', '.300x300x1.'),
                                 timeout=TIMEOUT,
                                 headers={'User-Agent': HEADERS['User-Agent']})
            r.raise_for_status()
            ctype = r.headers.get('content-type', '').lower()
            ext = 'png' if 'png' in ctype else 'webp' if 'webp' in ctype else 'jpg'
            path = COVERS_DIR / f'{key}.{ext}'
            path.write_bytes(r.content)
            out[key] = str(path).replace('\\', '/')
            print(f'   ✓ скачал {key} → {out[key]}  ({len(r.content)//1024} KB)')
        except Exception as e:
            print(f'   ✗ не скачалась {key}: {e}')
            out[key] = url
    return out


# ------------------------------------------------------------------ вставка в HTML
def patch_html(html: str, covers: dict, info: dict) -> str:
    block = (
        '/*DATA:START*/\n'
        'const COVERS = ' + json.dumps(covers, ensure_ascii=False, indent=4) + ';\n'
        'const TRACK_INFO = ' + json.dumps(info, ensure_ascii=False, indent=4) + ';\n'
        '/*DATA:END*/'
    )
    new, n = re.subn(r'/\*DATA:START\*/.*?/\*DATA:END\*/',
                     lambda m: block, html, flags=re.S)
    if n != 1:
        sys.exit('ERROR: не нашёл маркеры /*DATA:START*/ ... /*DATA:END*/ в HTML')
    return new


# ------------------------------------------------------------------ main
def main():
    global VERBOSE

    ap = argparse.ArgumentParser()
    ap.add_argument('html')
    ap.add_argument('--only', help='ключи альбомов через запятую')
    ap.add_argument('--download', action='store_true', help='скачать в covers/')
    ap.add_argument('--debug', action='store_true', help='показать парсинг HTML')
    ap.add_argument('--verbose', action='store_true', help='подробный лог')
    a = ap.parse_args()
    VERBOSE = a.verbose

    html_path = Path(a.html)
    if not html_path.exists():
        sys.exit(f'ERROR: файл не найден: {a.html}')
    html = html_path.read_text('utf-8')

    albums, tracks = read_project(html)

    if a.debug:
        print('=== ОТЛАДКА ===')
        print(f'Альбомов: {len(albums)}')
        for i, (key, al) in enumerate(albums.items(), 1):
            print(f'[{i}] ключ: "{key}"  title: "{al["title"]}"  '
                  f'треков: {len(tracks.get(key, []))}')
        sys.exit(0)

    if not albums:
        sys.exit('ERROR: не нашёл ни одного альбома в HTML — запусти с --debug '
                 'и покажи, как в файле выглядит объект альбома')

    try:
        hits = genius_search('mzlff')
        print(f'Genius OK, хитов по "mzlff": {len(hits)}')
    except requests.HTTPError as e:
        sys.exit(f'ERROR: Genius вернул {e.response.status_code} — проверь токен')
    except Exception as e:
        sys.exit(f'ERROR: сеть недоступна: {e}')

    print(f'Прочитано альбомов: {len(albums)}')

    only = set(a.only.split(',')) if a.only else None
    covers, info = collect(albums, tracks, only)

    if a.download:
        covers = download_covers(covers)

    Path('covers.json').write_text(
        json.dumps({'covers': covers, 'track_info': info},
                   ensure_ascii=False, indent=2), 'utf-8')
    html_path.write_text(patch_html(html, covers, info), 'utf-8')

    print('\n✅ ГОТОВО')
    print(f'   обложек:  {len(covers)} из {len(albums)}')
    print(f'   треков с фитами: {len(info)}')
    print('   отчёт: covers.json')


if __name__ == '__main__':
    main()
