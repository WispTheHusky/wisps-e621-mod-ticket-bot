from support import app as bot,SOURCE
import ast
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root=Path(self.folder.name)
        self.source=root/'source';self.source.mkdir()
        self.data=root/'profile'
        for name,value in [('DATA',self.data),('ROOT',self.source)]:
            p=patch.object(bot,name,value);p.start();self.addCleanup(p.stop)

    def test_fresh_install_has_neutral_defaults_and_no_key_lookup(self):
        with patch('ticket_bot.read_key') as keys:
            config=bot.load_config()
        keys.assert_not_called()
        self.assertEqual(config['username'],'')
        self.assertEqual(config['poll_seconds'],5)
        self.assertFalse((self.data/'state.json').exists())
        self.assertNotIn('sound_path',config)
        self.assertEqual(list(self.source.iterdir()),[])

    def test_only_explicit_installation_data_is_loaded(self):
        legacy=dict(bot.DEFAULT_CONFIG,username='ExistingModerator',poll_seconds=37,sound_enabled=False)
        old=self.source/'config.json';old.write_text(json.dumps(legacy))
        self.data.mkdir();history=self.data/'state.json'
        history.write_bytes(b'synthetic opaque existing history')
        with patch('ticket_bot.read_key') as keys,patch('ticket_bot.save_key') as save:
            config=bot.load_config()
        self.assertEqual(json.loads((self.data/'config.json').read_text()),bot.DEFAULT_CONFIG)
        self.assertEqual(config['username'],'')
        self.assertEqual(history.read_bytes(),b'synthetic opaque existing history')
        self.assertTrue(old.exists())
        keys.assert_not_called();save.assert_not_called()
        bot.save_settings(poll_seconds=55)
        self.assertEqual(bot.load_config()['poll_seconds'],55)

    def test_resource_cache_repairs_modified_helpers_without_touching_source(self):
        path=bot.resource_path('toast.ps1')
        path.write_bytes(b'corrupt synthetic cache')
        self.assertEqual(bot.resource_path('toast.ps1').read_bytes(),bot.asset_bytes('toast.ps1'))
        self.assertFalse((self.source/'toast.ps1').exists())
        with self.assertRaises(KeyError):bot.resource_path('../outside.txt')

    def test_generated_sound_is_three_seconds_without_external_wav(self):
        with wave.open(io.BytesIO(bot.chime_bytes())) as wav:
            self.assertEqual(wav.getnframes()/wav.getframerate(),3)
            self.assertEqual(wav.getnchannels(),1)
            self.assertEqual(wav.getsampwidth(),2)
            self.assertNotEqual(set(wav.readframes(wav.getnframes())),{0})
        self.assertFalse(any(name.endswith('.wav') for name in bot._EMBEDDED_ASSETS))
        self.assertFalse(list(self.data.rglob('*.wav')))

    def test_source_has_no_internal_imports_or_dynamic_source_execution(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
        removed={'bot','core','windows','flow','avatar','halo_geometry','halo_surface','compact_dashboard','indicators','dashboard'}
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):self.assertFalse({n.name for n in node.names}&removed)
            if isinstance(node,ast.ImportFrom):self.assertNotIn(node.module,removed)
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name):self.assertNotIn(node.func.id,('exec','eval'))
        text=SOURCE.read_text(encoding='utf-8')
        self.assertNotIn('WispTheHusky',text)
        self.assertNotIn('codex-runtimes',text)

    def test_single_file_copy_runs_from_empty_folder_with_fresh_profile(self):
        copy=self.source/'ticket_bot.pyw';shutil.copy2(SOURCE,copy)
        script=r'''
import importlib.machinery,importlib.util,sys
from unittest.mock import patch
loader=importlib.machinery.SourceFileLoader('portable',sys.argv[1])
spec=importlib.util.spec_from_loader(loader.name,loader)
app=importlib.util.module_from_spec(spec);loader.exec_module(app)
with patch.object(app,'read_key',side_effect=AssertionError('Unexpected credential lookup')),patch.object(app,'InstanceLock'),patch.object(app,'StopSignal') as signal,patch.object(app.urllib.request,'build_opener',side_effect=AssertionError('Unexpected network')):
    signal.return_value.is_set.return_value=False
    config=app.load_config()
    assert config['username']==''
    args=app.argparse.Namespace(test=False,background=False,delay=3600,simulate_fullscreen=0)
    ui=app.App(config,args);ui.root.update()
    assert ui.dashboard.refresh_indicator.smooth
    assert not ui.authenticated and not ui.schedule.enabled
    ui.show_login();ui.root.update()
    assert ui.dashboard.login_username.get()==''
    assert ui.dashboard.login_key.cget('show')
    assert ui.dashboard.submit_button.cget('text')=='Submit'
    ui.window_controls.detach();ui.root.destroy()
    print('portable UI passed')
'''
        env=dict(bot.os.environ,LOCALAPPDATA=str(self.data))
        run=subprocess.run([sys.executable,'-c',script,str(copy)],cwd=self.source,env=env,capture_output=True,text=True,timeout=15,creationflags=0x08000000)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn('portable UI passed',run.stdout)
        self.assertTrue((self.source/'data'/'config.json').exists())
        self.assertFalse((self.data/'E621TicketBot').exists())

    def test_missing_pillow_stays_on_setup_without_loading_account_or_settings(self):
        copy=self.source/'ticket_bot.pyw';shutil.copy2(SOURCE,copy)
        script=r'''
import importlib.machinery,importlib.util,sys
from unittest.mock import patch
loader=importlib.machinery.SourceFileLoader('bare',sys.argv[1]);spec=importlib.util.spec_from_loader(loader.name,loader)
app=importlib.util.module_from_spec(spec);loader.exec_module(app)
assert app.Image is None and app.ImageTk is None
real_setup=app.GraphicsSetup
def setup():
    screen=real_setup();screen.root.update()
    assert screen.retry_button.cget('text')=='Check Again'
    assert screen.copy_button.cget('text')=='Copy Setup Command'
    assert not any(w.winfo_class()=='Toplevel' for w in screen.root.winfo_children())
    screen.retry();assert not screen.ready and 'unavailable' in screen.feedback.cget('text')
    screen.root.after(50,screen.root.destroy)
    return screen
with patch.object(app,'GraphicsSetup',side_effect=setup),patch.object(app,'App',side_effect=AssertionError('No dashboard')),patch.object(app,'load_config',side_effect=AssertionError('No settings')),patch.object(app,'read_key',side_effect=AssertionError('No keys')),patch.object(app.urllib.request,'build_opener',side_effect=AssertionError('No network')),patch.object(sys,'argv',[str(sys.argv[1])]):
    assert app.main()==1
print('missing graphics blocked before account loading')
'''
        env=dict(bot.os.environ,LOCALAPPDATA=str(self.data))
        run=subprocess.run([sys.executable,'-S','-c',script,str(copy)],cwd=self.source,env=env,capture_output=True,text=True,timeout=15,creationflags=0x08000000)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn('missing graphics blocked',run.stdout)
        self.assertFalse((self.data/'E621TicketBot'/'config.json').exists())

    def test_pythonw_has_no_console(self):
        probe=self.source/'probe.pyw';result=self.source/'result.json'
        probe.write_text("import ctypes,json,pathlib; k=ctypes.WinDLL('kernel32'); k.GetConsoleWindow.restype=ctypes.c_void_p; pathlib.Path(__file__).with_name('result.json').write_text(json.dumps({'console':bool(k.GetConsoleWindow())}))")
        run=subprocess.run([str(Path(sys.executable).with_name('pythonw.exe')),str(probe)],timeout=5)
        self.assertEqual(run.returncode,0)
        self.assertFalse(json.loads(result.read_text())['console'])
