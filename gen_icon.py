"""生成藍色下載圖示（圓角漸層方塊 + 白色向下箭頭落入磁碟）。

產出：
  app_icon.png   1024px 母檔
  app_icon.ico   多尺寸（16/24/32/48/64/128/256）
  app_icon.py    內嵌 256px base64 的載入模組

用法：python gen_icon.py
"""

import base64

import numpy as np
from PIL import Image, ImageDraw

S = 1024
C1 = np.array([91, 155, 255], dtype=np.float32)   # 左上 #5B9BFF
C2 = np.array([27, 95, 222], dtype=np.float32)     # 右下 #1B5FDE


def build_master():
    # 對角漸層背景
    y, x = np.mgrid[0:S, 0:S].astype(np.float32)
    t = ((x + y) / (2.0 * (S - 1)))[..., None]
    grad = (C1 * (1.0 - t) + C2 * t).astype(np.uint8)
    img = Image.fromarray(grad, "RGB").convert("RGBA")

    # 圓角遮罩
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=220, fill=255)
    img.putalpha(mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    cx = S // 2

    # 箭桿（圓角直條）
    w = 120
    d.rounded_rectangle([cx - w // 2, 240, cx + w // 2, 480], radius=w // 2, fill=white)

    # 箭頭（三角形）
    hw = 110
    d.polygon([(cx - hw, 470), (cx + hw, 470), (cx, 620)], fill=white)

    # 磁碟（硬碟本體 + 指示燈）
    d.rounded_rectangle([300, 660, 724, 840], radius=40, fill=white)   # 本體
    d.ellipse([667, 732, 703, 768], fill=(27, 95, 222))                # 指示燈

    return img


def main():
    master = build_master()

    # 母檔與 256px 內嵌版
    master.save("app_icon.png", "PNG")
    icon256 = master.resize((256, 256), Image.LANCZOS)

    # Chrome 擴充功能圖示（manifest 引用的 16/48/128）
    for name, size in (("icon16.png", (16, 16)), ("icon48.png", (48, 48)), ("icon128.png", (128, 128))):
        master.resize(size, Image.LANCZOS).save(f"chrome_extension/images/{name}", "PNG")

    # 寫入 app_icon.py
    import io
    buf = io.BytesIO()
    icon256.save(buf, "PNG")
    data = buf.getvalue()
    b64 = base64.b64encode(data).decode()

    lines = []
    for i in range(0, len(b64), 76):
        lines.append('    "%s"' % b64[i:i + 76])

    py = '''"""專案官方圖示（內嵌，避免打包後路徑失效）。"""

import base64

from PySide6.QtGui import QIcon, QPixmap

_ICON_B64 = (
%s
)


def load_app_icon():
    """回傳專案官方圖示的 QIcon。"""
    data = base64.b64decode(_ICON_B64)
    pixmap = QPixmap()
    pixmap.loadFromData(data, "PNG")
    return QIcon(pixmap)
''' % "\n".join(lines)

    with open("app_icon.py", "w", encoding="utf-8") as f:
        f.write(py)

    # 多尺寸 ICO（單一來源圖 + sizes 參數，PIL 會生成各尺寸 entry）
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save("app_icon.ico", format="ICO", sizes=sizes)

    print("app_icon.png", master.size)
    print("app_icon.ico saved with", len(sizes), "sizes")
    print("app_icon.py written, b64 chars:", len(b64))


if __name__ == "__main__":
    main()
