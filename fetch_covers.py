#!/usr/bin/env python3
"""
fetch_covers.py v2.0 — собирает АЛЬБОМНЫЕ обложки и информацию о фитах
Установка:   pip install requests
Запуск:      python fetch_covers.py mzlff_quotes.html
python fetch_covers.py mzlff_quotes.html --download      (скачать обложки в папку covers/)
python fetch_covers.py mzlff_quotes.html --sources yandex,deezer,itunes
"""
import argparse, json, os, re, sys, time
import requests

ARTIST_RE = re.compile(r'mzlff', re.I)
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) mzlff-quotes/2.0'
S = requests.Session()
S.headers['User-Agent'] = UA
TIMEOUT = 15
PAUSE = 0.5  # Увеличил паузу чтобы не блокировали

def get(url, **kw):
    kw.setdefault('timeout', TIMEOUT)
    time.sleep(PAUSE)
    r = S.get(url, **kw)
    r.raise_for_status()
    return r

def norm(x):
    return re.sub(r'[^\w]+', '', x.lower())

def clean_title(t):
    """Очистить название от feat/ft"""
    return re.sub(r'\s*[\(\[]\s*(?:feat\.?|ft\.?)[^)\]]*[\)\]]\s*$', '', t, flags=re.I).strip()

def split_artists(names):
    """Разделить артистов на main и feats, убрать mzlff"""
    others = [n for n in names if not ARTIST_RE.search(n)]
    if names and ARTIST_RE.search(names[0]):
        return '', others
    return (others[0] if others else ''), others[1:]

# ------------------------------------------------------------------ парсинг HTML
def read_project(html):
    """Парсит альбомы и треки из HTML"""
    albums = {}
    # Ищем альбомы: key: {title: '...', url: '...'}
    for m in re.finditer(r"^\s*(\w+):\s*\{\s*title:\s*'((?:[^'\\]|\\.)*)',\s*url:\s*'([^']+)'", html, re.M):
        albums[m.group(1)] = {'title': m.group(2), 'url': m.group(3)}
    
    tracks = {}
    # Ищем треки: { track: '...', album: '...' }
    for m in re.finditer(r"\{ track: '((?:[^'\\]|\\.)*)', album: '(\w+)'", html):
        t = m.group(1).replace("\\'", "'")
        tracks.setdefault(m.group(2), [])
        if t not in tracks[m.group(2)]:
            tracks[m.group(2)].append(t)
    
    return albums, tracks

# ------------------------------------------------------------------ источники (ОБНОВЛЕНО для альбомных обложек)

YA_HEADERS = {'User-Agent': 'Yandex-Music-API', 'X-Yandex-Music-Client': 'YandexMusicAndroid/24023231'}

def ya_cover(uri):
    """Конвертирует URI обложки Яндекс в URL"""
    if not uri:
        return ''
    # %% заменяется на размер, например 1000x1000
    return 'https://' + uri.replace('%%', '1000x1000')

def src_yandex(key, album, titles, args):
    """
    ЯНДЕКС МУЗЫКА — ищем АЛЬБОМНУЮ обложку и информацию о фитах из треков
    """
    url = album['url']
    m = re.search(r'music\.yandex\.\w+/album/(\d+)', url)
    if not m:
        print(f"   yandex   не найден ID альбома в URL: {url}")
        return {}
    
    aid = m.group(1)
    out = {'cover': '', 'tracks': {}}
    
    try:
        # Получаем информацию об АЛЬБОМЕ (не о треке!)
        j = get(f'https://api.music.yandex.net/albums/{aid}/with-tracks', headers=YA_HEADERS).json()
        if 'result' not in j:
            print(f"   yandex   ошибка API: {j}")
            return {}
        
        album_data = j['result']
        
        # === АЛЬБОМНАЯ ОБЛОЖКА ===
        out['cover'] = ya_cover(album_data.get('coverUri'))
        
        # === ИНФОРМАЦИЯ О ФИТАХ из треков альбома ===
        for vol in album_data.get('volumes', []):
            for track in vol:
                track_title = clean_title(track['title'])
                artists = [a['name'] for a in track.get('artists', [])]
                
                # Сохраняем информацию о всех участниках трека
                if artists:
                    out['tracks'][track_title] = artists
                    
    except Exception as e:
        print(f"   yandex   ошибка: {e}")
    
    return out

