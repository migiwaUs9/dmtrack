"""
GameTracker: ゲーム状態の監視・カード検知・OCR
GUI から threading で呼び出される。
"""

import sys
import ctypes
import ctypes.wintypes
import time
import csv
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import os
import json
import re

from PIL import Image
import imagehash

import deck_engine

sys.stdout.reconfigure(encoding="utf-8")
ctypes.windll.shcore.SetProcessDpiAwareness(2)

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────
GAME_WINDOW_TITLE = "デュエプレ"
_BASE_DIR  = Path(__file__).parent
SAMPLE_DIR = _BASE_DIR / "sample"
LOG_DIR    = _BASE_DIR / "logs"

# サンプル画像基準サイズ
SAMPLE_W, SAMPLE_H = 1336, 789

# バトルスタートテンプレート切り出し座標（サンプル画像絶対値）
BS_X1, BS_Y1, BS_X2, BS_Y2 = 467, 50, 868, 500
# ライブフレームでの検索領域（ウィンドウサイズ比率）
BS_DETECT_REL = (BS_X1/SAMPLE_W, BS_Y1/SAMPLE_H, BS_X2/SAMPLE_W, BS_Y2/SAMPLE_H)
BS_MATCH_THRESHOLD = 0.60

# ターン帯テンプレート
TURN_TEXT_Y1, TURN_TEXT_Y2 = 260, 400
TURN_TEXT_X1, TURN_TEXT_X2 = 150, 1100
TURN_BAND_REL = (
    TURN_TEXT_X1/SAMPLE_W, TURN_TEXT_Y1/SAMPLE_H,
    TURN_TEXT_X2/SAMPLE_W, TURN_TEXT_Y2/SAMPLE_H,
)
TURN_MATCH_THRESHOLD = 0.65

# カード検知・OCR
# 紺色パネル検出パラメータ（HSV）
PANEL_SEARCH_X_START = 0.55
PANEL_HSV_LOWER      = (90, 50, 20)    # H, S, V 下限
PANEL_HSV_UPPER      = (130, 255, 120)  # H, S, V 上限
PANEL_MIN_W_RATIO    = 0.10
PANEL_MIN_H_RATIO    = 0.08
PANEL_NAME_H_RATIO   = 0.30
# 同一カード重複検知のハミング距離閾値（これ以下なら同一カードとみなす）
DEDUP_HASH_THRESHOLD = 8
# テキスト検出後カード識別まで待つ安定待機時間（秒）
CARD_SETTLE_WAIT = 0.3

# manga-ocr
MANGA_OCR_ENABLED = True  # Falseに切り替えるとTesseractフォールバック

# Tesseract設定（フォールバック用）
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSDATA_DIR  = str(_BASE_DIR)  # jpn.traineddata がプロジェクトルートにある

# pHash設定
HASH_DB_PATH    = _BASE_DIR / "card_hashes.json"
MASTER_DB_PATH  = _BASE_DIR / "cards_master.json"
UNKNOWN_DIR     = _BASE_DIR / "cards_unknown"
PHASH_SIZE      = 16
PHASH_THRESHOLD = 15  # ハミング距離がこれ以下で同一カードと判定

# デバッグモード: OCR時に実際に渡している画像を captures/debug/ に保存
DEBUG_OCR = True

# ステート
STATE_WAITING  = "waiting"
STATE_IN_MATCH = "in_match"

# キャプチャ間隔
INTERVAL_WAIT  = 1.5
INTERVAL_MATCH = 0.5

# 同じバトルスタートを連続検知しない最小インターバル（秒）
BS_COOLDOWN = 15.0

# WIN / LOSE テンプレート切り出し座標（サンプル画像絶対値）
WIN_X1,  WIN_Y1,  WIN_X2,  WIN_Y2  = 658, 292, 1139, 594
LOSE_X1, LOSE_Y1, LOSE_X2, LOSE_Y2 = 658, 270, 1239, 589
WIN_DETECT_REL  = (WIN_X1/SAMPLE_W,  WIN_Y1/SAMPLE_H,  WIN_X2/SAMPLE_W,  WIN_Y2/SAMPLE_H)
LOSE_DETECT_REL = (LOSE_X1/SAMPLE_W, LOSE_Y1/SAMPLE_H, LOSE_X2/SAMPLE_W, LOSE_Y2/SAMPLE_H)
RESULT_MATCH_THRESHOLD = 0.60
RESULT_COOLDOWN = 10.0


