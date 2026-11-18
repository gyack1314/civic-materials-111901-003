"""本地来源契约检查，不包含尚未实现的采集逻辑。"""
import gzip
import http.client
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from source.server import running_source

class SourceContract(unittest.TestCase):
    def request(self,url,payload=None,headers=None):
        parts=urlsplit(url);conn=http.client.HTTPConnection(parts.hostname,parts.port,timeout=10)
        body=json.dumps(payload).encode() if payload is not None else None
        conn.request('POST' if payload is not None else 'GET',parts.path+('?' + parts.query if parts.query else ''),body,headers or {})
        response=conn.getresponse();result=(response.status,dict(response.getheaders()),response.read());conn.close();return result
    def get(self,url,headers=None):return self.request(url,headers=headers)
    def post(self,url,payload):
        status,headers,body=self.request(url,payload)
        self.assertLess(status,300)
        return json.loads(body)
    def test_observable_source(self):
        with running_source() as (base,source):
            self.assertEqual(self.get(base+'/health')[0],200)
            self.post(base+'/__admin/advance',{'seconds':2})
            self.assertEqual(json.loads(self.get(base+'/health')[2])['time'],2)
            self.assertGreaterEqual(len(json.loads(self.get(base+'/__admin/log')[2])),3)

    def test_browser_application(self):
        from playwright.sync_api import sync_playwright
        with running_source() as (base,s),sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--no-sandbox']);page=browser.new_page(accept_downloads=True)
            page.goto(base);page.select_option('#company','C02');page.wait_for_function("document.querySelector('#context').dataset.ready==='yes'")
            page.click('#apply');page.wait_for_selector('[data-application="E-1"]')
            self.post(base+'/__admin/advance',{'seconds':3});page.click('#refresh');page.wait_for_selector('[data-state="ready"]')
            page.click('[data-ticket="E-1"]');page.wait_for_selector('a[download]')
            with page.expect_download() as dl:page.locator('a[download]').first.click()
            self.assertIn('E-1',dl.value.suggested_filename);browser.close()

if __name__=='__main__':unittest.main()