def src_deezer(key, album, titles, args):
    """
    DEEZER — ищем АЛЬБОМ по названию и исполнителю
    """
    album_title = album.get('title', '')
    if not album_title:
        return {}
    
    out = {'cover': '', 'tracks': {}}
    
    try:
        # Ищем АЛЬБОМЫ (не треки!)
        q = f'artist:"mzlff" album:"{album_title}"'
        data = get('https://api.deezer.com/search/album', params={'q': q}).json().get('data', [])
        
        if not data:
            # Пробуем поиск без указания альбома
            q = f'artist:"mzlff"'
            data = get('https://api.deezer.com/search/album', params={'q': q}).json().get('data', [])
        
        if data:
            album_info = data[0]
            out['cover'] = album_info.get('cover_xl', '')
            
            # Получаем треки альбома с информацией о фитах
            try:
                album_tracks = get(f"https://api.deezer.com/album/{album_info['id']}").json()
                for track in album_tracks.get('tracks', {}).get('data', []):
                    contributors = [c['name'] for c in track.get('contributors', [])]
                    if contributors:
                        out['tracks'][clean_title(track['title'])] = contributors
            except:
                pass
                
    except Exception as e:
        print(f"   deezer   ошибка: {e}")
    
    return out

def src_itunes(key, album, titles, args):
    """
    ITUNES — ищем АЛЬБОМ (не песни)
    """
    album_title = album.get('title', '')
    if not album_title:
        return {}
    
    out = {'cover': '', 'tracks': {}}
    
    try:
        # Ищем АЛЬБОМЫ (entity=album вместо song)
        j = get('https://itunes.apple.com/search',
                params={'term': f'mzlff {album_title}', 'entity': 'album', 'limit': 5}).json()
        
        for r in j.get('results', []):
            if ARTIST_RE.search(r.get('artistName', '')):
                # Берём обложку альбома максимального качества
                out['cover'] = re.sub(r'\d+x\d+bb', '1000x1000bb', r.get('artworkUrl100', ''))
                
                # Получаем треки альбома
                collection_id = r.get('collectionId')
                if collection_id:
                    try:
                        tracks_data = get('https://itunes.apple.com/lookup',
                                        params={'id': collection_id, 'entity': 'song'}).json()
                        for t in tracks_data.get('results', []):
                            if t.get('wrapperType') == 'track':
                                artists = [t.get('artistName', '')]
                                if artists[0]:
                                    out['tracks'][clean_title(t.get('trackName', ''))] = artists
                    except:
                        pass
                break
                
    except Exception as e:
        print(f"   itunes   ошибка: {e}")
    
    return out

def src_genius(key, album, titles, args):
    """
    GENIUS — информация о фитах из API
    """
    token = os.environ.get('GENIUS_TOKEN')
    if not token:
        print("   genius   пропущен (нет GENIUS_TOKEN)")
        return {}
    
    H = {'Authorization': 'Bearer ' + token}
    out = {'cover': '', 'tracks': {}}
    
    album_title = album.get('title', '')
    
    try:
        # Ищем альбом
        hits = get('https://api.genius.com/search', 
                   params={'q': f'mzlff {album_title}'}, headers=H).json()['response']['hits']
        
        for h in hits[:3]:  # Проверяем первые 3 результата
            r = h['result']
            # Проверяем что это альбом и от mzlff
            if r.get('type') == 'album' or 'album' in r.get('url', ''):
                # Получаем полную информацию об альбоме
                album_data = get(f"https://api.genius.com/albums/{r['id']}", headers=H).json()['response']['album']
                
                # Обложка альбома
                out['cover'] = album_data.get('cover_art_url', '')
                
                # Треки альбома с фитами
                for song in album_data.get('songs', []):
                    names = [a['name'] for a in song.get('primary_artists', [])]
                    names += [a['name'] for a in song.get('featured_artists', [])]
                    if names:
                        out['tracks'][clean_title(song['title'])] = names
                break
        
        # Если не нашли как альбом, ищем по трекам
        if not out['tracks']:
            for t in titles[:5]:
                hits = get('https://api.genius.com/search', 
                          params={'q': f'mzlff {clean_title(t)}'}, headers=H).json()['response']['hits']
                
                for h in hits[:2]:
                    r = h['result']
                    names = [r['primary_artist']['name']] + [a['name'] for a in r.get('featured_artists', [])]
                    if any(ARTIST_RE.search(n) for n in names):
                        song = get(f"https://api.genius.com/songs/{r['id']}", headers=H).json()['response']['song']
                        names = [a['name'] for a in song.get('primary_artists', [])] + \
                                [a['name'] for a in song.get('featured_artists', [])]
                        out['tracks'][clean_title(t)] = names
                        out['cover'] = out['cover'] or song.get('song_art_image_url', '')
                        break
                        
    except Exception as e:
        print(f"   genius   ошибка: {e}")
    
    return out