# ──────────────────────────────────────────────
# Win32 ヘルパー
# ──────────────────────────────────────────────
def find_hwnd(title: str) -> int | None:
    found = ctypes.c_int(0)

    def cb(hwnd, _):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value.strip() == title:
                found.value = hwnd
        return True

    ctypes.windll.user32.EnumWindows(
        ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)(cb), 0
    )
    return found.value or None


def capture_window(hwnd: int) -> np.ndarray:
    """PrintWindow で背後にあるウィンドウも正確にキャプチャ → BGR ndarray"""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32

    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top

    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc  = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap  = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    gdi32.SelectObject(mem_dc, bitmap)
    user32.PrintWindow(hwnd, mem_dc, 2)

    class BMIH(ctypes.Structure):
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

    bmi = BMIH()
    bmi.biSize = ctypes.sizeof(BMIH)
    bmi.biWidth, bmi.biHeight = w, -h
    bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
    buf = (ctypes.c_byte * (w * h * 4))()
    gdi32.GetDIBits(mem_dc, bitmap, 0, h, buf, ctypes.byref(bmi), 0)

    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)

    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
    return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)


# ──────────────────────────────────────────────
# 画像処理ユーティリティ
# ──────────────────────────────────────────────
def rel_crop(frame: np.ndarray, rel: tuple) -> np.ndarray:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = int(rel[0]*w), int(rel[1]*h), int(rel[2]*w), int(rel[3]*h)
    return frame[y1:y2, x1:x2]


