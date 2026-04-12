"""
DM Track GUI
tkinter ベースのメインウィンドウ。tracker.py を別スレッドで駆動する。
"""

import sys
import tkinter as tk
from tkinter import ttk, font as tkfont, filedialog
import threading
import queue

from tracker import GameTracker

sys.stdout.reconfigure(encoding="utf-8")


class DMTrackGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("DM Track")
        self.root.resizable(False, False)

        self._queue: queue.Queue = queue.Queue()
        self._tracker: GameTracker | None = None
        self._tracker_thread: threading.Thread | None = None
        self._match_count = 0

        self._build_ui()
        self._poll()

    # ──────────────────────────────────────────
    # UI 構築
    # ──────────────────────────────────────────
    def _build_ui(self):
        PAD = {"padx": 6, "pady": 4}

        # ── コントロールバー ──
        ctrl = tk.Frame(self.root, bg="#2c3e50", pady=6, padx=8)
        ctrl.pack(fill=tk.X)

        self._btn_start = tk.Button(
            ctrl, text="● 記録開始", bg="#27ae60", fg="white",
            activebackground="#2ecc71", relief=tk.FLAT,
            width=12, font=("", 10, "bold"), command=self._start
        )
        self._btn_start.pack(side=tk.LEFT, padx=4)

        self._btn_stop = tk.Button(
            ctrl, text="■ 記録終了", bg="#c0392b", fg="white",
            activebackground="#e74c3c", relief=tk.FLAT,
            width=12, font=("", 10, "bold"), state=tk.DISABLED, command=self._stop
        )
        self._btn_stop.pack(side=tk.LEFT, padx=4)

        self._btn_replay = tk.Button(
            ctrl, text="▶ リプレイ", bg="#2980b9", fg="white",
            activebackground="#3498db", relief=tk.FLAT,
            width=12, font=("", 10, "bold"), command=self._start_replay
        )
        self._btn_replay.pack(side=tk.LEFT, padx=4)

        self._lbl_status = tk.Label(
            ctrl, text="状態: 未起動", fg="#ecf0f1", bg="#2c3e50",
            font=("", 10), anchor="w"
        )
        self._lbl_status.pack(side=tk.LEFT, padx=12)

        self._lbl_match = tk.Label(
            ctrl, text="試合 #0", fg="#bdc3c7", bg="#2c3e50", font=("", 9)
        )
        self._lbl_match.pack(side=tk.RIGHT, padx=8)

        # ── 試合情報バー ──
        info = tk.Frame(self.root, bg="#f7f9fc", pady=3, padx=8,
                        relief=tk.GROOVE, bd=1)
        info.pack(fill=tk.X)

        tk.Label(info, text="ターン:", bg="#f7f9fc", font=("", 9)).pack(side=tk.LEFT)
        self._lbl_turn_num = tk.Label(info, text="-", bg="#f7f9fc",
                                       font=("", 9, "bold"), width=4)
        self._lbl_turn_num.pack(side=tk.LEFT)

        tk.Label(info, text="  現在:", bg="#f7f9fc", font=("", 9)).pack(side=tk.LEFT)
        self._lbl_turn_player = tk.Label(info, text="-", bg="#f7f9fc",
                                          font=("", 9, "bold"), fg="#2980b9")
        self._lbl_turn_player.pack(side=tk.LEFT)

        tk.Label(info, text="  ", bg="#f7f9fc").pack(side=tk.LEFT)
        self._lbl_order = tk.Label(info, text="-", bg="#f7f9fc",
                                    font=("", 9, "bold"), fg="#7d3c98")
        self._lbl_order.pack(side=tk.LEFT)

        self._lbl_result = tk.Label(info, text="", bg="#f7f9fc",
                                     font=("", 10, "bold"))
        self._lbl_result.pack(side=tk.RIGHT, padx=8)

        # ── カードリスト（2列） ──
        cards_frame = tk.Frame(self.root, padx=8, pady=6)
        cards_frame.pack(fill=tk.BOTH, expand=True)

        my_frame = tk.LabelFrame(cards_frame, text="自分のカード",
                                  font=("", 9, "bold"), fg="#27ae60")
        my_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        self._my_list = tk.Listbox(my_frame, height=10, width=26,
                                    font=("Meiryo", 9), selectmode=tk.BROWSE,
                                    activestyle="none")
        _sb_my = tk.Scrollbar(my_frame, command=self._my_list.yview)
        self._my_list.config(yscrollcommand=_sb_my.set)
        _sb_my.pack(side=tk.RIGHT, fill=tk.Y)
        self._my_list.pack(fill=tk.BOTH, expand=True)

        en_frame = tk.LabelFrame(cards_frame, text="相手のカード",
                                  font=("", 9, "bold"), fg="#c0392b")
        en_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        self._en_list = tk.Listbox(en_frame, height=10, width=26,
                                    font=("Meiryo", 9), selectmode=tk.BROWSE,
                                    activestyle="none")
        _sb_en = tk.Scrollbar(en_frame, command=self._en_list.yview)
        self._en_list.config(yscrollcommand=_sb_en.set)
        _sb_en.pack(side=tk.RIGHT, fill=tk.Y)
        self._en_list.pack(fill=tk.BOTH, expand=True)

        # ── デッキ推定 ──
        deck_frame = tk.Frame(self.root, bg="#eaf4ff", padx=10, pady=5,
                               relief=tk.GROOVE, bd=1)
        deck_frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        tk.Label(deck_frame, text="推定デッキ:", bg="#eaf4ff",
                 font=("", 9)).pack(side=tk.LEFT)
        self._lbl_deck = tk.Label(
            deck_frame, text="-", fg="#1a5276",
            font=("Meiryo", 11, "bold"), bg="#eaf4ff"
        )
        self._lbl_deck.pack(side=tk.LEFT, padx=6)
        self._lbl_deck_pct = tk.Label(
            deck_frame, text="", fg="#5d6d7e", bg="#eaf4ff", font=("", 9)
        )
        self._lbl_deck_pct.pack(side=tk.LEFT)

        # ── ログ ──
        log_outer = tk.LabelFrame(self.root, text="ログ", font=("", 9),
                                   padx=4, pady=4)
        log_outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self._log_text = tk.Text(
            log_outer, height=7, state=tk.DISABLED,
            font=("Consolas", 8), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", relief=tk.FLAT
        )
        _sb_log = tk.Scrollbar(log_outer, command=self._log_text.yview)
        self._log_text.config(yscrollcommand=_sb_log.set)
        _sb_log.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.pack(fill=tk.BOTH, expand=True)

        self.root.geometry("560x580")

    # ──────────────────────────────────────────
    # ログ出力
    # ──────────────────────────────────────────
    def _log(self, msg: str, color: str = "#d4d4d4"):
        self._log_text.config(state=tk.NORMAL)
        self._log_text.insert(tk.END, msg + "\n")
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    # ──────────────────────────────────────────
    # ボタン操作
    # ──────────────────────────────────────────
    def _start(self):
        self._btn_start.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL)
        self._lbl_status.config(text="状態: 初期化中...")
        self._log("▶ 記録開始")

        self._tracker = GameTracker(self._queue)
        self._tracker_thread = threading.Thread(
            target=self._tracker.run, daemon=True
        )
        self._tracker_thread.start()

    def _start_replay(self):
        path = filedialog.askopenfilename(
            title="リプレイ動画を選択",
            filetypes=[("動画ファイル", "*.mp4 *.avi *.mkv *.mov"), ("すべて", "*.*")]
        )
        if not path:
            return
        self._btn_start.config(state=tk.DISABLED)
        self._btn_replay.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL)
        self._lbl_status.config(text="状態: リプレイ中...")
        self._log(f"▶ リプレイ開始: {path}")

        self._tracker = GameTracker(self._queue, replay_path=path)
        self._tracker_thread = threading.Thread(
            target=self._tracker.run, daemon=True
        )
        self._tracker_thread.start()

    def _stop(self):
        if self._tracker:
            self._tracker.stop()
        self._btn_start.config(state=tk.NORMAL)
        self._btn_replay.config(state=tk.NORMAL)
        self._btn_stop.config(state=tk.DISABLED)
        self._lbl_status.config(text="状態: 停止")
        self._log("■ 記録終了")

    # ──────────────────────────────────────────
    # イベントポーリング（GUI スレッド）
    # ──────────────────────────────────────────
    def _poll(self):
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                self._handle(event)
            except queue.Empty:
                break
        self.root.after(100, self._poll)

    def _handle(self, event: dict):
        kind = event["type"]

        if kind == "STATUS":
            self._lbl_status.config(text=f"状態: {event['msg']}")

        elif kind == "BATTLE_START":
            self._match_count = event.get("match", self._match_count + 1)
            self._lbl_match.config(text=f"試合 #{self._match_count}")
            self._my_list.delete(0, tk.END)
            self._en_list.delete(0, tk.END)
            self._lbl_deck.config(text="-")
            self._lbl_deck_pct.config(text="")
            self._lbl_turn_num.config(text="-")
            self._lbl_turn_player.config(text="-")
            self._lbl_order.config(text="-")
            self._lbl_result.config(text="")
            self._log(f"\n{'='*36}\n  試合 #{self._match_count} 開始\n{'='*36}")

        elif kind == "ORDER_DETERMINED":
            order = event["order"]
            color = "#1a6b3c" if order == "先攻" else "#7d3c98"
            self._lbl_order.config(text=order, fg=color)
            self._log(f"  ▷ {order}")

        elif kind == "TURN_CHANGE":
            turn = event["turn"]
            player_label = "自分" if event["player"] == "YOUR_TURN" else "相手"
            color = "#27ae60" if event["player"] == "YOUR_TURN" else "#c0392b"
            self._lbl_turn_num.config(text=str(turn))
            self._lbl_turn_player.config(text=f"{player_label}のターン", fg=color)

        elif kind == "CARD_PLAYED":
            player = event["player"]
            name   = event["card"]
            turn   = event["turn"]
            if player == "YOUR_TURN":
                self._my_list.insert(tk.END, f"T{turn:02d}  {name}")
                tag = "【自分】"
            else:
                self._en_list.insert(tk.END, f"T{turn:02d}  {name}")
                tag = "【相手】"
            self._log(f"  [T{turn:02d}] {tag} {name}")

        elif kind == "DECK_INFERRED":
            deck = event["deck"]
            pct  = event["pct"]
            self._lbl_deck.config(text=deck)
            self._lbl_deck_pct.config(text=f"({pct:.0f}% 合致)")

        elif kind == "MATCH_END":
            result = event.get("result", "不明")
            order  = event.get("order",  "不明")
            deck   = event.get("deck",   "-")
            pct    = event.get("pct",    0.0)
            result_color = "#27ae60" if result == "WIN" else "#c0392b" if result == "LOSE" else "#7f8c8d"
            self._lbl_result.config(text=result, fg=result_color)
            self._log(f"  結果: {result}  {order}  推定: {deck} ({pct:.0f}%)")

        elif kind == "LOG_SAVED":
            self._log(f"  保存: {event['path']}")

        elif kind == "LOG":
            self._log(f"  {event['msg']}")

    # ──────────────────────────────────────────
    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    DMTrackGUI().run()
