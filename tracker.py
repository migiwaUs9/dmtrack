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
# 検知領域: 画面中央を広くカバー（自分・相手両方）
CARD_DETECT_REL  = (0.05, 0.03, 0.80, 0.97)
# OCR領域: カード名バナー（コスト円の右側 ～ カード右端）
CARD_NAME_REL    = (0.38, 0.15, 0.66, 0.21)
# カードイラスト領域（pHash用: カード名バナー下 ～ テキスト欄上）
CARD_ART_REL     = (0.34, 0.22, 0.66, 0.55)
CARD_DIFF_THRESHOLD = 20.0

CARD_HIDE_THRESHOLD = 10.0
# カードバナーのテキスト存在チェック閾値（black% がこれ以上でカード有りと判定）
CARD_TEXT_MIN_PCT = 3.0

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
    def __init__(self, event_queue):
        self._queue = event_queue
        self._running = False

        # 状態
        self.state = STATE_WAITING
        self.current_player: str | None = None
        self.turn_count = 0
        self.my_cards: list[str] = []
        self.enemy_cards: list[str] = []
        self.last_turn_event: str | None = None
        self.card_visible = False
        self.card_logged = False
        self.prev_card_region: np.ndarray | None = None
        self.last_bs_time = 0.0
        self.last_result_time = 0.0
        self.match_index = 0
        self.first_turn_player: str | None = None  # 先攻/後攻判定用

        self._hwnd: int | None = None
        self._templates: dict | None = None
        self._decks: dict = {}
        self._all_card_names: list[str] = []  # ファジーマッチ用
        self._hash_db: dict[str, str] = {}    # {hash文字列: カード名}
        self._last_card_hash: str | None = None  # 同一カード重複防止
        self._mocr = None  # manga-ocrインスタンス

    # ── 初期化 ──────────────────────────────────
    def _init(self):
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

        while self._running:
            try:
                frame = capture_window(self._hwnd)
                h, w = frame.shape[:2]

                if self.state == STATE_WAITING:
                    self._process_waiting(frame, w, h)
                    time.sleep(INTERVAL_WAIT)
                else:
                    self._process_in_match(frame, w, h)
                    time.sleep(INTERVAL_MATCH)

            except Exception as e:
                self._emit("STATUS", msg=f"エラー: {e}")
                time.sleep(2.0)

    def stop(self):
        self._running = False

    # ── 待機ステート ────────────────────────────
    def _process_waiting(self, frame: np.ndarray, w: int, h: int):
        if self._detect_battle_start(frame, w, h):
            self._start_match()

    # ── 対戦中ステート ───────────────────────────
    def _process_in_match(self, frame: np.ndarray, w: int, h: int):
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
                self.card_visible = False
                self.card_logged = False
                self._emit("TURN_CHANGE", player=key, turn=self.turn_count)
                break

        # カード拡大表示の検知（2段階: 差分トリガー → バナー検証）
        card_region = rel_crop(frame, CARD_DETECT_REL)
        diff = frame_diff_score(self.prev_card_region, card_region)

        if not self.card_visible and diff > CARD_DIFF_THRESHOLD:
            self.card_visible = True
            self.card_logged = False

        if self.card_visible and not self.card_logged:
            time.sleep(0.3)  # アニメーション安定待ち
            stable = capture_window(self._hwnd)

            # Step 1: カード名バナーにテキストが存在するか検証
            if not self._has_card_text(stable):
                # テキストなし → カード表示ではない（エフェクト等の誤爆）
                self.card_logged = True  # この差分イベントはスキップ
            else:
                # Step 2: カードイラストのpHashで識別
                card_name = self._identify_card(stable)
                if card_name and card_name != "(不明)":
                    player = self.current_player or "不明"
                    self._emit("CARD_PLAYED", player=player, card=card_name, turn=self.turn_count)
                    if player == "YOUR_TURN":
                        self.my_cards.append(card_name)
                    else:
                        self.enemy_cards.append(card_name)
                    # デッキ推定を更新
                    if self.enemy_cards:
                        deck, pct = deck_engine.infer(self.enemy_cards, self._decks)
                        self._emit("DECK_INFERRED", deck=deck, pct=pct)
                self.card_logged = True

        if self.card_visible and diff < CARD_HIDE_THRESHOLD:
            self.card_visible = False
            self.card_logged = False

        self.prev_card_region = card_region.copy()

    # ── バトルスタート検知 ───────────────────────
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
        self.card_visible = False
        self.card_logged = False
        self.prev_card_region = None
        self.last_bs_time = time.time()
        self.first_turn_player = None
        self._last_card_hash = None
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
    def _has_card_text(self, frame: np.ndarray) -> bool:
        """カード名バナー領域にテキストが存在するか判定。
        二値化マスクの黒ピクセル率(= テキスト部分)がCAD_TEXT_MIN_PCT以上ならTrue。
        """
        region = rel_crop(frame, CARD_NAME_REL)
        scale = 5
        up = cv2.resize(region, (region.shape[1] * scale, region.shape[0] * scale),
                        interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 160, 255, cv2.THRESH_BINARY)
        padded = cv2.copyMakeBorder(binary, 30, 30, 30, 30,
                                    cv2.BORDER_CONSTANT, value=0)
        inverted = cv2.bitwise_not(padded)

        black_pct = (inverted < 128).sum() / inverted.size * 100
        return black_pct >= CARD_TEXT_MIN_PCT

    # ── カード識別（pHash優先 → OCRフォールバック）──
    def _identify_card(self, frame: np.ndarray) -> str:
        ts = datetime.now().strftime("%H%M%S_%f")

        # カードイラスト領域を切り出してpHash計算
        art = rel_crop(frame, CARD_ART_REL)
        pil_art = Image.fromarray(cv2.cvtColor(art, cv2.COLOR_BGR2RGB))
        current_hash = imagehash.phash(pil_art, hash_size=PHASH_SIZE)
        hash_str = str(current_hash)

        # 同一カードの重複検知防止
        if hash_str == self._last_card_hash:
            return ""
        self._last_card_hash = hash_str

        # ハッシュDBで検索
        card_name = self._lookup_hash(current_hash)

        if card_name:
            if DEBUG_OCR:
                self._save_debug(frame, ts, method="hash", result=card_name,
                                 hash_str=hash_str)
            return card_name

        # DB にない → OCRフォールバック + 未知カード画像を保存
        card_name = self._ocr_card_name(frame, ts)

        # 未知カード画像を保存（後でラベル付け用）
        unknown_path = UNKNOWN_DIR / f"{ts}_{hash_str[:16]}.png"
        cv2.imwrite(str(unknown_path), art)

        # OCRで名前が取れたらハッシュDBに自動登録
        if card_name and card_name != "(不明)":
            self._hash_db[hash_str] = card_name
            self._save_hash_db()
            self._emit("STATUS",
                       msg=f"新カード登録: {card_name} (DB: {len(self._hash_db)}枚)")

        return card_name or "(不明)"

    def _lookup_hash(self, current_hash) -> str | None:
        """ハッシュDBからハミング距離が閾値以内の最も近いカードを返す"""
        best_name = None
        best_dist = PHASH_THRESHOLD + 1

        for stored_hex, name in self._hash_db.items():
            stored_hash = imagehash.hex_to_hash(stored_hex)
            dist = current_hash - stored_hash
            if dist < best_dist:
                best_dist = dist
                best_name = name

        return best_name

    # ── OCR（manga-ocr優先 / Tesseractフォールバック）──────
    def _ocr_card_name(self, frame: np.ndarray, ts: str = "") -> str:
        region = rel_crop(frame, CARD_NAME_REL)
        pil_region = Image.fromarray(cv2.cvtColor(region, cv2.COLOR_BGR2RGB))

        if self._mocr is not None:
            # manga-ocr: 前処理不要。PILイメージをそのまま渡す
            raw = self._mocr(pil_region)
            method_label = "manga_ocr"
        else:
            # Tesseract フォールバック
            import pytesseract
            scale = 5
            up = cv2.resize(region, (region.shape[1] * scale, region.shape[0] * scale),
                            interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
            _, binary = cv2.threshold(gray, 160, 255, cv2.THRESH_BINARY)
            padded = cv2.copyMakeBorder(binary, 30, 30, 30, 30,
                                        cv2.BORDER_CONSTANT, value=0)
            inverted = cv2.bitwise_not(padded)
            raw = pytesseract.image_to_string(
                inverted, lang="jpn", config="--psm 7 --oem 3"
            ).strip()
            method_label = "tesseract"

        # 日本語・英数字のみ抽出
        jp_only = "".join(re.findall(
            r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff"
            r"\u3000-\u303f\uff01-\uff5e\uff66-\uff9fA-Za-z0-9]+",
            raw
        ))

        # カード名マスターでファジーマッチ補正
        if jp_only and self._all_card_names:
            corrected = deck_engine.fuzzy_correct(jp_only, self._all_card_names)
            if corrected:
                jp_only = corrected

        if DEBUG_OCR:
            self._save_debug(frame, ts or datetime.now().strftime("%H%M%S_%f"),
                             method=method_label, result=jp_only, raw=raw)

        return jp_only or "(不明)"

    # ── デバッグ保存 ──────────────────────────────
    def _save_debug(self, frame: np.ndarray, ts: str, method: str,
                    result: str, hash_str: str = "", raw: str = ""):
        debug_dir = _BASE_DIR / "captures" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)

        region = rel_crop(frame, CARD_NAME_REL)
        art = rel_crop(frame, CARD_ART_REL)
        cv2.imwrite(str(debug_dir / f"card_region_{ts}.png"), region)
        cv2.imwrite(str(debug_dir / f"card_art_{ts}.png"), art)

        with open(str(debug_dir / f"card_result_{ts}.txt"), "w",
                  encoding="utf-8") as f:
            f.write(f"method: {method}\n")
            f.write(f"result: {result}\n")
            if hash_str:
                f.write(f"hash: {hash_str}\n")
            if raw:
                f.write(f"ocr_raw: {raw}\n")
