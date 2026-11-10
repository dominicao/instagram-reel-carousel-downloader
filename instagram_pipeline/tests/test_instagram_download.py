import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('instagram_download', Path(__file__).parents[1] / 'scripts/instagram_download.py')
downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(downloader)
URL = 'https://www.instagram.com/selenaveyne/reel/De0Mk8mvfBZ/?tracking=discard'


class DownloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.temp.name) / 'fixture.mp4'
        subprocess.run([downloader.binary('ffmpeg'), '-nostdin', '-v', 'error',
                        '-f', 'lavfi', '-i', 'color=c=blue:s=64x96:d=0.3',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.3',
                        '-c:v', 'mpeg4', '-c:a', 'aac', '-shortest', str(cls.fixture)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)
        self.addCleanup(self.work.cleanup)

    def test_url_scope_and_tracking_removal(self):
        self.assertEqual(downloader.canonicalize(URL), ('De0Mk8mvfBZ', 'https://www.instagram.com/p/De0Mk8mvfBZ/'))
        for bad in ['https://instagram.com.evil.test/p/abcde/', 'https://www.instagram.com/selena/',
                    'http://www.instagram.com/p/abcde/', 'https://user:secret@www.instagram.com/p/abcde/']:
            with self.assertRaises(downloader.DownloadError):
                downloader.canonicalize(bad)

    def test_import_decode_cache_and_tamper_detection(self):
        saved, media, cached = downloader.acquire(URL, self.root, self.fixture, 'screen-recording')
        self.assertFalse(cached)
        self.assertTrue(saved['media']['has_audio'])
        self.assertEqual(saved['media']['width'], 64)
        self.assertFalse(saved['source_identity_verified'])
        self.assertTrue(downloader.acquire(URL, self.root)[2])
        media.write_text('not a video')
        with self.assertRaises(downloader.DownloadError):
            downloader.acquire(URL, self.root)

    def test_html_cannot_be_mistaken_for_mp4(self):
        fake = self.root / 'fake.mp4'
        fake.write_text('<html>sign in</html>')
        with self.assertRaises(downloader.DownloadError):
            downloader.acquire(URL, self.root / 'library', fake)
        self.assertFalse(list(self.root.rglob('manifest.json')))

    def test_access_failure_is_redacted_and_stops_retries(self):
        def failure(args, **kwargs):
            self.assertIn('--ignore-config', args)
            self.assertNotIn('--cookies-from-browser', args)
            return subprocess.CompletedProcess(args, 1, '', "This content isn't available to everyone https://cdn.test/?secret=abc")
        with patch.object(downloader, 'run', side_effect=failure) as call:
            with self.assertRaisesRegex(downloader.DownloadError, 'access_restricted'):
                downloader.acquire(URL, self.root)
            with self.assertRaisesRegex(downloader.DownloadError, 'cooldown'):
                downloader.acquire(URL, self.root)
            self.assertEqual(call.call_count, 1)
        text = (self.root / 'De0Mk8mvfBZ/last-failure.json').read_text()
        self.assertNotIn('secret', text)
        self.assertFalse(list(self.root.rglob('manifest.json')))
        # An actual local file remains usable during a network cooldown.
        self.assertTrue(downloader.acquire(URL, self.root, self.fixture)[1].exists())

    def test_job_matches_source_and_preserves_research_gate(self):
        saved, media, _ = downloader.acquire(URL, self.root, self.fixture)
        job = self.root / 'job.json'
        job.write_text(json.dumps({'source_url': URL, 'generation_submitted': False,
                                   'source_file': None, 'research_status': 'age-proof missing'}))
        downloader.attach(job, saved, media)
        changed = json.loads(job.read_text())
        self.assertEqual(changed['research_status'], 'age-proof missing')
        self.assertFalse(changed['generation_submitted'])
        self.assertEqual(changed['status'], 'source_ready_requires_review')
        with self.assertRaises(downloader.DownloadError):
            downloader.attach(job, saved, media)
        job.write_text(json.dumps({'source_url': 'https://www.instagram.com/p/Another123/'}))
        with self.assertRaises(downloader.DownloadError):
            downloader.attach(job, saved, media)

    def test_error_categories(self):
        self.assertEqual(downloader.classify('HTTP 429 Too Many Requests'), 'rate_limited')
        self.assertEqual(downloader.classify('checkpoint challenge required'), 'verification_required')
        self.assertEqual(downloader.classify('network failure'), 'download_failed')

    def test_public_download_handoff_with_simulated_extractor(self):
        real_run = downloader.run
        def simulated(args, **kwargs):
            if Path(args[0]).name == 'yt-dlp':
                output = Path(args[args.index('--output') + 1].replace('%(ext)s', 'mp4'))
                downloader.shutil.copyfile(self.fixture, output)
                return subprocess.CompletedProcess(args, 0, '', '')
            return real_run(args, **kwargs)
        with patch.object(downloader, 'run', side_effect=simulated):
            saved, media, cached = downloader.acquire(URL, self.root)
        self.assertTrue(media.exists())
        self.assertEqual(saved['method'], 'yt-dlp-public')
        self.assertTrue(saved['media']['full_decode_passed'])
        self.assertFalse(cached)


if __name__ == '__main__':
    unittest.main()
