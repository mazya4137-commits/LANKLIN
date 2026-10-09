"""LANLink: offline chat and file sharing. Python 3.10+, standard library only."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote, quote
import argparse
from contextlib import contextmanager, closing
import hashlib
import hmac
import json
import secrets
import shutil
import socket
import sqlite3
import time
import tempfile
import zipfile
import io
import getpass
import moderation

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'storage'
MAX_FILE = 50 * 1024 * 1024
MAX_AVATAR = 2 * 1024 * 1024


def avatar_format(body):
    if body.startswith(b'\x89PNG\r\n\x1a\n') and body[12:16] == b'IHDR' and body.endswith(b'IEND\xaeB`\x82'):
        return 'png', 'image/png'
    if body.startswith(b'\xff\xd8\xff') and body.endswith(b'\xff\xd9'):
        return 'jpg', 'image/jpeg'
    if body[:4] == b'RIFF' and body[8:12] == b'WEBP' and int.from_bytes(body[4:8], 'little') + 8 == len(body):
        return 'webp', 'image/webp'
    raise ValueError('Выберите изображение PNG, JPEG или WebP')


@contextmanager
def db():
    c = sqlite3.connect(DATA / 'lanlink.sqlite', timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def setup():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / 'files').mkdir(exist_ok=True)
    (DATA / 'avatars').mkdir(exist_ok=True)
    cfg = DATA / 'config.json'
    if not cfg.exists():
        cfg.write_text(json.dumps({'join_code': secrets.token_hex(4)}, indent=2))
    moderation.initial(DATA)
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, salt TEXT, hash TEXT, seen REAL DEFAULT 0, is_admin INTEGER NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, uid INTEGER, expires REAL);
        CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, name TEXT, size INTEGER, sender INTEGER, recipient INTEGER, created REAL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, sender INTEGER, recipient INTEGER, text TEXT, file_id TEXT, created REAL, edited INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0, edited_by INTEGER);
        CREATE INDEX IF NOT EXISTS msg_route ON messages(recipient,id);
        CREATE TABLE IF NOT EXISTS rooms(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_by INTEGER NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS room_members(room_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY(room_id,user_id));
        CREATE TABLE IF NOT EXISTS read_cursors(user_id INTEGER NOT NULL, route TEXT NOT NULL, last_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,route));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor INTEGER NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, created REAL NOT NULL);
        ''')
        # Additive migrations keep existing LANLink databases and their history intact.
        user_columns = {r['name'] for r in c.execute('PRAGMA table_info(users)')}
        if 'is_admin' not in user_columns:
            c.execute('ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0')
        if 'blocked_until' not in user_columns:
            c.execute('ALTER TABLE users ADD COLUMN blocked_until REAL NOT NULL DEFAULT 0')
        if 'avatar_file' not in user_columns:
            c.execute('ALTER TABLE users ADD COLUMN avatar_file TEXT')
        message_columns = {r['name'] for r in c.execute('PRAGMA table_info(messages)')}
        for name, definition in (('edited', 'INTEGER NOT NULL DEFAULT 0'), ('deleted', 'INTEGER NOT NULL DEFAULT 0'), ('updated', 'REAL NOT NULL DEFAULT 0'), ('edited_by', 'INTEGER')):
            if name not in message_columns:
                c.execute(f'ALTER TABLE messages ADD COLUMN {name} {definition}')
        if 'room_id' not in message_columns:
            c.execute('ALTER TABLE messages ADD COLUMN room_id INTEGER')
        if 'censored' not in message_columns:
            c.execute('ALTER TABLE messages ADD COLUMN censored INTEGER NOT NULL DEFAULT 0')
        file_columns = {r['name'] for r in c.execute('PRAGMA table_info(files)')}
        if 'room_id' not in file_columns:
            c.execute('ALTER TABLE files ADD COLUMN room_id INTEGER')
        c.execute('UPDATE messages SET updated=created WHERE updated=0')
        c.execute('CREATE INDEX IF NOT EXISTS msg_room ON messages(room_id,id)')
    return json.loads(cfg.read_text())['join_code']


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200000).hex()


def set_admin(name,password):
    if not 6 <= len(password) <= 128 or not 2 <= len(name) <= 64:
        raise ValueError('Неверная длина логина или пароля')
    salt=secrets.token_hex(16)
    with db() as c:
        row=c.execute('SELECT id FROM users WHERE name=?',(name,)).fetchone()
        if row:
            uid=row['id']
            c.execute('UPDATE users SET salt=?,hash=?,is_admin=1,blocked_until=0 WHERE id=?',(salt,password_hash(password,salt),uid))
            c.execute('DELETE FROM sessions WHERE uid=?',(uid,))
        else:
            uid=c.execute('INSERT INTO users(name,salt,hash,is_admin) VALUES(?,?,?,1)',(name,salt,password_hash(password,salt))).lastrowid
        c.execute('UPDATE users SET is_admin=0 WHERE id!=?',(uid,))
    return uid


class Handler(BaseHTTPRequestHandler):
    server_version = 'LANLink/1.0'

    def reply(self, status, payload, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def user(self):
        token = next((p.strip()[4:] for p in self.headers.get('Cookie', '').split(';') if p.strip().startswith('sid=')), '')
        with db() as c:
            u = c.execute('SELECT u.id,u.name,u.is_admin,u.blocked_until,u.avatar_file FROM users u JOIN sessions s ON s.uid=u.id WHERE s.token=? AND s.expires>?', (token,time.time())).fetchone()
            if u:
                c.execute('UPDATE users SET seen=? WHERE id=?', (time.time(),u['id']))
        return dict(u) if u else None

    def recipient(self, value, uid):
        if value is None or value == '':
            return None
        try:
            rid = int(value)
        except (ValueError, TypeError):
            raise ValueError('Неверный получатель')
        with db() as c:
            if rid == uid or not c.execute('SELECT id FROM users WHERE id=?', (rid,)).fetchone():
                raise ValueError('Получатель не найден')
        return rid

    def route(self, query, uid):
        rid=self.recipient(query.get('recipient',[None])[0],uid)
        raw=query.get('room',[None])[0]
        if rid is not None and raw is not None:
            raise ValueError('Выберите одну беседу')
        if raw is not None:
            room=int(raw)
            if room < 1:
                raise ValueError('Комната не найдена')
            with db() as c:
                if not c.execute('SELECT 1 FROM room_members WHERE room_id=? AND user_id=?',(room,uid)).fetchone():
                    raise PermissionError('Нет доступа к комнате')
            return rid,room,'room:'+str(room)
        return rid,None,'dm:'+str(rid) if rid is not None else 'general'

    @staticmethod
    def where(rid,room,uid):
        if room is not None:
            return 'm.room_id=?',[room]
        if rid is not None:
            return '((m.sender=? AND m.recipient=?) OR (m.sender=? AND m.recipient=?)) AND m.room_id IS NULL',[uid,rid,rid,uid]
        return 'm.recipient IS NULL AND m.room_id IS NULL',[]

    @staticmethod
    def audit(c,uid,action,target):
        c.execute('INSERT INTO audit(actor,action,target,created) VALUES(?,?,?,?)',(uid,action,str(target),time.time()))

    def do_GET(self):
        p = urlsplit(self.path)
        if p.path in ('/', '/app.js', '/ui.js', '/style.css'):
            name = 'index.html' if p.path == '/' else p.path[1:]
            body = (ROOT / 'web' / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', {'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.split('.')[-1]])
            self.send_header('Content-Length',str(len(body)))
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)
            return
        u = self.user()
        if not u:
            return self.reply(401, {'error':'Войдите в аккаунт'})
        if p.path.startswith('/avatar/'):
            try:
                uid = int(p.path.removeprefix('/avatar/'))
            except ValueError:
                return self.reply(404,{'error':'Аватар не найден'})
            with db() as c:
                avatar = c.execute('SELECT avatar_file FROM users WHERE id=?',(uid,)).fetchone()
            if not avatar or not avatar['avatar_file']:
                return self.reply(404,{'error':'Аватар не найден'})
            filename = avatar['avatar_file']
            mime = {'.png':'image/png','.jpg':'image/jpeg','.webp':'image/webp'}.get(Path(filename).suffix)
            if not mime or Path(filename).name != filename:
                return self.reply(404,{'error':'Аватар не найден'})
            path = DATA / 'avatars' / filename
            if not path.is_file():
                return self.reply(404,{'error':'Аватар не найден'})
            self.send_response(200)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(path.stat().st_size))
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Cache-Control','private, max-age=3600')
            self.end_headers()
            with path.open('rb') as stream:
                shutil.copyfileobj(stream,self.wfile)
            return
        if p.path == '/api/state':
            try:
                q = parse_qs(p.query)
                rid,room,route_key = self.route(q,u['id'])
                before = max(0,int(q.get('before',['0'])[0]))
            except (ValueError,PermissionError) as e:
                return self.reply(400,{'error':str(e)})
            with db() as c:
                users = [dict(r) for r in c.execute('SELECT id,name,seen,is_admin,blocked_until,avatar_file FROM users ORDER BY name')]
                rooms=[dict(r) for r in c.execute('SELECT r.id,r.name FROM rooms r JOIN room_members x ON x.room_id=r.id WHERE x.user_id=? ORDER BY r.name',(u['id'],))]
                route,params=self.where(rid,room,u['id'])
                sql = f'SELECT m.*,u.name sender_name,u.is_admin sender_is_admin,u.avatar_file sender_avatar,eu.name editor_name,f.name file_name,f.size file_size FROM messages m JOIN users u ON u.id=m.sender LEFT JOIN users eu ON eu.id=m.edited_by LEFT JOIN files f ON f.id=m.file_id WHERE {route}'
                current=c.execute('SELECT COALESCE(MAX(m.id),0) FROM messages m WHERE '+route,params).fetchone()[0]
                unread={}
                routes=[('general',None,None)]+[('room:'+str(r['id']),None,r['id']) for r in rooms]+[('dm:'+str(x['id']),x['id'],None) for x in users if x['id']!=u['id']]
                for key,peer,rid_room in routes:
                    wh,pp=self.where(peer,rid_room,u['id'])
                    cursor=c.execute('SELECT last_id FROM read_cursors WHERE user_id=? AND route=?',(u['id'],key)).fetchone()
                    if cursor is None:
                        unread[key]=0  # First visit starts with no unread history.
                        maximum=c.execute('SELECT COALESCE(MAX(m.id),0) FROM messages m WHERE '+wh,pp).fetchone()[0]
                        c.execute('INSERT OR IGNORE INTO read_cursors(user_id,route,last_id) VALUES(?,?,?)',(u['id'],key,maximum))
                    else:
                        unread[key]=c.execute('SELECT COUNT(*) FROM messages m WHERE '+wh+' AND m.id>? AND m.sender!=?',pp+[cursor[0],u['id']]).fetchone()[0]
                if before:
                    rows=c.execute(sql+' AND m.id<? ORDER BY m.id DESC LIMIT 50',params+[before]).fetchall()[::-1]
                else:
                    rows=c.execute(sql+' ORDER BY m.id DESC LIMIT 200',params).fetchall()[::-1]
                    c.execute('INSERT INTO read_cursors(user_id,route,last_id) VALUES(?,?,?) ON CONFLICT(user_id,route) DO UPDATE SET last_id=excluded.last_id',(u['id'],route_key,current))
                    unread[route_key]=0
            return self.reply(200, {'me':u,'users':users,'rooms':rooms,'messages':[dict(r) for r in rows],'unread':unread,'has_more':len(rows)==(50 if before else 200),'now':time.time()})
        if p.path == '/api/search':
            try:
                q=parse_qs(p.query)
                rid,room,_=self.route(q,u['id'])
                needle=q.get('q',[''])[0].strip()
                if not 2<=len(needle)<=100:
                    raise ValueError('Поиск: от 2 до 100 символов')
            except (ValueError,PermissionError) as e:
                return self.reply(400,{'error':str(e)})
            where,params=self.where(rid,room,u['id'])
            with db() as c:
                rows=c.execute('SELECT m.id,m.text,m.created,m.file_id,u.name sender_name,f.name file_name FROM messages m JOIN users u ON u.id=m.sender LEFT JOIN files f ON f.id=m.file_id WHERE '+where+' AND m.deleted=0 AND (instr(lower(m.text),lower(?))>0 OR instr(lower(COALESCE(f.name,\'\')),lower(?))>0) ORDER BY m.id DESC LIMIT 50',params+[needle,needle]).fetchall()
            return self.reply(200,{'results':[dict(r) for r in rows]})
        if p.path == '/api/rooms':
            with db() as c:
                rooms=[dict(r) for r in c.execute('SELECT r.id,r.name,COUNT(x.user_id) members FROM rooms r LEFT JOIN room_members x ON x.room_id=r.id GROUP BY r.id ORDER BY r.name')]
            return self.reply(200,{'rooms':rooms})
        if p.path == '/api/admin/audit':
            if not u['is_admin']:
                return self.reply(403,{'error':'Требуются права администратора'})
            with db() as c:
                rows=c.execute('SELECT a.id,a.action,a.target,a.created,u.name actor FROM audit a JOIN users u ON u.id=a.actor ORDER BY a.id DESC LIMIT 100').fetchall()
            return self.reply(200,{'events':[dict(r) for r in rows]})
        if p.path == '/api/admin/censorship':
            if not u['is_admin']:
                return self.reply(403,{'error':'Требуются права администратора'})
            return self.reply(200,{'terms':moderation.terms(DATA)})
        if p.path == '/api/admin/backup':
            if not u['is_admin']:
                return self.reply(403,{'error':'Требуются права администратора'})
            with tempfile.TemporaryDirectory() as tmp:
                snapshot=Path(tmp)/'lanlink.sqlite'
                with db() as src, closing(sqlite3.connect(snapshot)) as dst:
                    src.backup(dst)
                output=io.BytesIO()
                with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
                    archive.write(snapshot,'storage/lanlink.sqlite')
                    archive.write(DATA/'config.json','storage/config.json')
                    archive.write(DATA/'censorship.json','storage/censorship.json')
                    for f in (DATA/'files').iterdir():
                        if f.is_file():
                            archive.write(f,'storage/files/'+f.name)
                    for f in (DATA/'avatars').iterdir():
                        if f.is_file():
                            archive.write(f,'storage/avatars/'+f.name)
                body=output.getvalue()
            self.send_response(200)
            self.send_header('Content-Type','application/zip')
            self.send_header('Content-Disposition','attachment; filename="lanlink-backup.zip"')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        if p.path.startswith('/download/'):
            fid = p.path.split('/')[-1]
            with db() as c:
                f = c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
            if not f or (f['recipient'] is not None and u['id'] not in (f['sender'],f['recipient'])):
                return self.reply(404, {'error':'Файл не найден'})
            if f['room_id'] is not None:
                with db() as c:
                    if not c.execute('SELECT 1 FROM room_members WHERE room_id=? AND user_id=?',(f['room_id'],u['id'])).fetchone():
                        return self.reply(404,{'error':'Файл не найден'})
            path = DATA / 'files' / f['id']
            if not path.is_file():
                return self.reply(404,{'error':'Файл отсутствует на сервере'})
            self.send_response(200)
            self.send_header('Content-Type','application/octet-stream')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(f['size']))
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''"+quote(f['name']))
            self.end_headers()
            with path.open('rb') as stream:
                shutil.copyfileobj(stream,self.wfile)
            return
        self.reply(404,{'error':'Адрес не найден'})

    def do_POST(self):
        # Browser cross-origin requests cannot supply this header without a preflight.
        if self.headers.get('X-LANLink') != '1':
            return self.reply(403,{'error':'Запрос отклонён'})
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host',''):
            return self.reply(403,{'error':'Чужой источник запроса'})
        try:
            p = urlsplit(self.path)
            size = int(self.headers.get('Content-Length','0'))
            limit = MAX_FILE if p.path == '/api/upload' else MAX_AVATAR if p.path == '/api/avatar' else 16384
            if size < 0 or size > limit:
                return self.reply(413,{'error':'Слишком большой файл или запрос'})
            if p.path == '/api/upload':
                return self.upload(p,size)
            if p.path == '/api/avatar':
                return self.upload_avatar(size)
            data = json.loads(self.rfile.read(size))
            if not isinstance(data,dict):
                raise ValueError('Ожидается JSON-объект')
            if p.path in ('/api/register','/api/login'):
                return self.auth(p.path,data)
            u = self.user()
            if not u:
                return self.reply(401,{'error':'Войдите в аккаунт'})
            if p.path == '/api/logout':
                token = next((p.strip()[4:] for p in self.headers.get('Cookie','').split(';') if p.strip().startswith('sid=')), '')
                with db() as c:
                    c.execute('DELETE FROM sessions WHERE token=?',(token,))
                return self.reply(200,{'ok':True},'sid=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            if p.path == '/api/avatar/delete':
                with db() as c:
                    old = c.execute('SELECT avatar_file FROM users WHERE id=?',(u['id'],)).fetchone()['avatar_file']
                    c.execute('UPDATE users SET avatar_file=NULL WHERE id=?',(u['id'],))
                if old:
                    (DATA / 'avatars' / old).unlink(missing_ok=True)
                return self.reply(200,{'ok':True})
            if p.path in ('/api/admin/censorship/add','/api/admin/censorship/remove'):
                self.require_admin(u)
                word=moderation.validate(data.get('word'))
                result=moderation.update(DATA,word,p.path.endswith('/remove'))
                with db() as c:
                    self.audit(c,u['id'],'remove_filter' if p.path.endswith('/remove') else 'add_filter',word)
                return self.reply(200,{'terms':result})
            if p.path == '/api/room/create':
                self.require_unblocked(u)
                name=data.get('name','')
                if not isinstance(name,str) or not 2<=len(name.strip())<=40:
                    raise ValueError('Название комнаты: от 2 до 40 символов')
                name,filtered=moderation.censor(name.strip(),DATA)
                if filtered:
                    raise ValueError('Название содержит слово из списка цензуры')
                with db() as c:
                    try:
                        room_id=c.execute('INSERT INTO rooms(name,created_by,created) VALUES(?,?,?)',(name,u['id'],time.time())).lastrowid
                    except sqlite3.IntegrityError:
                        return self.reply(409,{'error':'Комната с таким именем уже существует'})
                    c.execute('INSERT INTO room_members VALUES(?,?)',(room_id,u['id']))
                return self.reply(201,{'id':room_id,'name':name})
            if p.path == '/api/room/join':
                room_id=int(data.get('id'))
                with db() as c:
                    room=c.execute('SELECT id,name FROM rooms WHERE id=?',(room_id,)).fetchone()
                    if not room:
                        return self.reply(404,{'error':'Комната не найдена'})
                    c.execute('INSERT OR IGNORE INTO room_members VALUES(?,?)',(room_id,u['id']))
                return self.reply(200,dict(room))
            if p.path == '/api/message':
                self.require_unblocked(u)
                value = data.get('text','')
                if not isinstance(value,str) or not 1 <= len(value.strip()) <= 4000:
                    raise ValueError('Введите сообщение длиной до 4000 символов')
                value,filtered=moderation.censor(value.strip(),DATA)
                rid,room,_ = self.route({'recipient':[data.get('recipient')],'room':[data.get('room')]} if data.get('room') is not None else {'recipient':[data.get('recipient')]},u['id'])
                with db() as c:
                    c.execute('INSERT INTO messages(sender,recipient,text,created,room_id,censored) VALUES(?,?,?,?,?,?)',(u['id'],rid,value,time.time(),room,int(filtered)))
                return self.reply(201,{'ok':True,'censored':filtered})
            if p.path == '/api/message/edit':
                value = data.get('text','')
                if not isinstance(value,str) or not 1 <= len(value.strip()) <= 4000:
                    raise ValueError('Введите сообщение длиной до 4000 символов')
                value,filtered=moderation.censor(value.strip(),DATA)
                try:
                    mid = int(data.get('id'))
                except (ValueError, TypeError):
                    raise ValueError('Не найдено сообщение')
                with db() as c:
                    message = c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
                    if not message or message['deleted'] or message['file_id']:
                        return self.reply(404,{'error':'Сообщение недоступно для изменения'})
                    allowed = message['sender'] == u['id'] or (u['is_admin'] and message['recipient'] is None and message['room_id'] is None)
                    if not allowed:
                        return self.reply(403,{'error':'Можно изменять только свои сообщения'})
                    c.execute('UPDATE messages SET text=?,edited=1,edited_by=?,updated=?,censored=? WHERE id=?',(value,u['id'],time.time(),int(filtered),mid))
                    self.audit(c,u['id'],'edit_message',mid)
                return self.reply(200,{'ok':True,'censored':filtered})
            if p.path == '/api/message/delete':
                try:
                    mid = int(data.get('id'))
                except (ValueError, TypeError):
                    raise ValueError('Не найдено сообщение')
                with db() as c:
                    message = c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
                    if not message or message['deleted']:
                        return self.reply(404,{'error':'Сообщение уже удалено'})
                    allowed = message['sender'] == u['id'] or (u['is_admin'] and message['recipient'] is None and message['room_id'] is None)
                    if not allowed:
                        return self.reply(403,{'error':'Можно удалять только свои сообщения'})
                    if message['file_id']:
                        c.execute('DELETE FROM files WHERE id=?',(message['file_id'],))
                    c.execute('UPDATE messages SET text=?,file_id=NULL,deleted=1,updated=? WHERE id=?',('',time.time(),mid))
                    self.audit(c,u['id'],'delete_message',mid)
                if message['file_id']:
                    (DATA / 'files' / message['file_id']).unlink(missing_ok=True)
                return self.reply(200,{'ok':True})
            if p.path == '/api/admin/block':
                self.require_admin(u)
                try:
                    target_id = int(data.get('user_id'))
                    minutes = int(data.get('minutes'))
                except (ValueError, TypeError):
                    raise ValueError('Укажите пользователя и срок блокировки от 5 до 60 минут')
                if not 5 <= minutes <= 60:
                    raise ValueError('Срок блокировки должен быть от 5 до 60 минут')
                with db() as c:
                    target = c.execute('SELECT id,is_admin FROM users WHERE id=?',(target_id,)).fetchone()
                    if not target or target['is_admin']:
                        return self.reply(400,{'error':'Нельзя заблокировать этого пользователя'})
                    until = time.time()+minutes*60
                    c.execute('UPDATE users SET blocked_until=? WHERE id=?',(until,target_id))
                    self.audit(c,u['id'],'block_user',f'{target_id} ({minutes} мин)')
                return self.reply(200,{'ok':True,'blocked_until':until})
            if p.path == '/api/admin/unblock':
                self.require_admin(u)
                try:
                    target_id = int(data.get('user_id'))
                except (ValueError, TypeError):
                    raise ValueError('Не найден пользователь')
                with db() as c:
                    c.execute('UPDATE users SET blocked_until=0 WHERE id=? AND is_admin=0',(target_id,))
                    self.audit(c,u['id'],'unblock_user',target_id)
                return self.reply(200,{'ok':True})
            self.reply(404,{'error':'Адрес не найден'})
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            self.reply(400,{'error':str(e)})
        except PermissionError as e:
            self.reply(403,{'error':str(e)})
        except (sqlite3.Error,OSError):
            self.reply(500,{'error':'Ошибка хранилища. Проверьте свободное место и журнал сервера.'})

    def auth(self,path,data):
        name,password = data.get('name',''),data.get('password','')
        if not isinstance(name,str) or not isinstance(password,str) or not 2 <= len(name.strip()) <= 32 or not 6 <= len(password) <= 128:
            raise ValueError('Имя: 2–32 символа. Пароль: 6–128 символов.')
        name = name.strip()
        # Bound expensive login attempts by IP (in-memory, reset on restart).
        now=time.time()
        with self.server.attempt_lock:
            attempts=[t for t in self.server.attempts.get(self.client_address[0],[]) if t>now-60]
            if len(attempts)>=20:
                return self.reply(429,{'error':'Слишком много попыток. Подождите минуту.'})
            self.server.attempts[self.client_address[0]]=attempts+[now]
        with db() as c:
            if path == '/api/register':
                if not hmac.compare_digest(str(data.get('code','')),self.server.join_code):
                    return self.reply(403,{'error':'Неверный код сети'})
                salt=secrets.token_hex(16)
                try:
                    uid=c.execute('INSERT INTO users(name,salt,hash) VALUES(?,?,?)',(name,salt,password_hash(password,salt))).lastrowid
                except sqlite3.IntegrityError:
                    return self.reply(409,{'error':'Имя уже занято'})
            else:
                row=c.execute('SELECT * FROM users WHERE name=?',(name,)).fetchone()
                if not row or not hmac.compare_digest(row['hash'],password_hash(password,row['salt'])):
                    return self.reply(403,{'error':'Неверное имя или пароль'})
                uid=row['id']
            token=secrets.token_urlsafe(32)
            c.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
            c.execute('INSERT INTO sessions VALUES(?,?,?)',(token,uid,time.time()+86400))
        self.reply(200,{'ok':True},f'sid={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400')

    def upload(self,p,size):
        u=self.user()
        if not u:
            return self.reply(401,{'error':'Войдите в аккаунт'})
        self.require_unblocked(u)
        if size == 0:
            raise ValueError('Файл пустой')
        q=parse_qs(p.query)
        rid,room,_=self.route(q,u['id'])
        name=unquote(self.headers.get('X-Filename','file')).replace('\\','/').split('/')[-1]
        name=''.join(ch for ch in name if ord(ch)>=32)[:180] or 'file'
        suffix=Path(name).suffix
        if len(suffix)>12:
            suffix=''
        stem=name[:-len(suffix)] if suffix else name
        stem,filtered_stem=moderation.censor(stem,DATA)
        suffix,filtered_suffix=moderation.censor(suffix,DATA)
        name=stem+suffix
        filtered=filtered_stem or filtered_suffix
        fid=secrets.token_hex(16)
        path=DATA / 'files' / fid
        try:
            with path.open('wb') as f:
                remaining=size
                while remaining:
                    chunk=self.rfile.read(min(65536,remaining))
                    if not chunk:
                        raise ValueError('Передача прервана')
                    f.write(chunk)
                    remaining-=len(chunk)
            with db() as c:
                now=time.time()
                c.execute('INSERT INTO files(id,name,size,sender,recipient,created,room_id) VALUES(?,?,?,?,?,?,?)',(fid,name,size,u['id'],rid,now,room))
                c.execute('INSERT INTO messages(sender,recipient,text,file_id,created,room_id) VALUES(?,?,?,?,?,?)',(u['id'],rid,'',fid,now,room))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self.reply(201,{'ok':True,'id':fid,'censored':filtered})

    def upload_avatar(self,size):
        u = self.user()
        if not u:
            return self.reply(401,{'error':'Войдите в аккаунт'})
        if size == 0:
            raise ValueError('Выберите изображение до 2 МБ')
        body = self.rfile.read(size)
        if len(body) != size:
            raise ValueError('Передача изображения прервана')
        extension, _ = avatar_format(body)
        filename = secrets.token_hex(16) + '.' + extension
        path = DATA / 'avatars' / filename
        path.write_bytes(body)
        try:
            with db() as c:
                old = c.execute('SELECT avatar_file FROM users WHERE id=?',(u['id'],)).fetchone()['avatar_file']
                c.execute('UPDATE users SET avatar_file=? WHERE id=?',(filename,u['id']))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if old:
            (DATA / 'avatars' / old).unlink(missing_ok=True)
        self.reply(200,{'ok':True,'avatar_file':filename})

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    @staticmethod
    def require_admin(user):
        if not user['is_admin']:
            raise PermissionError('Требуются права главного администратора')

    @staticmethod
    def require_unblocked(user):
        remaining = user['blocked_until']-time.time()
        if remaining > 0:
            minutes = max(1,int((remaining+59)//60))
            raise PermissionError(f'Отправка временно заблокирована. Осталось около {minutes} мин.')


def main():
    import threading
    parser=argparse.ArgumentParser(description='LANLink offline server')
    parser.add_argument('--host',default='0.0.0.0')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--set-admin',metavar='LOGIN',help='Создать или обновить администратора и выйти')
    args=parser.parse_args()
    code=setup()
    if args.set_admin:
        password=getpass.getpass('Пароль администратора: ')
        confirm=getpass.getpass('Повторите пароль: ')
        if password != confirm:
            parser.error('Пароли не совпадают')
        set_admin(args.set_admin,password)
        print('Учётная запись администратора готова. Теперь запустите сервер обычным способом.')
        return
    server=ThreadingHTTPServer((args.host,args.port),Handler)
    server.join_code=code
    server.attempts={}
    server.attempt_lock=threading.Lock()
    print(f'\nLANLink: http://localhost:{args.port}\nКод регистрации / Join code: {code}')
    try:
        addresses=sorted({i[4][0] for i in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET)})
        for address in addresses:
            if not address.startswith('127.'):
                print(f'LAN: http://{address}:{args.port}')
    except OSError:
        pass
    print('Остановка: Ctrl+C. Все данные: storage/.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__=='__main__':
    main()
