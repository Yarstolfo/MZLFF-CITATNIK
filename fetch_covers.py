#!/usr/bin/env python3
"""
fetch_covers.py — собирает обложки и информацию о фитах для mzlff_quotes.html
из интернета и вшивает их в сам HTML (между маркерами /*DATA:START*/ ... /*DATA:END*/).

Установка:   pip install requests
Запуск:      python fetch_covers.py mzlff_quotes.html
             python fetch_covers.py mzlff_quotes.html --download      (скачать обложки в папку covers/)
             python fetch_covers.py mzlff_quotes.html --sources yandex,deezer,itunes

Источники (в порядке приоритета по умолчанию):
  yandex   — Яндекс Музыка (по ссылкам из albums, ключ не нужен)
  deezer   — Deezer API (ключ не нужен)
  itunes   — Apple iTunes Search API (ключ не нужен)
  genius   — Genius API         (нужен GENIUS_TOKEN — тот же, что у lyricsgenius)
  spotify  — Spotify Web API    (нужны SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET)
  og       — og:image со страницы по ссылке (VK Music, YouTube и т.п.)

Токены задаются переменными окружения (Windows PowerShell):
  $env:GENIUS_TOKEN="..."; $env:SPOTIFY_CLIENT_ID="..."; $env:SPOTIFY_CLIENT_SECRET="..."
"""
import argparse, json, os, re, sys, time
import requests

ARTIST_RE = re.compile(r'mzlff', re.I)
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) mzlff-quotes/1.0'
S = requests.Session()
S.headers['User-Agent'] = UA
TIMEOUT = 15
PAUSE = 0.35


def get(url, **kw):
    kw.setdefault('timeout', TIMEOUT)
    time.sleep(PAUSE)
    r = S.get(url, **kw)
    r.raise_for_status()
    return r


def norm(x):
    return re.sub(r'[^\w]+', '', x.lower())


def split_artists(names):
    """-> (main, feats) без самого mzlff. Если mzlff был первым — main пустой."""
    others = [n for n in names if not ARTIST_RE.search(n)]
    if names and ARTIST_RE.search(names[0]):
        return '', others
    return (others[0] if others else ''), others[1:]


# ------------------------------------------------------------------ парсинг HTML
def read_project(html):
    albums = {}
    for m in re.finditer(r"^\s*(\w+):\s*\{\s*title:\s*'((?:[^'\\]|\\.)*)',\s*url:\s*'([^']+)'", html, re.M):
        albums[m.group(1)] = {'title': m.group(2), 'url': m.group(3)}
    tracks = {}
    for m in re.finditer(r"\{ track: '((?:[^'\\]|\\.)*)', album: '(\w+)'", html):
        t = m.group(1).replace("\\'", "'")
        tracks.setdefault(m.group(2), [])
        if t not in tracks[m.group(2)]:
            tracks[m.group(2)].append(t)
    return albums, tracks


def clean_title(t):
    return re.sub(r'\s*[\(\[]\s*(?:feat\.?|ft\.?)[^)\]]*[\)\]]\s*$', '', t, flags=re.I).strip()


# ------------------------------------------------------------------ источники
# Каждая функция возвращает dict: {'cover': url|'', 'tracks': {название_трека: [артисты...]}}

YA_HEADERS = {'User-Agent': 'Yandex-Music-API', 'X-Yandex-Music-Client': 'YandexMusicAndroid/24023231'}


def ya_cover(uri):
    return 'https://' + uri.replace('%%', '1000x1000') if uri else ''


def src_yandex(key, album, titles, args):
    url = album['url']
    m = re.search(r'music\.yandex\.\w+/album/(\d+)(?:/track/(\d+))?', url)
    if not m:
        return {}
    aid, tid = m.groups()
    out = {'cover': '', 'tracks': {}}
    if tid:   # фит: ссылка на конкретный трек
        j = get(f'https://api.music.yandex.net/tracks/{tid}', headers=YA_HEADERS).json()['result'][0]
        out['cover'] = ya_cover(j.get('coverUri') or (j.get('albums') or [{}])[0].get('coverUri', ''))
        out['tracks'][titles[0] if titles else j['title']] = [a['name'] for a in j.get('artists', [])]
        return out
    j = get(f'https://api.music.yandex.net/albums/{aid}/with-tracks', headers=YA_HEADERS).json()['result']
    out['cover'] = ya_cover(j.get('coverUri'))
    by_title = {}
    for vol in j.get('volumes', []):
        for t in vol:
            by_title[norm(clean_title(t['title']))] = [a['name'] for a in t.get('artists', [])]
    for t in titles:
        names = by_title.get(norm(clean_title(t)))
        if names:
            out['tracks'][t] = names
    if not titles:   # одиночный релиз без явных названий
        pass
    return out


