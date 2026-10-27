"""Offline regression tests for verified Threads shared video resolution."""
import sys
import types
import unittest
from pathlib import Path

# Load the pure parsing helpers without requiring the bundled executable's Python.
source = (Path(__file__).parents[1] / 'Plugins/vitract/yt_dlp_plugins/extractor/vitract_threads.py').read_text()
module = types.ModuleType('threads_helpers')
exec(source.split('from yt_dlp.extractor.common import InfoExtractor')[0], module.__dict__)
BASE = 'https://www.threads.com/@test/post/ABCDE123'
CARD = '<a href="' + BASE + '">post</a><div class="LinkAttachmentOuterContainer"><a href="{}">shared</a></div>'

class ThreadsTests(unittest.TestCase):
    def test_verified_card(self):
        info = module.parse_threads_embed(CARD.format('https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.instagram.com%2Fp%2FABCDE99%2F'), BASE + '/media', 2)
        self.assertEqual(info['url'], 'https://www.instagram.com/p/ABCDE99/')
        self.assertEqual(info['vitract_video_index'], 2)

    def test_unrelated_links_not_followed(self):
        for link in ['https://instagram.com.evil.test/p/ABCDE99/', 'https://www.instagram.com/accounts/login/', 'http://www.instagram.com/p/ABCDE99/', 'https://user:pass@www.instagram.com/p/ABCDE99/', 'https://l.facebook.com/l.php?u=https://127.0.0.1/a']:
            with self.assertRaises(module.ThreadsError):
                module.parse_threads_embed(CARD.format(link), BASE)
        with self.assertRaises(module.ThreadsError):
            module.parse_threads_embed(CARD.format('https://www.instagram.com/p/ABCDE99/').replace('LinkAttachmentOuterContainer','BodyTextContainer'), BASE)

    def test_identity_required(self):
        with self.assertRaises(module.ThreadsError):
            module.parse_threads_embed(CARD.format('https://www.instagram.com/p/ABCDE99/').replace('ABCDE123','OTHER12'), BASE)

    def test_direct_video_keeps_priority(self):
        html = CARD.format('https://www.instagram.com/p/ABCDE99/') + '<video src="https://a.cdninstagram.com/video.mp4"></video>'
        info = module.parse_threads_embed(html, BASE)
        self.assertEqual(info['formats'][0]['url'], 'https://a.cdninstagram.com/video.mp4')
        self.assertNotIn('_type', info)

    def test_mixed_carousel_filters_photos(self):
        first = {'id':'first', 'formats':[{'url':'https://a.cdninstagram.com/first.mp4'}]}
        second = {'id':'second', 'formats':[{'url':'https://a.cdninstagram.com/second.mp4'}]}
        raw = {'_type':'playlist','entries':[{'id':'photo','formats':[]}, first, second]}
        info = module.select_shared_video(raw, 2)
        self.assertEqual(info['id'], 'second')
        self.assertEqual(info['vitract_video_count'], 2)
        with self.assertRaises(module.ThreadsError): module.select_shared_video(raw, 3)
        with self.assertRaises(module.ThreadsError): module.select_shared_video({'formats':[]}, 1)

if __name__ == '__main__': unittest.main()
