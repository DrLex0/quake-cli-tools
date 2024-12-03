"""Command line utility for creating and manipulating WAD files

Supported Games:
    - QUAKE
"""

import argparse
import io
import os
import struct
import sys

from PIL import Image

from vgio import quake
from vgio.quake import lmp, wad

import qcli
from qcli.common import Parser, ResolvePathAction, read_from_stdin


def has_fullbright(img):
    """Given img in P mode with Quake palette, return whether it contains
    at least 1 fullbright pixel or not"""
    for y in range(0, img.height):
        for x in range(0, img.width):
            if img.getpixel((x, y)) > 223:
                return True


def main():
    """CLI entrypoint"""

    # Create and configure argument parser
    parser = Parser(
        prog='wad',
        description='Default action is to add or replace wad file entries from'
            ' list.\nIf list is omitted, wad will use stdin.',
        formatter_class=argparse.RawTextHelpFormatter,
        epilog='example:\n  wad tex.wad image.png => adds image.png to tex.wad'
    )

    parser.add_argument(
        'file',
        metavar='file.wad',
        action=ResolvePathAction,
        help='wad file to add entries to'
    )

    parser.add_argument(
        'list',
        nargs='*',
        action=ResolvePathAction,
        default=read_from_stdin()
    )

    parser.add_argument(
        '-t', '--type',
        dest='type',
        default='MIPTEX',
        choices=['LUMP', 'QPIC', 'MIPTEX'],
        help='list data type [default: MIPTEX]'
    )

    parser.add_argument(
        '-r', '--raw-indexed',
        dest='raw_color_mode',
        action='store_true',
        help='for images that use indexed color, assume they have the Quake color palette and skip RGB conversion; avoids color shifts'
    )

    parser.add_argument(
        '-s', '--smooth-mip',
        dest='smooth_mip',
        action='store_true',
        help='smooth mipmap scaling, looks better in game engines that still rely on mipmaps in the BSP'
    )

    parser.add_argument(
        '-S', '--smart-mip',
        dest='smart_mip',
        action='store_true',
        help='smart mipmap scaling, uses smooth scaling if texture has no fullbright pixels, nearest neighbor otherwise. Only reliable in combination with -r.'
    )

    parser.add_argument(
        '-q', '--quiet',
        dest='quiet',
        action='store_true',
        help='quiet mode'
    )

    parser.add_argument(
        '-v', '--version',
        dest='version',
        action='version',
        version='{} version {}'.format(parser.prog, qcli.__version__)
    )

    # Parse the arguments
    args = parser.parse_args()

    if not args.list:
        parser.error('the following arguments are required: list')

    if args.quiet:
        def log(message):
            pass

    else:
        def log(message):
            print(message)

    # Ensure directory structure
    dir = os.path.dirname(args.file) or '.'
    os.makedirs(dir, exist_ok=True)

    filemode = 'a'
    if not os.path.isfile(args.file):
        filemode = 'w'

    with wad.WadFile(args.file, filemode) as wad_file:
        log(f'Archive: {os.path.basename(args.file)}')

        # Flatten out palette
        palette = []
        for p in quake.palette:
            palette += p

        # Create palette image for Image.quantize()
        palette_image = Image.frombytes('P', (16, 16), bytes(palette))
        palette_image.putpalette(palette)

        # Same for palette without fullbright
        palette_nofb = palette[:-96] + [0] * 96
        palette_image_nofb = Image.frombytes('P', (16, 16), bytes(palette_nofb))
        palette_image_nofb.putpalette(palette_nofb)

        # Process input files
        for file in args.list:
            if args.type == 'LUMP':
                log(f'  adding: {file}')
                wad_file.write(file)

            elif args.type == 'QPIC':
                img = Image.open(file)
                if img.mode != 'P' or not args.raw_color_mode:
                    img = img.convert(mode='RGB').quantize(palette=palette_image)
                pixels = img.tobytes()
                name = os.path.basename(file).split('.')[0]

                qpic = lmp.Lmp()
                qpic.width = img.width
                qpic.height = img.height
                qpic.pixels = pixels

                buff = io.BytesIO()
                lmp.Lmp.write(buff, qpic)
                file_size = buff.tell()
                buff.seek(0)

                info = wad.WadInfo(name)
                info.file_size = file_size
                info.disk_size = info.file_size
                info.compression = wad.CompressionType.NONE
                info.type = wad.LumpType.QPIC

                log(f'  adding: {file}')

                wad_file.writestr(info, buff)

            else:
                try:
                    img = Image.open(file)
                    img_rgb = img.convert(mode='RGB')
                    if img.mode != 'P' or not args.raw_color_mode:
                        img = img_rgb.quantize(palette=palette_image)
                    has_fb = has_fullbright(img)

                    name = os.path.basename(file).split('.')[0]

                    mip = wad.Miptexture()
                    mip.name = name
                    mip.width = img.width
                    mip.height = img.height
                    mip.offsets = [40]
                    mip.pixels = []

                    # Build mip maps
                    smooth_scaling = args.smooth_mip or (args.smart_mip and not has_fb)
                    for i in range(4):
                        if i > 0:
                            if smooth_scaling:
                                # If original had no FB pixels, ensure we won't
                                # introduce any with the smooth scaling
                                target_palette = palette_image if has_fb else palette_image_nofb
                                # resizing RGB image will benefit from smoothing and filtering
                                resized_image = img_rgb.resize(
                                    (img.width // pow(2, i), img.height // pow(2, i))
                                ).quantize(palette=target_palette)
                            else:
                                # resizing quantized img will use nearest-neighbor
                                resized_image = img.resize(
                                    (img.width // pow(2, i), img.height // pow(2, i))
                                )
                            data = resized_image.tobytes()
                        else:
                            data = img.tobytes()
                        mip.pixels += struct.unpack(f'<{len(data)}B', data)
                        if i < 3:
                            mip.offsets += [mip.offsets[-1] + len(data)]

                    buff = io.BytesIO()
                    wad.Miptexture.write(buff, mip)
                    buff.seek(0)

                    info = wad.WadInfo(name)
                    info.file_size = 40 + len(mip.pixels)
                    info.disk_size = info.file_size
                    info.compression = wad.CompressionType.NONE
                    info.type = wad.LumpType.MIPTEX

                    log(f'  adding: {file}')

                    wad_file.writestr(info, buff)

                except:
                    parser.error(sys.exc_info()[1])

    sys.exit(0)


if __name__ == '__main__':
    main()