def src_deezer(key, album, titles, args):
    out = {'cover': '', 'tracks': {}}
    for t in titles[:3]:
        q = f'artist:"mzlff" track:"{clean_title(t)}"'
        data = get('https://api.deezer.com/search', params={'q': q}).json().get('data', [])
        if not data:
            continue
        hit = data[0]
        out['cover'] = out['cover'] or hit['album'].get('cover_xl', '')
        try:
            tr = get(f"https://api.deezer.com/track/{hit['id']}").json()
            names = [c['name'] for c in tr.get('contributors', [])]
            if names:
                out['tracks'][t] = names
        except Exception:
            pass
    return out


def src_itunes(key, album, titles, args):
    out = {'cover': '', 'tracks': {}}
    for t in titles[:3]:
        j = get('https://itunes.apple.com/search',
                params={'term': 'mzlff ' + clean_title(t), 'entity': 'song', 'limit': 8}).json()
        for r in j.get('results', []):
            if ARTIST_RE.search(r.get('artistName', '')):
                out['cover'] = re.sub(r'\d+x\d+bb', '1000x1000bb', r.get('artworkUrl100', ''))
                break
        if out['cover']:
            break
    return out


def src_genius(key, album, titles, args):
    token = os.environ.get('GENIUS_TOKEN')
    if not token:
        return {}
    H = {'Authorization': 'Bearer ' + token}
    out = {'cover': '', 'tracks': {}}
    for t in titles:
        hits = get('https://api.genius.com/search', params={'q': 'mzlff ' + clean_title(t)}, headers=H).json()['response']['hits']
        for h in hits:
            r = h['result']
            names = [r['primary_artist']['name']] + [a['name'] for a in r.get('featured_artists', [])]
            if any(ARTIST_RE.search(n) for n in names):
                song = get(f"https://api.genius.com/songs/{r['id']}", headers=H).json()['response']['song']
                names = [a['name'] for a in song.get('primary_artists', [])] + [a['name'] for a in song.get('featured_artists', [])]
                out['tracks'][t] = names
                out['cover'] = out['cover'] or (song.get('album') or {}).get('cover_art_url') or song.get('song_art_image_url', '')
                break
    return out


_spotify_token = {}


def spotify_headers():
    cid, sec = os.environ.get('SPOTIFY_CLIENT_ID'), os.environ.get('SPOTIFY_CLIENT_SECRET')
    if not (cid and sec):
        return None
    if not _spotify_token:
        r = requests.post('https://accounts.spotify.com/api/token', data={'grant_type': 'client_credentials'},
                          auth=(cid, sec), timeout=TIMEOUT)
        r.raise_for_status()
        _spotify_token['t'] = r.json()['access_token']
    return {'Authorization': 'Bearer ' + _spotify_token['t']}


def src_spotify(key, album, titles, args):
    H = spotify_headers()
    if not H:
        return {}
    out = {'cover': '', 'tracks': {}}
    for t in titles:
        items = get('https://api.spotify.com/v1/search', headers=H,
                    params={'q': f'track:{clean_title(t)} artist:mzlff', 'type': 'track', 'limit': 5}).json()['tracks']['items']
        for it in items:
            names = [a['name'] for a in it['artists']]
            if any(ARTIST_RE.search(n) for n in names):
                out['tracks'][t] = names
                imgs = it['album'].get('images') or []
                out['cover'] = out['cover'] or (imgs[0]['url'] if imgs else '')
                break
    return out