_spotify_token = {}

def spotify_headers():
    """Получить токен Spotify"""
    cid, sec = os.environ.get('SPOTIFY_CLIENT_ID'), os.environ.get('SPOTIFY_CLIENT_SECRET')
    if not (cid and sec):
        return None
    
    if not _spotify_token:
        r = requests.post('https://accounts.spotify.com/api/token', 
                         data={'grant_type': 'client_credentials'},
                         auth=(cid, sec), timeout=TIMEOUT)
        r.raise_for_status()
        _spotify_token['t'] = r.json()['access_token']
    
    return {'Authorization': 'Bearer ' + _spotify_token['t']}

def src_spotify(key, album, titles, args):
    """
    SPOTIFY — ищем АЛЬБОМ и его треки
    """
    H = spotify_headers()
    if not H:
        print("   spotify  пропущен (нет SPOTIFY_CLIENT_ID/SECRET)")
        return {}
    
    album_title = album.get('title', '')
    out = {'cover': '', 'tracks': {}}
    
    try:
        # Ищем АЛЬБОМЫ (type=album)
        search_q = f'album:"{album_title}" artist:mzlff' if album_title else 'artist:mzlff'
        items = get('https://api.spotify.com/v1/search', headers=H,
                   params={'q': search_q, 'type': 'album', 'limit': 5}).json()['albums']['items']
        
        if items:
            album_data = items[0]
            # Обложка альбома (первая картинка — самая большая)
            imgs = album_data.get('images') or []
            out['cover'] = imgs[0]['url'] if imgs else ''
            
            # Получаем треки альбома
            album_id = album_data['id']
            tracks_data = get(f'https://api.spotify.com/v1/albums/{album_id}/tracks', 
                             headers=H).json()
            
            for track in tracks_data.get('items', []):
                names = [a['name'] for a in track.get('artists', [])]
                if names:
                    out['tracks'][clean_title(track['name'])] = names
                    
    except Exception as e:
        print(f"   spotify  ошибка: {e}")
    
    return out

def src_og(key, album, titles, args):
    """
    OG IMAGE — берём обложку из meta-тегов страницы
    """
    url = album['url']
    
    # YouTube
    if 'youtube.com/watch' in url or 'youtu.be/' in url:
        vid = re.search(r'(?:v=|youtu\.be/)([\w-]{11})', url)
        if vid:
            return {'cover': f'https://img.youtube.com/vi/{vid.group(1)}/maxresdefault.jpg'}
    
    # Остальные сайты (VK, Yandex и т.п.)
    try:
        html = get(url).text
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I) or
             re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html, re.I))
        if m:
            return {'cover': m.group(1).replace('&amp;', '&')}
    except:
        pass
    
    return {}

SOURCES = {
    'yandex': src_yandex,
    'deezer': src_deezer,
    'itunes': src_itunes,
    'genius': src_genius,
    'spotify': src_spotify,
    'og': src_og
}