def template_match(frame: np.ndarray, template: np.ndarray,
                   region_rel: tuple, target_w: int, target_h: int,
                   src_w: int, src_h: int) -> float:
    """region_rel でフレームをクロップし、テンプレートをスケールして最大マッチスコアを返す"""
    region = rel_crop(frame, region_rel)
    tw = int(template.shape[1] / src_w * target_w)
    th = int(template.shape[0] / src_h * target_h)
    if tw < 1 or th < 1 or tw > region.shape[1] or th > region.shape[0]:
        return 0.0
    scaled = cv2.resize(template, (tw, th))
    res = cv2.matchTemplate(region, scaled, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return float(max_val)


def frame_diff_score(a: np.ndarray | None, b: np.ndarray) -> float:
    if a is None or a.shape != b.shape:
        return 0.0
    return float(cv2.absdiff(a, b).mean())


# ──────────────────────────────────────────────
# テンプレート読み込み
# ──────────────────────────────────────────────
def load_templates() -> dict[str, np.ndarray]:
    files = {
        "battlestart.png":    "battlestart.png",
        "Mine_turnstart.png": "Mine_turnstart.png",
        "Enemy_turnstart.png":"Enemy_turnstart.png",
        "WIN.PNG":            "WIN.PNG",
        "LOSE.PNG":           "LOSE.PNG",
    }
    imgs = {}
    for key, fname in files.items():
        img = cv2.imread(str(SAMPLE_DIR / fname))
        if img is None:
            raise FileNotFoundError(f"テンプレート画像が見つかりません: sample/{fname}")
        imgs[key] = img
    return {
        "BATTLE_START": imgs["battlestart.png"][BS_Y1:BS_Y2, BS_X1:BS_X2],
        "YOUR_TURN":    imgs["Mine_turnstart.png"][TURN_TEXT_Y1:TURN_TEXT_Y2, TURN_TEXT_X1:TURN_TEXT_X2],
        "ENEMY_TURN":   imgs["Enemy_turnstart.png"][TURN_TEXT_Y1:TURN_TEXT_Y2, TURN_TEXT_X1:TURN_TEXT_X2],
        "WIN":          imgs["WIN.PNG"][WIN_Y1:WIN_Y2,   WIN_X1:WIN_X2],
        "LOSE":         imgs["LOSE.PNG"][LOSE_Y1:LOSE_Y2, LOSE_X1:LOSE_X2],
    }


# ──────────────────────────────────────────────
# GameTracker クラス
# ──────────────────────────────────────────────
class GameTracker:
    def __init__(self, event_queue, replay_path: str | None = None):
        self._queue = event_queue
        self._running = False
        self._replay_path = replay_path  # 動画ファイルパス（Noneならライブ）

        # 状態
        self.state = STATE_WAITING
        self.current_player: str | None = None
        self.turn_count = 0
        self.my_cards: list[str] = []
        self.enemy_cards: list[str] = []
        self.last_turn_event: str | None = None
        self.last_bs_time = 0.0
        self.last_result_time = 0.0
        self.match_index = 0
        self.first_turn_player: str | None = None  # 先攻/後攻判定用

        # カード検知の状態マシン用変数
        # パネル検出の安定判定用
        self._prev_panel_hashes = []
        self._panel_stable_count = 0
        self._known_panel_hashes = []

        self._hwnd: int | None = None
        self._templates: dict | None = None
        self._decks: dict = {}
        self._all_card_names: list[str] = []
        self._hash_db: dict[str, str] = {}
        self._mocr = None

    # ── 初期化 ──────────────────────────────────
    def _init(self):
        if self._replay_path:
            self._emit("STATUS", msg=f"リプレイモード: {self._replay_path}")
            self._replay_cap = cv2.VideoCapture(self._replay_path)
            if not self._replay_cap.isOpened():
                self._emit("STATUS", msg="動画ファイルを開けません")
                return False
            fps = self._replay_cap.get(cv2.CAP_PROP_FPS) or 30
            total = int(self._replay_cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self._replay_interval = 1.0 / fps
            self._emit("STATUS", msg=f"動画: {total}フレーム, {fps:.1f}fps")
        else:
            self._replay_cap = None
            self._emit("STATUS", msg="ウィンドウを検索中...")
            self._hwnd = find_hwnd(GAME_WINDOW_TITLE)
            if not self._hwnd:
                self._emit("STATUS", msg="デュエプレが起動していません")
                return False

        self._emit("STATUS", msg="テンプレート読み込み中...")
        self._templates = load_templates()

        self._emit("STATUS", msg="デッキ定義読み込み中...")
        try:
            self._decks = deck_engine.load_decks()
        except FileNotFoundError:
            self._emit("STATUS", msg="decks.json が見つかりません")

        self._emit("STATUS", msg="カードマスター読み込み中...")
        if MASTER_DB_PATH.exists():
            with open(MASTER_DB_PATH, encoding="utf-8") as f:
                master_data = json.load(f)
                self._all_card_names = [c["name"] for c in master_data.get("cards", [])]
            self._emit("STATUS", msg=f"マスターDB: {len(self._all_card_names)} 枚ロード")
        else:
            self._emit("STATUS", msg="cards_master.json が見つかりません")

        # manga-ocrロード
        if MANGA_OCR_ENABLED:
            self._emit("STATUS", msg="manga-ocr モデルをロード中...")
            try:
                from manga_ocr import MangaOcr
                self._mocr = MangaOcr()
                self._emit("STATUS", msg="manga-ocr 準備完了")
            except Exception as e:
                self._emit("STATUS", msg=f"manga-ocr ロード失敗: {e} → Tesseractフォールバック")
                self._mocr = None
        else:
            # Tesseract 設定
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
            os.environ["TESSDATA_PREFIX"] = TESSDATA_DIR

        # カードハッシュDB読み込み
        self._load_hash_db()

        LOG_DIR.mkdir(exist_ok=True)
        UNKNOWN_DIR.mkdir(exist_ok=True)
        self._emit("STATUS", msg="待機中（対戦開始を検知しています）")
        return True

    # ── イベント送出 ────────────────────────────
    def _emit(self, event_type: str, **kwargs):
        self._queue.put({"type": event_type, **kwargs})

    # ── メインループ ────────────────────────────
    def run(self):
        self._running = True
        try:
            if not self._init():
                self._running = False
                return
        except Exception as e:
            self._emit("STATUS", msg=f"初期化エラー: {e}")
            self._running = False
            return

        frame_no = 0
        while self._running:
            try:
                # フレーム取得
                if self._replay_cap:
                    ret, frame = self._replay_cap.read()
                    if not ret:
                        self._emit("STATUS", msg="リプレイ終了")
                        self._emit("LOG", msg=f"リプレイ完了: {frame_no}フレーム処理")
                        break
                    frame_no += 1
                    if frame_no % 100 == 0:
                        self._emit("LOG", msg=f"[Replay] frame #{frame_no}")
                else:
                    frame = capture_window(self._hwnd)

                h, w = frame.shape[:2]

                if self.state == STATE_WAITING:
                    self._process_waiting(frame, w, h)
                    if not self._replay_cap:
                        time.sleep(INTERVAL_WAIT)
                else:
                    self._process_in_match(frame, w, h)
                    if not self._replay_cap:
                        time.sleep(INTERVAL_MATCH)

            except Exception as e:
                import traceback
                self._emit("LOG", msg=f"エラー: {e}\n{traceback.format_exc()}")
                self._emit("STATUS", msg=f"エラー: {e}")
                time.sleep(2.0)

        if self._replay_cap:
            self._replay_cap.release()

    def stop(self):
        self._running = False

    # ── 待機ステート ────────────────────────────
    def _process_waiting(self, frame: np.ndarray, w: int, h: int):
        if self._detect_battle_start(frame, w, h):
            self._start_match()

    # ── 対戦中ステート ───────────────────────────
    def _process_in_match(self, frame: np.ndarray, w: int, h: int):
        self._emit("LOG", msg=f"[Match] frame {w}x{h} state={self.state}")
        # 次の対戦開始検知（= 前の試合終了）
        if time.time() - self.last_bs_time > BS_COOLDOWN:
            if self._detect_battle_start(frame, w, h):
                self._end_match()
                self._start_match()
                return

        # WIN / LOSE 検知（優先）
        if time.time() - self.last_result_time > RESULT_COOLDOWN:
            for result_key, detect_rel in (("WIN", WIN_DETECT_REL), ("LOSE", LOSE_DETECT_REL)):
                score = template_match(
                    frame, self._templates[result_key],
                    detect_rel, w, h, SAMPLE_W, SAMPLE_H
                )
                if score >= RESULT_MATCH_THRESHOLD:
                    self.last_result_time = time.time()
                    self._end_match(result=result_key)
                    return

        # ターン検知
        for key in ("YOUR_TURN", "ENEMY_TURN"):
            score = template_match(
                frame, self._templates[key],
                TURN_BAND_REL, w, h, SAMPLE_W, SAMPLE_H
            )
            if score >= TURN_MATCH_THRESHOLD and key != self.last_turn_event:
                self.current_player = key
                self.turn_count += 1
                # 最初のターンで先攻/後攻を確定
                if self.turn_count == 1:
                    self.first_turn_player = key
                    order = "先攻" if key == "YOUR_TURN" else "後攻"
                    self._emit("ORDER_DETERMINED", order=order)
                self.last_turn_event = key
                self._emit("TURN_CHANGE", player=key, turn=self.turn_count)
                break

        # ── 右パネル検出によるカード検知 ──
        # 全パネルをハッシュで追跡し、新規パネルのみOCRする
        panels = self._find_detail_panels(frame)
        self._emit("LOG", msg=f"[Detect] panels={len(panels)} size={w}x{h}")
        current_panel_hashes = []

        for px, py, pw, ph in panels:
            art_region = frame[py:py + ph, px:px + pw]
            pil_art = Image.fromarray(cv2.cvtColor(art_region, cv2.COLOR_BGR2RGB))
            h = imagehash.phash(pil_art, hash_size=PHASH_SIZE)
            current_panel_hashes.append((h, px, py, pw, ph))

        # 前フレームのハッシュリストと比較し安定判定
        if not hasattr(self, '_prev_panel_hashes'):
            self._prev_panel_hashes = []
            self._panel_stable_count = 0
            self._known_panel_hashes = []  # 既にOCR済みパネルのハッシュ

        # 前フレームとパネル構成が同じか（全体のハッシュ距離で判定）
        prev_h_list = [h for h, *_ in self._prev_panel_hashes]
        curr_h_list = [h for h, *_ in current_panel_hashes]

        same_as_prev = (len(prev_h_list) == len(curr_h_list) and
                        all(a - b <= 5 for a, b in zip(curr_h_list, prev_h_list)))

        if same_as_prev:
            self._panel_stable_count += 1
        else:
            self._panel_stable_count = 0
        self._prev_panel_hashes = current_panel_hashes

        self._emit("LOG", msg=f"[Stable] count={self._panel_stable_count} known={len(self._known_panel_hashes)} cur={len(current_panel_hashes)}")

        # 2フレーム安定したら新規パネルをチェック
        if self._panel_stable_count >= 2 and current_panel_hashes:
            for phash, px, py, pw, ph in current_panel_hashes:
                # 既知パネルと照合
                is_known = any(phash - kh <= DEDUP_HASH_THRESHOLD
                               for kh in self._known_panel_hashes)
                if is_known:
                    continue

                # 新規パネル発見
                self._known_panel_hashes.append(phash)
                name_region = frame[py:py + int(ph * PANEL_NAME_H_RATIO), px:px + pw]
                art_region = frame[py:py + ph, px:px + pw]

                if not self._has_card_text(name_region):
                    continue

                ts = str(int(time.time()))
                if DEBUG_OCR:
                    debug_dir = _BASE_DIR / "captures" / "debug"
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(debug_dir / f"panel_name_{ts}.png"), name_region)
                    cv2.imwrite(str(debug_dir / f"panel_art_{ts}.png"), art_region)

                card_name = self._identify_card(name_region, art_region, ts, phash)
                self._emit("LOG", msg=f"[OCR] panel=({px},{py}) result={card_name}")
                if card_name and card_name != "(不明)":
                    player = self.current_player or "不明"
                    self._emit("CARD_PLAYED", player=player, card=card_name, turn=self.turn_count)
                    if player == "YOUR_TURN":
                        self.my_cards.append(card_name)
                    else:
                        self.enemy_cards.append(card_name)

                    if self.enemy_cards:
                        deck, pct = deck_engine.infer(self.enemy_cards, self._decks)
                        self._emit("DECK_INFERRED", deck=deck, pct=pct)

    # ── 黒背景パネル検出 ─────────────────────────
    # ── 紺色パネル検出 ─────────────────────────
    def _find_detail_panels(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        """右側の紺色パネルをHSV色検出で探し、(x, y, w, h) のリストを返す（y昇順）"""
        fh, fw = frame.shape[:2]
        x_start = int(fw * PANEL_SEARCH_X_START)
        roi = frame[0:fh, x_start:fw]

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        lower = np.array(PANEL_HSV_LOWER, dtype=np.uint8)
        upper = np.array(PANEL_HSV_UPPER, dtype=np.uint8)
        mask = cv2.inRange(hsv, lower, upper)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        min_w = int(fw * PANEL_MIN_W_RATIO)
        min_h = int(fh * PANEL_MIN_H_RATIO)
        panels = []
        for cnt in contours:
            rx, ry, rw, rh = cv2.boundingRect(cnt)
            # 最小サイズ＋縦横比で手札欄などの誤検出を除外（高さ>幅*0.3）
            if rw >= min_w and rh >= min_h and rh > rw * 0.3:
                panels.append((rx + x_start, ry, rw, rh))

        panels.sort(key=lambda p: p[1])
        return panels

    def _detect_battle_start(self, frame: np.ndarray, w: int, h: int) -> bool:
        score = template_match(
            frame, self._templates["BATTLE_START"],
            BS_DETECT_REL, w, h, SAMPLE_W, SAMPLE_H
        )
        return score >= BS_MATCH_THRESHOLD

    # ── 試合開始・終了処理 ───────────────────────
    def _start_match(self):
        self.match_index += 1
        self.state = STATE_IN_MATCH
        self.current_player = None
        self.turn_count = 0
        self.my_cards = []
        self.enemy_cards = []
        self.last_turn_event = None
        self.last_bs_time = time.time()
        self.first_turn_player = None
        self._prev_panel_hashes = []
        self._panel_stable_count = 0
        self._known_panel_hashes = []
        self._emit("BATTLE_START", match=self.match_index)
        self._emit("STATUS", msg=f"対戦中 (試合 #{self.match_index})")

    def _end_match(self, result: str = "不明"):
        # 先攻/後攻を確定
        if self.first_turn_player == "YOUR_TURN":
            order = "先攻"
        elif self.first_turn_player == "ENEMY_TURN":
            order = "後攻"
        else:
            order = "不明"

        deck, pct = deck_engine.infer(self.enemy_cards, self._decks) if self.enemy_cards else ("-", 0.0)
        self._emit("MATCH_END",
                   match=self.match_index,
                   result=result,
                   order=order,
                   my_cards=list(self.my_cards),
                   enemy_cards=list(self.enemy_cards),
                   deck=deck, pct=pct)
        self._save_csv(result=result, order=order, deck=deck, pct=pct)
        self.state = STATE_WAITING
        self._emit("STATUS", msg="待機中（対戦開始を検知しています）")

    # ── CSV 保存 ────────────────────────────────
    def _save_csv(self, result: str = "不明", order: str = "不明",
                  deck: str = "-", pct: float = 0.0):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = LOG_DIR / f"match_{self.match_index:03d}_{ts}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["# 試合",       str(self.match_index)])
            w.writerow(["# 結果",       result])
            w.writerow(["# 先攻後攻",   order])
            w.writerow(["# 推定デッキ", f"{deck} ({pct:.0f}%)"])
            w.writerow([])
            w.writerow(["プレイヤー", "カード名"])
            for c in self.my_cards:
                w.writerow(["自分", c])
            for c in self.enemy_cards:
                w.writerow(["相手", c])
        self._emit("LOG_SAVED", path=str(path))

    # ── ハッシュDB ─────────────────────────────
    def _load_hash_db(self):
        if HASH_DB_PATH.exists():
            with open(HASH_DB_PATH, encoding="utf-8") as f:
                self._hash_db = json.load(f)
            self._emit("STATUS", msg=f"カードDB: {len(self._hash_db)} 枚登録済み")
        else:
            self._hash_db = {}

    def _save_hash_db(self):
        with open(HASH_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(self._hash_db, f, ensure_ascii=False, indent=2)

    # ── カード表示バリデーション ──────────────────
    def _has_card_text(self, region: np.ndarray) -> bool:
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        edge_pct = (edges > 0).sum() / edges.size * 100
        return edge_pct >= 1.0

    def _identify_card(self, name_region: np.ndarray, art_region: np.ndarray,
                       ts: str, phash: "imagehash.ImageHash") -> str:
        """pHash(imagehashオブジェクト)でDB検索し、なければOCRフォールバック"""
        card_name = self._lookup_hash(phash)
        if card_name:
            return card_name

        # DB にない → OCRフォールバック
        card_name = self._ocr_card_name(name_region, ts)

        # 未知カード画像を保存（後で手動ラベル付け用）
        hash_hex = str(phash)
        unknown_path = UNKNOWN_DIR / f"{ts}_{hash_hex[:16]}.png"
        cv2.imwrite(str(unknown_path), art_region)

        # OCRで意味のある名前のみ自動登録
        if card_name:
            self._hash_db[hash_hex] = card_name
            self._save_hash_db()
            self._emit("STATUS", msg=f"新カード自動登録: {card_name} (DB: {len(self._hash_db)}枚)")
            return card_name

        return "(不明)"

    def _lookup_hash(self, phash: "imagehash.ImageHash") -> str | None:
        """ハッシュDBからハミング距離が閾値以内の最も近いカードを返す"""
        best_name = None
        best_dist = PHASH_THRESHOLD + 1

        for stored_hex, name in self._hash_db.items():
            stored_hash = imagehash.hex_to_hash(stored_hex)
            dist = phash - stored_hash
            if dist < best_dist:
                best_dist = dist
                best_name = name

        return best_name

    # ── OCR（manga-ocr優先 / Tesseractフォールバック）──────
    def _ocr_card_name(self, region: np.ndarray, ts: str | None = None) -> str | None:
        if self._mocr is None and not MANGA_OCR_ENABLED:
            return None

        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        _, bin_mask = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)
        inv_mask = cv2.bitwise_not(bin_mask)
        from PIL import Image
        pil_img = Image.fromarray(cv2.cvtColor(inv_mask, cv2.COLOR_GRAY2RGB))

        if self._mocr is not None:
            raw = self._mocr(pil_img)
            method_label = "manga_ocr"
        else:
            import pytesseract
            raw = pytesseract.image_to_string(pil_img, lang="jpn", config="--psm 7").strip()
            method_label = "tesseract"

        import re, deck_engine
        jp_only = "".join(re.findall(
            r"[぀-ゟ゠-ヿ一-鿿　-〿！-～ｦ-ﾟA-Za-z0-9]+",
            raw
        ))

        final_name = None
        if jp_only and self._all_card_names:
            corrected = deck_engine.fuzzy_correct(jp_only, self._all_card_names)
            if corrected:
                final_name = corrected

        if DEBUG_OCR:
            from datetime import datetime
            self._save_debug(region, ts or datetime.now().strftime("%H%M%S_%f"),
                             method=method_label, result=final_name or "拒否(Garbage: " + jp_only + ")", raw=raw)

        return final_name

    def _save_debug(self, name_region: np.ndarray, ts: str, method: str,
                    result: str, hash_str: str = "", raw: str = ""):
        debug_dir = _BASE_DIR / "captures" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)

        cv2.imwrite(str(debug_dir / f"card_name_{ts}.png"), name_region)

        with open(str(debug_dir / f"card_result_{ts}.txt"), "w",
                  encoding="utf-8") as f:
            f.write(f"method: {method}\n")
            f.write(f"result: {result}\n")
            if hash_str:
                f.write(f"hash: {hash_str}\n")
            if raw:
                f.write(f"ocr_raw: {raw}\n")
