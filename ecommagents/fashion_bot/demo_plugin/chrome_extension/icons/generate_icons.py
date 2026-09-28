"""
Generate simple icon files for the Chrome extension
Run: python generate_icons.py
"""

import base64
import os

# Simple 1-pixel cyan PNG as base64 (we'll scale it)
# This creates a simple colored icon

def create_simple_icon(size, filename):
    """Create a simple colored square icon using pure Python"""
    
    # PNG header and IHDR chunk
    def png_chunk(chunk_type, data):
        import struct
        import zlib
        chunk = chunk_type + data
        return struct.pack('>I', len(data)) + chunk + struct.pack('>I', zlib.crc32(chunk) & 0xffffffff)
    
    import struct
    import zlib
    
    # PNG signature
    signature = b'\x89PNG\r\n\x1a\n'
    
    # IHDR chunk
    width = size
    height = size
    bit_depth = 8
    color_type = 2  # RGB
    ihdr_data = struct.pack('>IIBBBBB', width, height, bit_depth, color_type, 0, 0, 0)
    ihdr = png_chunk(b'IHDR', ihdr_data)
    
    # IDAT chunk (image data)
    # Create cyan colored image with gradient effect
    raw_data = b''
    for y in range(height):
        raw_data += b'\x00'  # Filter byte
        for x in range(width):
            # Create a simple gradient/pattern
            r = 0
            g = int(200 + 55 * (y / height))  # Gradient
            b = 255
            raw_data += bytes([r, g, b])
    
    compressed = zlib.compress(raw_data, 9)
    idat = png_chunk(b'IDAT', compressed)
    
    # IEND chunk
    iend = png_chunk(b'IEND', b'')
    
    # Write PNG file
    with open(filename, 'wb') as f:
        f.write(signature + ihdr + idat + iend)
    
    print("Created " + filename)

if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    create_simple_icon(16, os.path.join(script_dir, 'icon16.png'))
    create_simple_icon(48, os.path.join(script_dir, 'icon48.png'))
    create_simple_icon(128, os.path.join(script_dir, 'icon128.png'))
    
    print("\n✅ All icons generated!")
    print("You can replace these with custom icons later.")