# ------------------------------------------------------------------ сборка
def collect(albums, tracks, order, only=None):
    """Собирает обложки и информацию о фитах"""
    covers, info = {}, {}
    
    for key, album in albums.items():
        if only and key not in only:
            continue
        
        titles = tracks.get(key, [])
        is_fit = key.startswith('fit_')
        
        print(f'\n== {key}  ({album["title"] or (titles[0] if titles else "без названия")})')
        
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
            
            # Сохраняем обложку (только первую найденную)
            if res.get('cover') and key not in covers:
                covers[key] = res['cover']
                got.append('обложка')
            
            # Сохраняем информацию о фитах
            for t, names in (res.get('tracks') or {}).items():
                k = f'{key}|{t}'
                if k in info or not names:
                    continue
                
                main, feats = split_artists(names)
                
                # Для фитов: если mzlff не первый, корректируем
                if is_fit and not main and feats:
                    main, feats = feats[0], feats[1:]
                
                # Сохраняем только если есть фиты
                if feats or (is_fit and main):
                    info[k] = {'main': main if is_fit else '', 'feats': feats}
                    got.append(f'фиты: {t}')
            
            print(f'   {name:8} ' + (', '.join(got) if got else 'ничего нового'))
    
    return covers, info

def download(covers, folder='covers'):
    """Скачивает обложки локально"""
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
            print(f'✓ скачал {key}')
        except Exception as e:
            print(f'✗ не скачалась {key}: {e}')
            out[key] = url
    
    return out

def patch_html(html, covers, info):
    """Вставляет данные в HTML между маркерами"""
    block = ('/*DATA:START*/\n'
             'const COVERS = ' + json.dumps(covers, ensure_ascii=False, indent=4) + ';\n'
             'const TRACK_INFO = ' + json.dumps(info, ensure_ascii=False, indent=4) + ';\n'
             '/*DATA:END*/')
    
    new, n = re.subn(r'/\*DATA:START\*/.*?/\*DATA:END\*/', lambda m: block, html, flags=re.S)
    
    if n != 1:
        sys.exit('ERROR: Маркеры /*DATA:START*/ ... /*DATA:END*/ не найдены в HTML')
    
    return new

def main():
    ap = argparse.ArgumentParser(description='Сбор альбомных обложек и информации о фитах')
    ap.add_argument('html', help='HTML файл для обработки')
    ap.add_argument('--sources', default='yandex,deezer,spotify,itunes,genius,og',
                   help='Источники через запятую (по умолчанию: yandex,deezer,spotify,itunes,genius,og)')
    ap.add_argument('--only', help='Обработать только указанные альбомы (ключи через запятую)')
    ap.add_argument('--download', action='store_true', help='Скачать обложки в папку covers/')
    
    a = ap.parse_args()
    
    order = [x.strip() for x in a.sources.split(',') if x.strip()]
    bad = [x for x in order if x not in SOURCES]
    if bad:
        sys.exit('ERROR: Неизвестные источники: ' + ', '.join(bad))
    
    print(f'Читаю {a.html}...')
    html = open(a.html, encoding='utf-8').read()
    albums, tracks = read_project(html)
    
    if not albums:
        sys.exit('ERROR: Не найдены альбомы в HTML. Проверьте формат файла.')
    
    print(f'Найдено альбомов: {len(albums)}')
    
    covers, info = collect(albums, tracks, order, 
                          set(a.only.split(',')) if a.only else None)
    
    if a.download:
        covers = download(covers)
    
    # Сохраняем JSON с данными
    json.dump({'covers': covers, 'track_info': info}, 
              open('covers.json', 'w', encoding='utf-8'), 
              ensure_ascii=False, indent=2)
    
    # Обновляем HTML
    open(a.html, 'w', encoding='utf-8').write(patch_html(html, covers, info))
    
    print(f'\n✅ ГОТОВО!')
    print(f'   Обложек найдено: {len(covers)} из {len(albums)}')
    print(f'   Треков с фитами: {len(info)}')
    print(f'   HTML обновлён')
    print(f'   covers.json сохранён')

if __name__ == '__main__':
    main()
