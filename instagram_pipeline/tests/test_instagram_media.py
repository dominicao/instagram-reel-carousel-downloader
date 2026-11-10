import http.cookiejar
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import instagram_media as m
from PIL import Image
from gallery_dl.extractor.message import Message

URL='https://www.instagram.com/p/Dd5eza_Ckvc/'

class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.fixture=self.root/'fixture.png'
        Image.new('RGB',(40,60),(100,120,140)).save(self.fixture)

    def fake_fetch(self,url,work,cookies):
        found=[]
        for i in (1,2,3):
            p=work/f'{i}.png';shutil.copyfile(self.fixture,p)
            found.append((i,p,'image'))
        return found,3,{'caption':'example'}

    def test_carousel_archive_order_cache_tamper(self):
        with patch.object(m,'fetch_carousel',side_effect=self.fake_fetch) as fetch:
            saved,cached=m.acquire(URL,'carousel',self.root/'library')
            self.assertFalse(cached)
            self.assertEqual([x['index'] for x in saved['assets']],[1,2,3])
            self.assertTrue(m.acquire(URL,'carousel',self.root/'library')[1])
            self.assertEqual(fetch.call_count,1)
        folder=self.root/'library/Dd5eza_Ckvc'
        (folder/'002.png').write_text('bad')
        with self.assertRaises(m.base.DownloadError):
            m.acquire(URL,'carousel',self.root/'library')

    def test_partial_has_no_ready_manifest(self):
        def partial(url,work,cookies):
            assets,_,meta=self.fake_fetch(url,work,cookies)
            return assets[:2],3,meta
        with patch.object(m,'fetch_carousel',side_effect=partial):
            with self.assertRaisesRegex(m.base.DownloadError,'incomplete'):
                m.acquire(URL,'carousel',self.root/'lib')
        self.assertFalse(list(self.root.rglob('manifest.json')))
        self.assertFalse(list(self.root.rglob('.incoming-*')))

    def test_locked_session_never_contacts_instagram(self):
        with patch.object(m,'chrome_session',side_effect=m.base.DownloadError('browser_session_locked')),patch.object(m,'fetch_reel') as fetch:
            with self.assertRaisesRegex(m.base.DownloadError,'browser_session_locked'):
                m.acquire(URL,'reel',self.root/'lib','chrome')
            fetch.assert_not_called()
        failure=json.loads(next(self.root.rglob('last-failure.json')).read_text())
        self.assertIsNone(failure['retry_after'])

    def test_challenge_stops_and_cools_down(self):
        with patch.object(m,'fetch_carousel',side_effect=m.base.DownloadError('verification_required')) as fetch:
            for reason in ('verification_required','cooldown'):
                with self.assertRaisesRegex(m.base.DownloadError,reason):
                    m.acquire(URL,'carousel',self.root/'lib')
            self.assertEqual(fetch.call_count,1)

    def test_html_rejected_and_cdn_scope(self):
        p=self.root/'fake.jpg';p.write_text('<html>login</html>')
        with self.assertRaises(m.base.DownloadError):m.validate_asset(p,'image')
        for url in ('http://scontent.cdninstagram.com/a','https://evil.test/a','https://cdninstagram.com.evil.test/a'):
            with self.assertRaises(m.base.DownloadError):m.cdn_url(url)
        self.assertEqual(m.cdn_url('https://scontent.cdninstagram.com/a'),'https://scontent.cdninstagram.com/a')

    def test_gallery_actual_message_contract(self):
        class FakeExtractor:
            category='instagram';subcategory='post'
            def _parse_post(self,p): return p
            def __iter__(self):
                yield Message.Directory,'',{'post_shortcode':'Dd5eza_Ckvc','count':2,'description':'hi'}
                for i in (1,2):
                    yield Message.Url,'https://scontent.cdninstagram.com/img',{'num':i,'extension':'jpg'}
        def download(session,url,path,total):
            shutil.copyfile(self.fixture,path);return path.stat().st_size
        with patch('gallery_dl.extractor.find',return_value=FakeExtractor()),patch.object(m,'download_asset',side_effect=download):
            assets,count,meta=m.fetch_carousel(URL,self.root,None)
        self.assertEqual(count,2)
        self.assertEqual([a[0] for a in assets],[1,2])
        self.assertEqual(meta['description'],'hi')

    def test_cookie_logger_accepts_upstream_keyword_arguments(self):
        log=m.QuietLogger()
        log.warning('cannot decrypt v10 cookies: no key found', only_once=True)
        log.warning('another warning', only_once=True)
        self.assertEqual(log.reason,'browser_session_locked')
        log.debug('not persisted', only_once=True)

    def test_session_container_is_compatible_with_real_instagram_extractor(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.cookies import YoutubeDLCookieJar
        from yt_dlp.extractor.instagram import InstagramIE
        source=http.cookiejar.CookieJar()
        for domain,name in [('.instagram.com','sessionid'),('.example.com','unrelated')]:
            source.set_cookie(http.cookiejar.Cookie(0,name,'synthetic-test-value',None,False,
                domain,True,True,'/',True,True,None,True,None,None,{}))
        with patch('yt_dlp.cookies.extract_cookies_from_browser',return_value=source):
            jar=m.chrome_session()
        self.assertIsInstance(jar,YoutubeDLCookieJar)
        self.assertEqual([c.name for c in jar],['sessionid'])
        with YoutubeDL({'quiet':True,'cachedir':False}) as y:
            y.cookiejar=jar
            self.assertIn('sessionid',InstagramIE(y)._get_cookies('https://www.instagram.com/'))

    def test_no_browser_access_by_default(self):
        with patch.object(m,'chrome_session') as auth,patch.object(m,'fetch_carousel',side_effect=self.fake_fetch):
            m.acquire(URL,'carousel',self.root/'lib')
            auth.assert_not_called()

if __name__=='__main__': unittest.main()
