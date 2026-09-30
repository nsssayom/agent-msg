"""Real loopback HTTP tests with a scratch journal, no agent interaction."""
import contextlib
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from agent_msg.journal import Journal
from agent_msg.web import make_server, serve, STATUSES
from test_package import envelope


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'journal.db'
        with Journal(self.path) as journal:
            for status in STATUSES:
                e=envelope('<script>window.pwned=true</script>')
                journal.append(e,{'pid':1});journal.event(e['id'],status,'test')
            self.mid=e['id']
        self.server,url=make_server(self.path)
        self.token=parse_qs(urlsplit(url).fragment)['token'][0]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def request(self,path,method='GET',auth=True,headers=None):
        hdr={'Authorization':'Bearer '+self.token} if auth else {}
        hdr.update(headers or {})
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        conn.request(method,path,headers=hdr)
        response=conn.getresponse();body=response.read();result=(response.status,dict(response.getheaders()),body)
        conn.close();return result

    def test_api_requires_bearer_no_cookie_or_url_token(self):
        self.assertEqual(self.request('/api/stats',auth=False)[0],403)
        self.assertEqual(self.request('/api/stats?token='+self.token,auth=False)[0],403)
        self.assertEqual(self.request('/api/stats',auth=False,headers={'Cookie':'agent_msg_ui='+self.token})[0],403)
        status,headers,body=self.request('/api/stats')
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['messages'],len(STATUSES))
        self.assertNotIn('Set-Cookie',headers)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'],'no-store')

    def test_host_methods_and_traversal(self):
        self.assertEqual(self.request('/api/stats',headers={'Host':'evil.example'})[0],421)
        self.assertEqual(self.request('/api/stats',method='POST')[0],405)
        self.assertEqual(self.request('/../journal.py')[0],404)
        self.assertEqual(self.request('/%2e%2e/journal.py')[0],404)
        self.assertEqual(self.request('/api/stats',method='HEAD')[2],b'')
        self.assertEqual(self.request('/api/stats',headers={'Host':f'agent-msg.local:{self.server.server_port}'})[0],200)
        self.assertEqual(self.request('/api/stats',headers={'Host':'agent-msg.local:99999'})[0],421)

    def test_named_url_and_non_loopback_resolution_rejected(self):
        server,url=make_server(self.path,hostname='agent-msg.local')
        try:
            self.assertTrue(url.startswith('http://agent-msg.local:'))
            self.assertEqual(server.server_address[0],'127.0.0.1')
        finally:server.server_close()
        with patch('agent_msg.web.socket.getaddrinfo',return_value=[(2,1,6,'',('203.0.113.1',0))]):
            with self.assertRaisesRegex(ValueError,'must resolve to 127.0.0.1'):
                serve(self.path,hostname='agent-msg.local')

    def test_default_ui_opens_exact_authenticated_url_and_falls_back_safely(self):
        for addresses,hostname in [([], '127.0.0.1'), ([(2,1,6,'',('127.0.0.1',0))], 'agent-msg.local')]:
            url=f'http://{hostname}:8765/#token=test-token'
            server=Mock()
            with patch('agent_msg.web.socket.getaddrinfo',return_value=addresses), \
                 patch('agent_msg.web.make_server',return_value=(server,url)) as make, \
                 patch('agent_msg.web.webbrowser.open',return_value=True) as browser, \
                 patch('agent_msg.web.threading.Timer',side_effect=lambda delay,callback:SimpleNamespace(start=callback,daemon=False)):
                serve(self.path)
            make.assert_called_once_with(self.path,0,hostname=hostname)
            browser.assert_called_once_with(url,new=1)
            server.server_close.assert_called_once()

    def test_static_is_public_but_contains_no_journal_data(self):
        for path in ('/','/app.js','/style.css'):
            status,headers,body=self.request(path,auth=False)
            self.assertEqual(status,200)
            self.assertNotIn(self.mid.encode(),body)
            self.assertNotIn(self.token.encode(),body)
        script=self.request('/app.js',auth=False)[2]
        self.assertNotIn(b'.innerHTML',script)

    def test_filters_and_conversation(self):
        for status in STATUSES:
            code,_,body=self.request('/api/messages?status='+status)
            self.assertEqual(code,200);self.assertEqual(len(json.loads(body)['messages']),1)
        self.assertEqual(self.request('/api/messages?limit=bad')[0],400)
        self.assertEqual(self.request('/api/messages?status=bogus')[0],400)
        self.assertEqual(self.request('/api/messages/missing')[0],404)
        self.assertEqual(self.request('/api/messages/'+self.mid+'/conversation')[0],200)

    def test_missing_and_uninitialised_journal_503(self):
        self.path.unlink()
        self.assertEqual(self.request('/api/messages')[0],503)
        self.assertFalse(self.path.exists())
        with contextlib.closing(sqlite3.connect(self.path)):pass
        self.assertEqual(self.request('/api/messages')[0],503)


if __name__=='__main__':unittest.main()
