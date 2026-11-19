import io
from pathlib import Path
import struct
import tempfile
import unittest
from PIL import Image
from deskpet_platform.role_archive import ROLE1_BURST_AUDIO, ROLE1_OPTIONAL_FILES, validate_photo_motion, validate_resource_size

class PhotoMotionTest(unittest.TestCase):
    def test_frames_must_match_geometry_index_and_role(self):
        image=io.BytesIO()
        Image.new('RGB',(480,320)).save(image,'JPEG')
        jpeg=image.getvalue()
        def pack(frames):
            ends=[12+4*(1+len(frames))]
            for chunk in frames: ends.append(ends[-1]+len(chunk))
            return struct.pack('<4s4H',b'RSC1',480,320,1,1)+struct.pack('<3I',*ends)+b''.join(frames)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'scene-frames.bin'
            path.write_bytes(pack([jpeg,jpeg]))
            self.assertFalse(validate_photo_motion(path,1,1))
            with self.assertRaises(ValueError): validate_photo_motion(path,2,1)
            path.write_bytes(pack([jpeg,b'broken']))
            with self.assertRaises(ValueError): validate_photo_motion(path,1,1)

    def test_timed_track_rejects_out_of_range_reference(self):
        image=io.BytesIO()
        Image.new('RGB',(320,480)).save(image,'JPEG')
        jpeg=image.getvalue()
        head=struct.pack('<4s4H4H',b'RSC2',480,320,1,1,0,1,50,1)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'scene-frames.bin'
            for index in (0,1):
                path.write_bytes(head+struct.pack('<H2I',index,30,30+len(jpeg))+jpeg)
                if index:
                    with self.assertRaises(ValueError):validate_photo_motion(path,1,1,1)
                else:self.assertTrue(validate_photo_motion(path,1,1,1))

    def test_music_asset_requires_complete_pcm_samples_and_a_bounded_size(self):
        limit = ROLE1_OPTIONAL_FILES[ROLE1_BURST_AUDIO]
        validate_resource_size(ROLE1_BURST_AUDIO, 88_000, limit)
        for size in (0, 88_001, limit + 2):
            with self.subTest(size=size), self.assertRaises(ValueError):
                validate_resource_size(ROLE1_BURST_AUDIO, size, limit)
