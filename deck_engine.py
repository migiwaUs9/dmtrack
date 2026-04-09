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
    r"A-Za-z0-9"        # 英数字
    r"ぁ-んァ-ヶ"       # 濁点なども
    r"]+"
)

def _normalize(s: str) -> str:
    """ノイズを除去して正規化。OCRの揺れを最小限に抑える"""
    if not s: return ""
    # カタカナ、英数字、漢字のみ残す（記号・空白除去）
    s = "".join(_JP_PATTERN.findall(s))
    # 半角/全角の統一、大文字小文字の統一
    s = s.translate(str.maketrans(
        "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
        "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    )).upper()
    # 長音と漢字の一を同一視するなどの高度な正規化は一旦保留（複雑化回避）
    return s


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
                  threshold: float = 0.60) -> str | None:
    """OCR結果を既知カード名リストでファジーマッチ補正する。
    4000枚規模のリストに対して、高速な絞り込みを行いつつ補正を行う。
    """
    n_ocr = _normalize(ocr_text)
    if not n_ocr or len(n_ocr) < 2:  # 短すぎるのは無視
        return None

    best_name = None
    best_score = 0.0

    # 1次フィルタ: 
    # 短い方の文字列が長い方に含まれているかをチェック（高速）
    for card_name in card_names:
        n_card = _normalize(card_name)
        if not n_card: continue

        # 完全一致は即終了
        if n_ocr == n_card:
            return card_name

        # 部分一致チェック
        if n_card in n_ocr or n_ocr in n_card:
            # 長さの比率をスコアにする
            score = min(len(n_ocr), len(n_card)) / max(len(n_ocr), len(n_card))
            # 部分一致ボーナス
            score += 0.2
            if score > best_score:
                best_score = score
                best_name = card_name
            continue
            
        # 編集距離ベースの簡易的なファジーマッチ（SequenceMatcherは重いので、
        # ある程度確信が持てる場合のみ詳細計算するのが理想だが、
        # まずはスライドウィンドウで全件チェック）
        # ただし、文字数が大幅に違う場合はスキップして高速化
        if abs(len(n_ocr) - len(n_card)) > 5:
            continue

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
