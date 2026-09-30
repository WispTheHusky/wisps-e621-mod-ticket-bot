from support import fixture_config
import io
import json
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import urllib.request
from ticket_bot import PollSchedule,queue_message
from ticket_bot import State,NoRedirect
from ticket_bot import fit_text
from ticket_bot import fetch_avatar,validate_avatar_request,AvatarUnavailable,decode_thumbnail,read_public


class ScheduleTests(unittest.TestCase):
    def test_grammar(self):
        self.assertEqual(queue_message(0),'Queue currently has 0 tickets.')
        self.assertEqual(queue_message(1),'Queue currently has 1 ticket.')
        self.assertEqual(queue_message(3),'Queue currently has 3 tickets.')

    def test_fast_scan_minimum_one_second_and_dot_cycle(self):
        s=PollSchedule()
        s.begin(100)
        for offset,dots in ((0,''),(.125,'.'),(.25,'..'),(.375,'...'),(.5,'')):
            self.assertEqual(s.presentation(100+offset)[0],'Scanning queue'+dots)
        s.finish(100.1)
        self.assertEqual(s.presentation(100.99)[2],'scanning')
        text,fraction,phase=s.presentation(101)
        self.assertEqual(text,'Next refresh in 5s')
        self.assertEqual(phase,'waiting')
        self.assertEqual(fraction,1)
        self.assertAlmostEqual(s.presentation(103.05)[1],.5)
        self.assertEqual(s.presentation(105.1)[1],0)
        self.assertEqual(s.presentation(105.1)[0],'Next refresh in 0s')
        self.assertFalse(s.ready(105))
        self.assertTrue(s.ready(105.1))

    def test_slow_scan_full_reverse_bar_then_drain(self):
        s=PollSchedule()
        s.begin(10)
        self.assertEqual(s.presentation(14)[2],'scanning')
        s.finish(15)
        self.assertEqual(s.presentation(15)[1],1)
        self.assertEqual(s.presentation(17.5)[1],.5)
        self.assertEqual(s.presentation(20)[1],0)
        self.assertFalse(s.ready(19.99))
        self.assertTrue(s.ready(20))

    def test_fast_backoff_visible_bar_preserves_retry_deadline(self):
        s=PollSchedule()
        s.begin(100)
        s.finish(100.1,retry_delay=120)
        self.assertEqual(s.presentation(100.99)[2],'scanning')
        self.assertEqual(s.presentation(101),('Retry in 120s',1,'backoff'))
        self.assertAlmostEqual(s.presentation(160.55)[1],.5)
        self.assertEqual(s.deadline,220.1)
        self.assertFalse(s.ready(220))
        self.assertEqual(s.presentation(220.1)[1],0)
        self.assertTrue(s.ready(220.1))

    def test_pause_inflight_settles_without_reentry(self):
        s=PollSchedule()
        s.begin(10)
        s.toggle_pause(10.1)
        self.assertEqual(s.presentation(10.2)[2],'paused')
        self.assertFalse(s.begin(11))
        s.finish(12)
        self.assertFalse(s.ready(30))
        s.toggle_pause(30)
        self.assertTrue(s.ready(35))

    def test_interval_and_resume_never_shorten_backoff(self):
        s=PollSchedule()
        s.begin(10)
        s.finish(11,retry_delay=120)
        s.set_interval(5,20)
        self.assertEqual(s.deadline,131)
        s.toggle_pause(30);s.toggle_pause(31)
        self.assertEqual(s.deadline,132)
        self.assertAlmostEqual(s.presentation(31)[1],101/120)
        s.start_test();s.end_test(50)
        self.assertEqual(s.deadline,131)
        self.assertEqual(s.presentation(50)[1],1)
        self.assertEqual(s.presentation(90.5)[1],.5)

    def test_pause_freezes_four_seconds_and_fraction_until_resume(self):
        s=PollSchedule(30)
        s.begin(0);s.finish(3)
        self.assertAlmostEqual(s.presentation(29)[1],4/30)
        s.toggle_pause(29)
        self.assertAlmostEqual(s.presentation(100)[1],4/30)
        s.toggle_pause(100)
        self.assertEqual(s.deadline,104)
        self.assertAlmostEqual(s.presentation(100)[1],4/30)
        self.assertEqual(s.presentation(104)[1],0)

    def test_interval_change_while_paused_resets_frozen_wait(self):
        s=PollSchedule(30)
        s.begin(0);s.finish(3);s.toggle_pause(29)
        s.set_interval(10,40)
        self.assertEqual(s.presentation(60)[1],1)
        s.toggle_pause(100)
        self.assertEqual(s.deadline,110)

    def test_test_prior_pause_and_terminal_cannot_resume(self):
        s=PollSchedule()
        s.paused=True
        self.assertTrue(s.start_test())
        self.assertFalse(s.start_test())
        s.end_test(10)
        self.assertTrue(s.paused)
        s.paused=False
        s.finish(10,terminal=True)
        s.start_test();s.end_test(20);s.toggle_pause(21)
        self.assertFalse(s.ready(100))
        self.assertEqual(s.presentation(100)[2],'stopped')

    def test_normal_interval_edits_reanchor_without_flood(self):
        s=PollSchedule()
        s.set_interval(30,10)
        self.assertEqual(s.deadline,40)
        s.set_interval(5,12)
        self.assertEqual(s.deadline,17)
        for value in (0,4,3601,5.5,True):
            with self.assertRaises(ValueError):s.set_interval(value,20)


