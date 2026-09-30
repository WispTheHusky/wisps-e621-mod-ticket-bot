import support  # Load the canonical windowed source for the test runner.
from ticket_bot import asset_bytes
import struct
import unittest


class IconTests(unittest.TestCase):
    def test_icon_contains_required_windows_sizes(self):
        data = asset_bytes('assets/e621-ticket-bot.ico')
        reserved, kind, count = struct.unpack_from('<HHH', data)
        self.assertEqual((reserved, kind), (0, 1))
        sizes = set()
        for i in range(count):
            w,h,_,_,_,_,length,offset = struct.unpack_from('<BBBBHHII', data, 6+16*i)
            sizes.add((w or 256, h or 256))
            self.assertLessEqual(offset+length, len(data))
            self.assertGreater(length, 0)
        self.assertTrue({(n,n) for n in (16,20,24,32,48,256)} <= sizes)

    def test_preview_is_256_pixel_png(self):
        data = asset_bytes('assets/e621-ticket-bot.png')
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack_from('>II', data, 16), (256,256))