def src_og(key, album, titles, args):
    """og:image со страницы по ссылке: VK Music, YouTube, Yandex (если нет captcha)."""
    url = album['url']
    if 'youtube.com/watch' in url or 'youtu.be/' in url:
        vid = re.search(r'(?:v=|youtu\.be/)([\w-]{11})', url)
        return {'cover': f'https://img.youtube.com/vi/{vid.group(1)}/maxresdefault.jpg'} if vid else {}
    html = get(url).text
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I) \
        or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html, re.I)
    return {'cover': m.group(1).replace('&amp;', '&')} if m else {}


SOURCES = {'yandex': src_yandex, 'deezer': src_deezer, 'itunes': src_itunes,
           'genius': src_genius, 'spotify': src_spotify, 'og': src_og}


# ------------------------------------------------------------------ сборка
def collect(albums, tracks, order, only=None):
    covers, info = {}, {}
    for key, album in albums.items():
        if only and key not in only:
            continue
        titles = tracks.get(key, [])
        if not titles:
            continue
        is_fit = key.startswith('fit_')
        print(f'\n== {key}  ({album["title"] or titles[0]})')
        for name in order:
            fn = SOURCES[name]
            try:
                res = fn(key, album, titles, None)
            except Exception as e:
                print(f'   {name:8} ошибка: {e}')
                continue
            if not res:
                print(f'   {name:8} пропущен')
                continue
            got = []
            if res.get('cover') and key not in covers:
                covers[key] = res['cover']
                got.append('обложка')
            for t, names in (res.get('tracks') or {}).items():
                k = f'{key}|{t}'
                if k in info or not names:
                    continue
                main, feats = split_artists(names)
                if is_fit and not main and feats:     # в фите mzlff может стоять не первым
                    main, feats = feats[0], feats[1:]
                if feats or (is_fit and main):
                    info[k] = {'main': main if is_fit else '', 'feats': feats}
                    got.append('фиты: ' + t)
            print(f'   {name:8} ' + (', '.join(got) if got else 'ничего нового'))
    return covers, info


def download(covers, folder='covers'):
    os.makedirs(folder, exist_ok=True)
    out = {}
    for key, url in covers.items():
        try:
            r = get(url)
            ext = 'png' if 'png' in r.headers.get('content-type', '') else 'jpg'
            path = f'{folder}/{key}.{ext}'
            with open(path, 'wb') as f:
                f.write(r.content)
            out[key] = path
        except Exception as e:
            print(f'не скачалась {key}: {e}')
            out[key] = url
    return out


def patch_html(html, covers, info):
    block = ('/*DATA:START*/\n    const COVERS = ' + json.dumps(covers, ensure_ascii=False, indent=4).replace('\n', '\n    ') +
             ';\n    const TRACK_INFO = ' + json.dumps(info, ensure_ascii=False, indent=4).replace('\n', '\n    ') +
             ';\n    /*DATA:END*/')
    new, n = re.subn(r'/\*DATA:START\*/.*?/\*DATA:END\*/', lambda m: block, html, flags=re.S)
    if n != 1:
        sys.exit('Маркеры /*DATA:START*/ ... /*DATA:END*/ не найдены в HTML')
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('html')
    ap.add_argument('--sources', default='yandex,deezer,itunes,genius,spotify,og')
    ap.add_argument('--only', help='ключи альбомов через запятую (по умолчанию все)')
    ap.add_argument('--download', action='store_true', help='скачать обложки в covers/')
    a = ap.parse_args()
    order = [x.strip() for x in a.sources.split(',') if x.strip()]
    bad = [x for x in order if x not in SOURCES]
    if bad:
        sys.exit('Неизвестные источники: ' + ', '.join(bad))

    html = open(a.html, encoding='utf-8').read()
    albums, tracks = read_project(html)
    covers, info = collect(albums, tracks, order, set(a.only.split(',')) if a.only else None)
    if a.download:
        covers = download(covers)
    json.dump({'covers': covers, 'track_info': info}, open('covers.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    open(a.html, 'w', encoding='utf-8').write(patch_html(html, covers, info))
    print(f'\nГотово: обложек {len(covers)} из {len(albums)}, треков с фитами {len(info)}. HTML обновлён, covers.json сохранён.')


if __name__ == '__main__':
    main()
