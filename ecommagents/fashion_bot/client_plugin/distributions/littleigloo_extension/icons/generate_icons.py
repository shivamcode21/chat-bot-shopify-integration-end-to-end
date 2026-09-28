"""
Generate simple icon files for the Chrome extension.
Run: python generate_icons.py
"""

import os
import struct
import zlib


def create_simple_icon(size, filename):
    def png_chunk(chunk_type, data):
        chunk = chunk_type + data
        return struct.pack('>I', len(data)) + chunk + struct.pack('>I', zlib.crc32(chunk) & 0xffffffff)

    signature = b'\x89PNG\r\n\x1a\n'
    ihdr_data = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)
    ihdr = png_chunk(b'IHDR', ihdr_data)

    raw_data = b''
    for y in range(size):
        raw_data += b'\x00'
        for x in range(size):
            # Purple gradient (matching #6C5CE7 primary color)
            r = 108
            g = int(92 + 30 * (y / size))
            b = 231
            raw_data += bytes([r, g, b])

    idat = png_chunk(b'IDAT', zlib.compress(raw_data, 9))
    iend = png_chunk(b'IEND', b'')

    with open(filename, 'wb') as f:
        f.write(signature + ihdr + idat + iend)
    print("Created " + filename)


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for sz in (16, 48, 128):
        create_simple_icon(sz, os.path.join(script_dir, f'icon{sz}.png'))
    print("\nAll icons generated!")
