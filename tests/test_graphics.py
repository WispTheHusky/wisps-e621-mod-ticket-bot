from support import app as bot
import argparse
import io
from pathlib import Path
import unittest
from unittest.mock import patch, Mock


class GraphicsTests(unittest.TestCase):
    def test_waiting_dot_is_solid_antialiased_and_pulses_while_spinner_is_hollow(self):
        bot.configure_dashboard_dpi()
        root = bot.tk.Tk(); root.withdraw()
        self.addCleanup(root.destroy)
        canvas = bot.tk.Canvas(root)
        indicator = bot.SmoothIndicator(canvas, 30, 30, 32)
        indicator.update('dot', 0, 30, 30)
        dot = bot.ImageTk.getimage(indicator.current)
        self.assertEqual(canvas.type(indicator.item), 'image')
        self.assertGreater(dot.getpixel((16, 16))[3], 240)
        self.assertTrue(any(0 < p < 255 for p in dot.getchannel('A').tobytes()))
        indicator.update('dot', .375, 30, 30)
        self.assertNotEqual(dot.tobytes(), bot.ImageTk.getimage(indicator.current).tobytes())
        indicator.update('spinner', .2, 30, 30)
        self.assertEqual(bot.ImageTk.getimage(indicator.current).getpixel((16, 16))[3], 0)

    def test_fixture_avatar_decodes_to_a_circular_tk_image(self):
        fixture = bot.Image.new('RGB', (90, 70), '#418DD1')
        raw = io.BytesIO(); fixture.save(raw, format='PNG')
        png = bot.decode_thumbnail(raw.getvalue())
        image = bot.Image.open(io.BytesIO(png))
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertEqual(image.getpixel((32, 32))[3], 255)
        root = bot.tk.Tk(); root.withdraw()
        self.addCleanup(root.destroy)
        photo = bot.tk.PhotoImage(master=root, data=png)
        self.assertEqual((photo.width(), photo.height()), (64, 64))

    def test_missing_graphics_cannot_start_app_or_silently_use_degraded_renderers(self):
        with patch.object(bot, 'Image', None), patch.object(bot, 'ImageTk', None), \
                patch.object(bot, 'InstanceLock') as lock, patch.object(bot, '_BandedHalo') as fallback:
            with self.assertRaises(bot.GraphicsUnavailable): bot.App({}, argparse.Namespace())
            with self.assertRaises(bot.GraphicsUnavailable): bot.SmoothIndicator(Mock(), 0, 0, 24)
            with self.assertRaises(bot.GraphicsUnavailable): bot.Halo(None, 3, 28).show()
            lock.assert_not_called()
            fallback.assert_not_called()

    def test_setup_command_targets_opening_interpreter_and_quotes_apostrophes(self):
        with patch.object(bot.sys, 'executable', r"C:\Moderator's Python\pythonw.exe"):
            command = bot.graphics_install_command()
        self.assertTrue(command.startswith("& 'C:\\Moderator''s Python\\python.exe' -m pip "))
        self.assertIn('--index-url https://pypi.org/simple Pillow', command)

    def test_setup_retry_preserves_screen_until_graphics_are_available(self):
        screen = bot.GraphicsSetup()
        try:
            screen.root.update()
            with patch.object(bot, 'load_graphics', return_value=False): screen.retry()
            self.assertFalse(screen.ready)
            self.assertIn('unavailable', screen.feedback.cget('text'))
            with patch.object(bot, 'load_graphics', return_value=True): screen.retry()
            self.assertTrue(screen.ready)
        finally:
            try: screen.root.destroy()
            except bot.tk.TclError: pass
