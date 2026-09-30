"""Compose captions around unmodified browser screenshots and encode a GIF."""
import base64
import hashlib
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

source = Path(sys.argv[1])
manifest = json.loads((source / 'frames.json').read_text(encoding='utf-8'))
font_candidates = [os.getenv('TOUR_FONT', ''), 'C:/Windows/Fonts/msyh.ttc',
                   '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
font_path = next((p for p in font_candidates if p and Path(p).exists()), None)
if not font_path:
    raise SystemExit('Set TOUR_FONT to a Chinese TrueType/OpenType font.')
font = lambda size: ImageFont.truetype(font_path, size)
width, ui_height, header, footer = 1280, 780, 106, 72
height = header + ui_height + footer

def compose(frame, poster=False):
    canvas = Image.new('RGB', (width, height), '#080d18')
    screenshot = Image.open(source / ('poster-source.png' if poster else frame['file'])).convert('RGB')
    assert screenshot.size == (width, ui_height)
    canvas.paste(screenshot, (0, header))
    draw = ImageDraw.Draw(canvas)
    draw.text((26, 13), 'STOCK KING', font=font(16), fill='#79a2ff')
    draw.text((171, 13), '投资研究工作台  /  实际界面操作演示', font=font(16), fill='#a9b7cf')
    draw.rounded_rectangle((1075, 14, 1254, 44), radius=8, fill='#1a263b')
    draw.text((1090, 19), 'DEMO · 历史样本', font=font(14), fill='#f0c476')
    title = '从自选到复盘，让研究有迹可循' if poster else f"0{frame['step'] + 1}  {frame['title']}"
    subtitle = '自选  →  图表  →  精选  →  原因  →  记录  →  复盘' if poster else frame['subtitle']
    draw.text((26, 47), title, font=font(25), fill='#f0f4fc')
    draw.text((470 if poster else 268, 57), subtitle, font=font(16), fill='#a9b7cf')
    draw.line((0, header - 1, width, header - 1), fill='#2a3a56', width=1)
    y = header + ui_height
    draw.line((0, y, width, y), fill='#2a3a56')
    for index, (label, _) in enumerate(manifest['steps']):
        left = 26 + index * 206
        active = index == frame['step'] and not poster
        draw.rounded_rectangle((left, y + 12, left + 187, y + 15), radius=2, fill='#6596ff' if active else '#25324a')
        draw.text((left, y + 21), f'{index + 1:02d} {label}', font=font(13), fill='#dce7fc' if active else '#91a0bb')
    draw.text((26, y + 47), '当前 Vue 界面 + 隔离演示数据 · 行情截至 2026-09-29 · 不代表实时行情、真实推荐或实际收益', font=font(12), fill='#b6a789')
    if frame.get('pointer') and not poster:
        x, py = frame['pointer']['x'], frame['pointer']['y'] + header
        if frame.get('click'):
            draw.ellipse((x - 18, py - 18, x + 18, py + 18), outline='#f0c476', width=3)
        draw.polygon([(x, py), (x + 1, py + 20), (x + 6, py + 15), (x + 10, py + 24), (x + 14, py + 22), (x + 10, py + 13), (x + 18, py + 13)], fill='white', outline='#182238')
    return canvas

frames, durations = [], []
last_pointer = {'x': 960, 'y': 400}
for frame in manifest['frames']:
    if frame.get('click'):
        target = frame['pointer']
        # A presentation cursor makes actual UI clicks easier to follow without
        # altering the underlying screenshot or inventing intermediate UI states.
        for ratio in (0.2, 0.4, 0.6, 0.8):
            tween = {**frame, 'click': False, 'pointer': {
                'x': last_pointer['x'] + (target['x'] - last_pointer['x']) * ratio,
                'y': last_pointer['y'] + (target['y'] - last_pointer['y']) * ratio}}
            frames.append(compose(tween)); durations.append(50)
        frames.append(compose(frame)); durations.append(frame['duration'] - 200)
        last_pointer = target
    else:
        frames.append(compose(frame)); durations.append(frame['duration'])
# One shared palette retains crisp dark UI text and allows unchanged pixels to compress.
atlas = Image.new('RGB', (640 * 4, 479 * ((len(frames) + 3) // 4)))
for index, frame in enumerate(frames):
    atlas.paste(frame.resize((640, 479)), ((index % 4) * 640, (index // 4) * 479))
palette = atlas.quantize(colors=192)
encoded = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
gif_buffer = BytesIO()
encoded[0].save(gif_buffer, format='GIF', save_all=True, append_images=encoded[1:], duration=durations, loop=0, optimize=True, disposal=1)
poster = compose(manifest['frames'][0], poster=True)
poster_buffer = BytesIO()
poster.save(poster_buffer, format='PNG', optimize=True)

def payload(name, target, data):
    return {'name': name, 'target': target, 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
            'base64': base64.b64encode(data).decode('ascii')}

# Transfer bytes through stdout. Some Windows file filters transform Python's
# binary file writes, leaving files that Python can read but Git cannot decode.
files = [payload('stockking-workspace-tour.gif', 'media', gif_buffer.getvalue()),
         payload('stockking-workspace-tour.png', 'media', poster_buffer.getvalue())]
for index in (0, 6, 8, 10, 12, 16, 18):
    frame = manifest['frames'][index]
    review_buffer = BytesIO()
    compose(frame).save(review_buffer, format='PNG')
    files.append(payload(f"review-{frame['file']}", 'review', review_buffer.getvalue()))
with Image.open(BytesIO(gif_buffer.getvalue())) as check:
    assert check.info.get('loop') == 0
    total = 0
    for index in range(check.n_frames):
        check.seek(index)
        check.convert('RGB').load()
        total += check.info.get('duration', 0)
    assert check.size == (width, height)
    assert total == sum(durations)
    assert check.n_frames == len(encoded) and check.n_frames > 1
    print(json.dumps({'size': check.size, 'frames': check.n_frames, 'durationMs': total, 'files': files}))
