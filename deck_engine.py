"""
デッキ推定エンジン
相手の使用カードリスト × decks.json の重みでスコアリングする。
"""

import json
import re
from difflib import SequenceMatcher
from pathlib import Path


def load_decks(path: str | None = None) -> dict:
    if path is None:
        path = str(Path(__file__).parent / "decks.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # _comment キーは除外
    return {k: v for k, v in data.items() if not k.startswith("_")}


# OCR結果から日本語文字のみ抽出するパターン
_JP_PATTERN = re.compile(
    r"[\u3040-\u309f"   # ひらがな
    r"\u30a0-\u30ff"    # カタカナ
    r"\u4e00-\u9fff"    # 漢字
    r"\u3000-\u303f"    # 和文記号（・など）
    r"！-～"            # 全角英数記号
    r"]+"
)

# ファジーマッチの類似度閾値（0〜1）
_FUZZY_THRESHOLD = 0.65


def _normalize(s: str) -> str:
    """日本語文字のみ残して結合"""
    return "".join(_JP_PATTERN.findall(s))


def _card_matches(ocr_name: str, deck_card: str) -> bool:
    """OCRの揺れ・部分欠けに対応した照合。
    1) 部分一致（どちらかが他方を含む）
    2) ファジーマッチ（文字列類似度 >= 閾値）
    """
    n_ocr  = _normalize(ocr_name)
    n_deck = _normalize(deck_card)

    if not n_ocr or not n_deck:
        return False

    # 部分一致
    if n_deck in n_ocr or n_ocr in n_deck:
        return True

    # ファジーマッチ（短い方が長い方の部分と一致するか確認）
    shorter, longer = (n_ocr, n_deck) if len(n_ocr) <= len(n_deck) else (n_deck, n_ocr)
    # shorter の長さ分だけ longer をスライドして最高スコアを求める
    best = 0.0
    for i in range(len(longer) - len(shorter) + 1):
        window = longer[i:i + len(shorter)]
        ratio = SequenceMatcher(None, shorter, window).ratio()
        if ratio > best:
            best = ratio
    return best >= _FUZZY_THRESHOLD


def collect_all_card_names(decks: dict) -> list[str]:
    """全デッキ定義からユニークなカード名一覧を返す"""
    names = set()
    for card_weights in decks.values():
        names.update(card_weights.keys())
    return sorted(names)


def fuzzy_correct(ocr_text: str, card_names: list[str],
                  threshold: float = 0.45) -> str | None:
    """OCR結果を既知カード名リストでファジーマッチ補正する。
    OCRはゴミ文字が混ざるため、スライドウィンドウでカード名長の部分文字列を
    それぞれ比較し、最も類似度の高いカード名を返す。閾値未満なら None。
    """
    n_ocr = _normalize(ocr_text)
    if not n_ocr:
        return None

    best_name = None
    best_score = 0.0

    for card_name in card_names:
        n_card = _normalize(card_name)
        if not n_card:
            continue

        # 部分一致: カード名がOCR結果に含まれる or その逆
        if n_card in n_ocr or n_ocr in n_card:
            score = min(len(n_ocr), len(n_card)) / max(len(n_ocr), len(n_card))
            # 部分一致はボーナス
            score = min(score + 0.3, 1.0)
            if score > best_score:
                best_score = score
                best_name = card_name
            continue

        # スライドウィンドウ: OCR文字列からカード名長の部分を切り出して比較
        # （OCRにゴミ文字が混ざるケースに対応）
        card_len = len(n_card)
        if len(n_ocr) >= card_len:
            for i in range(len(n_ocr) - card_len + 1):
                window = n_ocr[i:i + card_len]
                ratio = SequenceMatcher(None, window, n_card).ratio()
                if ratio > best_score:
                    best_score = ratio
                    best_name = card_name
        else:
            # OCRの方が短い場合: 全体で比較
            ratio = SequenceMatcher(None, n_ocr, n_card).ratio()
            if ratio > best_score:
                best_score = ratio
                best_name = card_name

    if best_score >= threshold:
        return best_name
    return None


def infer(enemy_cards: list[str], decks: dict) -> tuple[str, float]:
    """
    相手の使用カードリストからデッキを推定する。
    Returns: (デッキ名, 合致率%) のタプル。不明なら ("-", 0.0)
    """
    if not enemy_cards or not decks:
        return "-", 0.0

    best_deck = "-"
    best_pct = 0.0

    for deck_name, card_weights in decks.items():
        total_weight = sum(card_weights.values())
        if total_weight == 0:
            continue

        matched_weight = 0.0
        for deck_card, weight in card_weights.items():
            if any(_card_matches(ec, deck_card) for ec in enemy_cards):
                matched_weight += weight

        pct = matched_weight / total_weight * 100
        if pct > best_pct:
            best_pct = pct
            best_deck = deck_name

    return best_deck, best_pct


def score_all(enemy_cards: list[str], decks: dict) -> list[tuple[str, float]]:
    """全デッキのスコアを降順で返す（デバッグ・詳細表示用）"""
    results = []
    for deck_name, card_weights in decks.items():
        total = sum(card_weights.values())
        if total == 0:
            continue
        matched = sum(
            w for c, w in card_weights.items()
            if any(_card_matches(ec, c) for ec in enemy_cards)
        )
        results.append((deck_name, matched / total * 100))
    return sorted(results, key=lambda x: x[1], reverse=True)
