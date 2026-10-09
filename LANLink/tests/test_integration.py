import sys
import tempfile
import threading
import unittest
import json
import urllib.request
import urllib.error
import urllib.parse
import zipfile
import io
import base64
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        server.DATA=Path(cls.tmp.name)
        code=server.setup()
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.http.join_code=code
        cls.http.attempts={}
        cls.http.attempt_lock=threading.Lock()
        cls.url='http://127.0.0.1:'+str(cls.http.server_port)
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
        cls.code=code

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.tmp.cleanup()

    def request(self,path,data=None,cookie='',raw=None,headers=None):
        h={'X-LANLink':'1','Cookie':cookie}
        h.update(headers or {})
        payload=raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        req=urllib.request.Request(self.url+path,data=payload,headers=h)
        try:
            r=urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            r=e
        return r.code,r.read(),r.headers

    def test_full_flow(self):
        self.assertEqual(self.request('/api/state')[0],401)
        self.assertEqual(self.request('/api/register',{'name':'Alice','password':'secret1','code':'wrong'})[0],403)
        cookies=[]
        for name in ('Alice','Bob','Carol'):
            status,body,headers=self.request('/api/register',{'name':name,'password':'secret1','code':self.code})
            self.assertEqual(status,200)
            cookies.append(headers['Set-Cookie'].split(';')[0])
        a,b,c=cookies
        avatar=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/O9kAAAAASUVORK5CYII=')
        self.assertEqual(self.request('/api/avatar',cookie=a,raw=b'not an image')[0],400)
        self.assertEqual(self.request('/api/avatar',cookie=a,raw=avatar)[0],200)
        self.assertEqual(self.request('/avatar/1')[0],401)
        self.assertEqual(self.request('/avatar/1',cookie=b)[1],avatar)
        self.assertTrue(json.loads(self.request('/api/state',cookie=b)[1])['users'][0]['avatar_file'])
        self.assertEqual(self.request('/api/login',{'name':'Alice','password':'badpass'})[0],403)
        self.assertEqual(self.request('/api/message',{'text':'hello all'},a)[0],201)
        self.assertEqual(self.request('/api/message',{'text':'private','recipient':2},a)[0],201)
        public=json.loads(self.request('/api/state',cookie=c)[1])
        self.assertEqual([m['text'] for m in public['messages']],['hello all'])
        personal=json.loads(self.request('/api/state?recipient=1',cookie=b)[1])
        self.assertEqual(personal['messages'][0]['text'],'private')
        self.assertEqual(json.loads(self.request('/api/state?recipient=1',cookie=c)[1])['messages'],[])
        data=b'LAN file bytes\x00\xff'
        status,body,_=self.request('/api/upload?recipient=2',cookie=a,raw=data,headers={'X-Filename':'..%2Fhello.txt'})
        self.assertEqual(status,201)
        fid=json.loads(body)['id']
        self.assertEqual(self.request('/download/'+fid,cookie=c)[0],404)
        self.assertEqual(self.request('/download/'+fid,cookie=b)[1],data)
        self.assertEqual(self.request('/api/message',{'text':'x'},a,headers={'Origin':'http://evil.example'})[0],403)
        self.assertEqual(self.request('/api/message',{'text':'x'},a,headers={'X-LANLink':''})[0],403)
        self.assertEqual(self.request('/api/upload',cookie=a,raw=b'')[0],400)
        self.assertEqual(self.request('/api/message',{'text':'x'*4001},a)[0],400)
        self.assertEqual(self.request('/api/logout',{},a)[0],200)
        self.assertEqual(self.request('/api/state',cookie=a)[0],401)
        # Reopen the same database: persisted history remains available.
        server.setup()
        self.assertEqual(json.loads(self.request('/api/state',cookie=b)[1])['messages'][0]['text'],'hello all')
        # A registered user does not gain admin rights automatically.
        self.assertFalse(json.loads(self.request('/api/state',cookie=b)[1])['me']['is_admin'])
        room=json.loads(self.request('/api/room/create',{'name':'Учебная группа'},b)[1])
        rid=room['id']
        self.assertEqual(self.request('/api/message',{'text':'тема урока','room':rid},b)[0],201)
        self.assertEqual(self.request('/api/state?room='+str(rid),cookie=c)[0],400)
        self.assertEqual(self.request('/api/search?q=test&room='+str(rid),cookie=c)[0],400)
        group_file=json.loads(self.request('/api/upload?room='+str(rid),cookie=b,raw=b'group',headers={'X-Filename':'notes.txt'})[1])['id']
        self.assertEqual(self.request('/download/'+group_file,cookie=c)[0],404)
        self.assertEqual(self.request('/api/room/join',{'id':rid},c)[0],200)
        self.assertEqual(self.request('/download/'+group_file,cookie=c)[1],b'group')
        self.assertEqual(json.loads(self.request('/api/search?q='+urllib.parse.quote('урока')+'&room='+str(rid),cookie=c)[1])['results'][0]['text'],'тема урока')
        self.assertEqual(json.loads(self.request('/api/search?q='+urllib.parse.quote('notes')+'&room='+str(rid),cookie=c)[1])['results'][0]['file_name'],'notes.txt')
        self.assertEqual(self.request('/api/admin/audit',cookie=b)[0],403)
        server.set_admin('test-admin@local','test-secret-pass')
        status,_,headers=self.request('/api/login',{'name':'test-admin@local','password':'test-secret-pass'})
        self.assertEqual(status,200)
        admin=headers['Set-Cookie'].split(';')[0]
        self.assertTrue(json.loads(self.request('/api/state',cookie=admin)[1])['me']['is_admin'])
        self.assertEqual(self.request('/api/admin/block',{'user_id':2,'minutes':5},admin)[0],200)
        self.assertEqual(self.request('/api/message',{'text':'blocked'},b)[0],403)
        self.assertEqual(self.request('/api/admin/unblock',{'user_id':2},admin)[0],200)
        self.assertEqual(self.request('/api/message',{'text':'unblocked'},b)[0],201)
        response=json.loads(self.request('/api/message',{'text':'Ты дурак!'},b)[1])
        self.assertTrue(response['censored'])
        self.assertEqual(json.loads(self.request('/api/state',cookie=b)[1])['messages'][-1]['text'],'Ты *****!')
        self.assertEqual(self.request('/api/admin/censorship',cookie=b)[0],403)
        self.assertEqual(self.request('/api/admin/censorship/add',{'word':'злюка*'},admin)[0],200)
        status,body,_=self.request('/api/message',{'text':'зл-юка рядом'},b)
        self.assertEqual(status,201)
        self.assertTrue(json.loads(body)['censored'])
        self.assertEqual(json.loads(self.request('/api/state',cookie=b)[1])['messages'][-1]['text'],'****** рядом')
        last=json.loads(self.request('/api/state',cookie=b)[1])['messages'][-1]['id']
        status,body,_=self.request('/api/message/edit',{'id':last,'text':'Злюками'},b)
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)['censored'])
        self.assertEqual(json.loads(self.request('/api/state',cookie=b)[1])['messages'][-1]['text'],'*******')
        self.assertEqual(self.request('/api/room/create',{'name':'Злюка группа'},b)[0],400)
        self.assertTrue(json.loads(self.request('/api/upload',cookie=b,raw=b'file',headers={'X-Filename':urllib.parse.quote('злюка.txt')})[1])['censored'])
        self.assertTrue(json.loads(self.request('/api/state',cookie=b)[1])['messages'][-1]['file_name'].endswith('.txt'))
        self.assertEqual(self.request('/api/admin/censorship/remove',{'word':'злюка*'},admin)[0],200)
        # A selected DM does not mark new common-chat messages as read.
        self.request('/api/state',cookie=b)
        self.assertEqual(self.request('/api/message',{'text':'one more public'},c)[0],201)
        unread=json.loads(self.request('/api/state?recipient=3',cookie=b)[1])['unread']
        self.assertEqual(unread['general'],1)
        self.assertEqual(json.loads(self.request('/api/state',cookie=b)[1])['unread']['general'],0)
        self.assertGreaterEqual(len(json.loads(self.request('/api/admin/audit',cookie=admin)[1])['events']),2)
        status,body,_=self.request('/api/admin/backup',cookie=admin)
        self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertIn('storage/lanlink.sqlite',archive.namelist())
            self.assertIn('storage/censorship.json',archive.namelist())
            self.assertIn('storage/files/'+group_file,archive.namelist())
            self.assertTrue(any(name.startswith('storage/avatars/') for name in archive.namelist()))
        self.assertEqual(self.request('/api/avatar/delete',{},a)[0],401)
        status,_,headers=self.request('/api/login',{'name':'Alice','password':'secret1'})
        self.assertEqual(status,200)
        a2=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('/api/avatar/delete',{},a2)[0],200)
        self.assertEqual(self.request('/avatar/1',cookie=b)[0],404)

if __name__=='__main__':
    unittest.main()
