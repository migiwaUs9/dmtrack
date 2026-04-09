"""
Task 1: デュエプレウィンドウを直接キャプチャするテスト
PrintWindow API を使い、ウィンドウが背後にある状態でも正確にキャプチャする。
"""

import sys
import os
import ctypes
import ctypes.wintypes
from datetime import datetime
from PIL import Image

GAME_WINDOW_TITLE = "デュエプレ"

# DPI スケーリングの影響を受けない座標を取得するため Process を DPI Aware に設定
ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE


def find_window(title: str) -> int | None:
    """指定タイトルのウィンドウ hwnd を返す。見つからなければ None。"""
    found = ctypes.c_int(0)

    def callback(hwnd, _):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value.strip() == title:
            found.value = hwnd
        return True

    ctypes.windll.user32.EnumWindows(
        ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)(callback), 0
    )
    return found.value or None


def capture_window(hwnd: int) -> Image.Image:
    """
    PrintWindow API でウィンドウを直接キャプチャする。
    ウィンドウが背後にあっても正確に取得できる。
    """
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top

    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)

    # PW_RENDERFULLCONTENT = 2: ハードウェアアクセラレーションも含めて描画
    user32.PrintWindow(hwnd, mem_dc, 2)

    # ビットマップデータを取得 (BGRA 32bit)
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize",          ctypes.c_uint32),
            ("biWidth",         ctypes.c_int32),
            ("biHeight",        ctypes.c_int32),
            ("biPlanes",        ctypes.c_uint16),
            ("biBitCount",      ctypes.c_uint16),
            ("biCompression",   ctypes.c_uint32),
            ("biSizeImage",     ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed",       ctypes.c_uint32),
            ("biClrImportant",  ctypes.c_uint32),
        ]

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = width
    bmi.biHeight = -height  # 負値 = 上から下の行順
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0  # BI_RGB

    buf = (ctypes.c_byte * (width * height * 4))()
    gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(bmi), 0)

    # クリーンアップ
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)

    # BGRA → RGBA に変換して PIL Image へ
    img = Image.frombytes("RGBA", (width, height), bytes(buf), "raw", "BGRA")
    return img.convert("RGB")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    output_dir = "captures"
    os.makedirs(output_dir, exist_ok=True)

    hwnd = find_window(GAME_WINDOW_TITLE)
    if hwnd is None:
        print(f"ウィンドウ '{GAME_WINDOW_TITLE}' が見つかりませんでした。")
        return

    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    print(f"ウィンドウ検出: hwnd={hwnd}, pos=({rect.left}, {rect.top}), size={w}x{h}")

    img = capture_window(hwnd)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(output_dir, f"duelplace_{timestamp}.png")
    img.save(filename)

    print(f"保存完了: {filename}")
    print(f"  サイズ: {img.size[0]}x{img.size[1]} px")
    print(f"  ファイルサイズ: {os.path.getsize(filename):,} bytes")


if __name__ == "__main__":
    main()
