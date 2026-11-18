"""本地合成资料来源，仅提供来源行为与观察记录。"""
import argparse
from contextlib import contextmanager
import gzip
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

DATA = Path(__file__).resolve().parents[1] / 'data'

class Source:
    def __init__(self):
        self.lock = threading.RLock()
        self.clock = 0
        self.phase = 0
        self.log = []
        self.extra = {}

    def response(self, method, raw_path, headers, body):
        path = urlsplit(raw_path).path
        q = {k:v[-1] for k,v in parse_qs(urlsplit(raw_path).query).items()}
        with self.lock:
            self.log.append({'method':method,'path':raw_path,'time':self.clock,
                             'headers':{k.lower():v for k,v in headers.items()}})
            if path == '/health': return 200, {}, {'来源':'本地合成资料','phase':self.phase,'time':self.clock}
            if path == '/__admin/log': return 200, {}, self.log[:-1]
            if path == '/__admin/advance' and method == 'POST':
                value=json.loads(body or b'{}')
                self.clock += int(value.get('seconds',0))
                self.phase = int(value.get('phase',self.phase))
                return 200, {}, {'phase':self.phase,'time':self.clock}
            if path.startswith('/__admin/'):
                return 404, {}, {'error':'未知演练控制'}
            return self.product(method,path,q,{k.lower():v for k,v in headers.items()},body)

    def product(self,method,path,q,h,body):
        jobs=self.extra.setdefault('jobs',[])
        if path=='/':
            session=self.extra.setdefault('session','local-browser-session')
            return 200,{'Set-Cookie':'source_session='+session+'; Path=/; SameSite=Strict'},(DATA/'portal.html').read_text(encoding='utf-8')
        if path=='/context':
            company=q.get('company','C01');year=q.get('year','2025')
            return 200,{}, {'company':company,'year':year,'scope':'集团' if company=='C01' else '单体','label':'同名实业','ready':True}
        if path=='/apply' and method=='POST':
            v=json.loads(body); key='E-'+str(len(jobs)+1)
            if any(self.clock<x['ready_at'] for x in jobs):return 409,{}, {'error':'已有导出正在办理'}
            jobs.append({'id':key,'company':v['company'],'year':v['year'],'scope':v['scope'],'created':self.clock,'visible':self.clock+(2 if self.phase==1 else 0),'ready_at':self.clock+3,'actor':'operator' if self.phase==2 else 'automatic','files':['main','supplement']})
            if self.phase==1:return 0,{},b''
            return 201,{}, {'accepted':key,'quota_used':len(jobs)}
        if path=='/jobs':
            rows=[{**x,'state':'ready' if self.clock>=x['ready_at'] else 'queued'} for x in jobs if self.clock>=x['visible']]
            return 200,{}, {'jobs':rows,'quota_used':len(jobs)}
        if path=='/ticket':
            job=next(x for x in jobs if x['id']==q['id'])
            if self.clock<job['ready_at']:return 409,{}, {'error':'仍在处理'}
            return 200,{}, {'links':[{'part':part,'url':f'/download/{job["id"]}/{part}?expires={self.clock+4}&session='+self.extra.get('session','local-browser-session')} for part in job['files']]}
        if path.startswith('/download/'):
            _,_,key,part=path.split('/');job=next(x for x in jobs if x['id']==key)
            if q.get('session')!=self.extra.get('session','local-browser-session') or 'source_session='+q.get('session','') not in h.get('cookie',''):return 403,{}, {'error':'下载会话不符'}
            if self.clock>int(q['expires']):return 410,{}, {'error':'下载会话过期'}
            if self.phase==3 and part=='supplement':return 503,{}, {'error':'补充册暂不可用'}
            return 200,{'Content-Type':'text/plain; charset=utf-8','Content-Disposition':f'attachment; filename="{key}-{part}.txt"'},json.dumps({'application':key,'company':job['company'],'year':job['year'],'scope':job['scope'],'part':part},ensure_ascii=False).encode()
        return 404,{}, {'error':'未知门户页面'}

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self,*args): pass
    def do_GET(self): self.handle_request()
    def do_POST(self): self.handle_request()
    def handle_request(self):
        size=int(self.headers.get('Content-Length','0'))
        try:
            status,headers,body=self.server.source.response(self.command,self.path,self.headers,self.rfile.read(size))
            if not isinstance(body,(str,bytes)):
                body=json.dumps(body,ensure_ascii=False).encode();headers.setdefault('Content-Type','application/json; charset=utf-8')
            elif isinstance(body,str):
                body=body.encode();headers.setdefault('Content-Type','text/html; charset=utf-8')
        except (ValueError,KeyError,TypeError) as exc:
            status,headers,body=400,{'Content-Type':'application/json'},json.dumps({'error':str(exc)},ensure_ascii=False).encode()
        if status==0:
            self.close_connection=True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.send_response(status)
        for k,v in headers.items():self.send_header(k,str(v))
        self.send_header('Content-Length',len(body))
        self.end_headers()
        if body:self.wfile.write(body)

@contextmanager
def running_source(port=0):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.source=Source()
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield 'http://127.0.0.1:'+str(server.server_port),server.source
    finally: server.shutdown();server.server_close();thread.join()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='启动本地资料来源')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    server=ThreadingHTTPServer(('0.0.0.0',args.port),Handler);server.source=Source()
    print('资料来源已启动，端口 '+str(args.port),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
