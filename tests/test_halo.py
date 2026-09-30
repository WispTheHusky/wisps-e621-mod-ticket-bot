from support import fixture_config
import unittest
from unittest.mock import patch
import ticket_bot as geometry
import ticket_bot as windows


class GeometryTests(unittest.TestCase):
    def test_official_logo_colors(self):
        self.assertEqual(geometry.PALETTE[0], (1, 84, 157))
        self.assertEqual(geometry.PALETTE[-1], (252, 191, 49))

    def test_ring_covers_perimeter_without_filling_centre(self):
        for w, h, thickness in ((1920, 1080, 28), (2560, 1440, 42), (1080, 1920, 35)):
            result = geometry.bands(w, h, thickness)
            self.assertEqual(len(result), 40)
            area = 0
            for (x, y, bw, bh), alpha, plan in result:
                self.assertTrue(0 <= x < x+bw <= w and 0 <= y < y+bh <= h)
                self.assertFalse(x <= w/2 < x+bw and y <= h/2 < y+bh)
                area += bw*bh
                self.assertEqual(sum((r-l)*(b-t) for (l,t,r,b), _ in plan), bw*bh)
                self.assertTrue(all(0 <= p <= 1 for _, p in plan))
            self.assertEqual(area, w*h - (w-2*thickness)*(h-2*thickness))

    def test_colors_travel_clockwise_and_repeat_after_one_turn(self):
        phases = [i/100 for i in range(100)]
        initial = [geometry.color_index(p, 0) for p in phases]
        moved = [geometry.color_index(p, 0.3) for p in phases]
        self.assertNotEqual(initial, moved)
        for p in phases:
            self.assertEqual(geometry.color_index(p+0.1, 0.3), geometry.color_index(p, 0))
            self.assertEqual(geometry.color_index(p, 3), geometry.color_index(p, 0))

    def test_all_displays_created_at_negative_origins_and_scaled_width(self):
        monitors = [(-1920, -200, 0, 880, 96), (0, 0, 2560, 1440, 144)]
        with patch('ticket_bot.all_monitors', return_value=monitors), \
             patch('ticket_bot.register_halo_classes'), \
             patch('ticket_bot.create_window', side_effect=range(1, 81)) as create, \
             patch('ticket_bot.layer_alpha'), patch('ticket_bot.set_pos'), \
             patch('ticket_bot.destroy_window') as destroy:
            halo = windows._BandedHalo(None, 3, 28)
            halo.show()
            self.assertEqual(len(halo.windows), 80)
            self.assertEqual(create.call_args_list[0].args[4:8], (-1920, -200, 1920, 3))
            self.assertEqual(create.call_args_list[40].args[4:8], (0, 0, 2560, 4))
            with patch('ticket_bot.time.monotonic', return_value=halo.started+3):
                halo.tick()
            self.assertFalse(halo.windows)
            self.assertFalse(halo.paint_plans)
            self.assertEqual(destroy.call_count, 80)

    def test_smooth_halo_uses_four_surfaces_per_monitor(self):
        from unittest.mock import Mock
        monitors=[(-1920,-200,0,880,96),(0,0,2560,1440,144)]
        with patch('ticket_bot.all_monitors',return_value=monitors),patch('ticket_bot.register_halo_classes'),patch('ticket_bot.LayeredSurface') as surface:
            surface.side_effect=[Mock(hwnd=i) for i in range(8)]
            halo=windows.Halo(None,3,28)
            halo.show()
            self.assertEqual(len(halo.windows),8)
            self.assertEqual(surface.call_args_list[0].args[:2],(-1920,-200))
            self.assertEqual(surface.call_args_list[4].args[2].rect,(0,0,2560,42))
            owned=list(halo.surfaces)
            halo.hide()
            for item in owned:item.close.assert_called_once()
            self.assertFalse(halo.windows)

    def test_gradient_is_per_pixel_and_edges_do_not_cover_centre(self):
        from ticket_bot import EdgeTexture
        width,height,t=400,240,40
        textures=[EdgeTexture(width,height,t,e) for e in range(4)]
        values=[textures[0].alpha.getpixel((width//2,y)) for y in range(t)]
        self.assertGreater(len(set(values)),30)
        self.assertEqual(values[-1],0)
        self.assertTrue(all(a>=b for a,b in zip(values,values[1:])))
        self.assertEqual(sum(w*h for tex in textures for x,y,w,h in [tex.rect]),width*height-(width-2*t)*(height-2*t))
        self.assertNotEqual(textures[0].frame(0),textures[0].frame(.5))


if __name__ == '__main__':
    unittest.main()