class HistoryTests(unittest.TestCase):
    def test_last_ten_real_persist_and_baseline_has_no_fake_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json'
            s=State(path,'TestUser')
            s.accept({1},{1:'baseline'})
            self.assertEqual(s.history,[])
            for i in range(2,15):s.accept({i},{i:f'real synthetic fixture {i}'})
            loaded=State(path,'TestUser')
            self.assertEqual([r[0] for r in loaded.history],list(range(14,4,-1)))
            self.assertEqual(loaded.seen,set(range(1,15)))
            self.assertEqual(loaded.last_ticket_found,s.last_ticket_found)

    def test_font_fitting_is_bounded_literal_dots(self):
        for text in ('a'*10000,'many\nlines\tand words', '漢字'*500):
            fitted=fit_text(text,lambda s:len(s)*7,100)
            self.assertLessEqual(len(fitted)*7,100)
            self.assertTrue(fitted.endswith('...'))
            self.assertNotIn('\n',fitted)


class AvatarTests(unittest.TestCase):
    def response(self,data,mime='application/json'):
        r=Mock();r.status=200;r.headers={'Content-Type':mime}
        r.read.return_value=json.dumps(data).encode() if not isinstance(data,bytes) else data
        r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False)
        return r

    def test_public_avatar_flow_never_sends_credentials(self):
        opener,stop=Mock(),Mock();stop.wait.return_value=False
        url='https://static1.e621.net/data/preview/ab/cd/'+'a'*32+'.jpg'
        opener.open.side_effect=[self.response({'id':4,'name':'TestUser','avatar_id':42}),
                                 self.response({'post':{'id':42,'preview':{'url':url},'flags':{}}}),self.response(b'image','image/jpeg')]
        with patch('ticket_bot.decode_thumbnail',return_value=b'png'):
            image,note=fetch_avatar('TestUser',stop,opener)
        self.assertEqual(image,b'png')
        self.assertEqual(len(opener.open.call_args_list),3)
        for call in opener.open.call_args_list:
            request=call.args[0]
            self.assertEqual(request.get_method(),'GET')
            self.assertFalse(any(k.lower()=='authorization' for k in request.headers))
        self.assertEqual(opener.open.call_args_list[1].args[0].full_url,'https://e621.net/posts/42.json')

    def test_cropped_avatar_uses_validated_origin(self):
        opener,stop=Mock(),Mock();stop.wait.return_value=False
        url='https://static1.e621.net/data/preview/'+'a'*32+'.jpg'
        opener.open.side_effect=[self.response({'id':4,'name':'TestUser','avatar_id':42,'has_cropped_avatar':True}),self.response({'post':{'id':42,'preview':{'url':url}}}),self.response(b'image','image/jpeg')]
        with patch('ticket_bot.decode_thumbnail',return_value=b'png'):fetch_avatar('TestUser',stop,opener)
        self.assertEqual(opener.open.call_args_list[-1].args[0].full_url,'https://static1.e621.net/data/avatars/4.jpg')

    def test_media_rejects_external_redirect_auth_query_and_mutation(self):
        good='https://static1.e621.net/data/avatars/4.jpg'
        for url in ('http://static1.e621.net/data/avatars/4.jpg',good+'?auth=secret',good.replace('static1.e621.net','evil.test'),good.replace('/data/avatars/4.jpg','/file.exe')):
            with self.assertRaises(AvatarUnavailable):validate_avatar_request(urllib.request.Request(url),url,True)
        for req in (urllib.request.Request(good,method='POST'),urllib.request.Request(good,headers={'Authorization':'test'})):
            with self.assertRaises(AvatarUnavailable):validate_avatar_request(req,good,True)
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},good))

    def test_wrong_profile_identity_never_fetches_avatar(self):
        opener,stop=Mock(),Mock();opener.open.return_value=self.response({'id':1,'name':'OtherUser','avatar_id':5})
        data,_=fetch_avatar('TestUser',stop,opener)
        self.assertIsNone(data);self.assertEqual(opener.open.call_count,1)

    def test_actual_decoder_accepts_small_png_and_rejects_bad_data(self):
        from PIL import Image
        out=io.BytesIO();Image.new('RGB',(20,20),'blue').save(out,'PNG')
        self.assertTrue(decode_thumbnail(out.getvalue()).startswith(b'\x89PNG'))
        with self.assertRaises(Exception):decode_thumbnail(b'not an image')

    def test_bounded_media_size_and_content_type(self):
        url='https://static1.e621.net/data/avatars/4.jpg'
        opener=Mock()
        opener.open.return_value=self.response(b'x'*2_000_001,'image/jpeg')
        with self.assertRaises(AvatarUnavailable):read_public(url,'TestUser',opener,True)
        opener.open.return_value=self.response(b'<svg/>','image/svg+xml')
        with self.assertRaises(AvatarUnavailable):read_public(url,'TestUser',opener,True)
