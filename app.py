import sys
import asyncio
import threading
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QWidget,
    QPushButton, QLabel, QFileDialog
)
from PyQt5.QtCore import QTimer
import pyqtgraph as pg
from scipy.signal import find_peaks

# BLE 関連ライブラリ
from bleak import BleakClient

# ===== Polar H10 の設定 =====
POLAR_H10_ADDRESS = "7A8C6159-C50B-0651-3075-5411D72CA0E9"
PMD_CONTROL_UUID = "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
PMD_DATA_UUID = "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"
ECG_WRITE = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
HEART_RATE_MEASUREMENT_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# ===== グローバル変数 =====
ecg_session_data = []  # ECG サンプル値
ecg_session_time = []  # サンプルごとの相対時刻（秒）
sample_counter = 0     # サンプル番号
SAMPLING_RATE = 100    # 仮のサンプリングレート（Hz）

current_ble_hr = None  # BLE から取得した最新の心拍数

# 心拍通知ログ (timestamp, heart_rate)
hr_log = []

# 複数スレッドからのアクセス対策用ロック
data_lock = threading.Lock()

# セッション開始時刻
session_start_time = datetime.now()

# BLE 接続状態を示すフラグ
ble_connected = False

# ===== BLE 通知ハンドラ =====
def pmd_data_handler(sender: str, data: bytearray):
    """
    Polar H10 の ECG 通知ハンドラ
    ※ data[10:] に 3 バイト毎の ECG サンプルが連続していると仮定
    """
    if not data:
        return

    if data[0] == 0x00:
        samples = data[10:]
        step = 3
        global sample_counter
        with data_lock:
            offset = 0
            while offset + step <= len(samples):
                # 3バイトを符号付きリトルエンディアン整数に変換
                ecg_value = int.from_bytes(samples[offset:offset + step],
                                           byteorder="little", signed=True)
                ecg_session_data.append(ecg_value)
                # サンプル番号から相対時刻（秒）を算出
                ecg_session_time.append(sample_counter / SAMPLING_RATE)
                sample_counter += 1
                offset += step

def parse_heart_rate_measurement(data: bytearray) -> int:
    """
    Heart Rate Measurement 通知データのパース
    ※ data[0] の下位ビットで 8bit/16bit を判定
    """
    if (data[0] & 0x01) == 0:
        hr = data[1]
    else:
        hr = int.from_bytes(data[1:3], byteorder='little')
    print(f"Heart Rate Measurement: {hr} bpm")
    return hr

def heart_rate_notification_handler(sender: str, data: bytearray):
    """
    Heart Rate Measurement キャラクタリスティックの通知ハンドラ
    ※ 通知毎に現在時刻と心拍値を hr_log に記録
    """
    global current_ble_hr
    hr = parse_heart_rate_measurement(data)
    with data_lock:
        current_ble_hr = hr
        hr_log.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), hr))

# ===== BLE 通信メイン =====
async def ble_main():
    global ble_connected
    print(f"=== Polar H10 ({POLAR_H10_ADDRESS}) への接続を試みます ===")
    async with BleakClient(POLAR_H10_ADDRESS, timeout=30.0) as client:
        ble_connected = True
        print("+++ 接続完了 +++")
        print("=== ECG取得開始コマンドを送信 ===")
        await client.write_gatt_char(PMD_CONTROL_UUID, ECG_WRITE)
        print("=== PMD_DATA_UUID で通知受信を開始 ===")
        await client.start_notify(PMD_DATA_UUID, pmd_data_handler)
        print("=== Heart Rate Measurement 通知を開始 ===")
        await client.start_notify(HEART_RATE_MEASUREMENT_CHAR_UUID, heart_rate_notification_handler)
        print("=== リアルタイム ECG & Heart Rate データ受信中... ===")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            print("=== 通知受信停止 ===")
            await client.stop_notify(PMD_DATA_UUID)
            await client.stop_notify(HEART_RATE_MEASUREMENT_CHAR_UUID)
    ble_connected = False
    print("+++ 切断完了 +++")

def run_ble():
    asyncio.run(ble_main())

# ===== PyQt5 GUI アプリケーション =====
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("リアルタイム ECG & Heart Rate 可視化")
        
        # プロットウィジェット (pyqtgraph)
        self.plot_widget = pg.PlotWidget(title="リアルタイム ECG (最新10秒)")
        self.plot_widget.setLabel('left', "ECG Value")
        self.plot_widget.setLabel('bottom', "Time (s)")
        
        # 心拍数表示ラベル
        self.heart_rate_label = QLabel("未接続")
        self.heart_rate_label.setStyleSheet("font-size: 18px;")
        
        # CSV出力ボタン
        self.csv_button = QPushButton("CSV出力 (統合)")
        self.csv_button.clicked.connect(self.export_csv)
        
        # レイアウト設定
        layout = QVBoxLayout()
        layout.addWidget(self.plot_widget)
        layout.addWidget(self.heart_rate_label)
        layout.addWidget(self.csv_button)
        
        central_widget = QWidget()
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)
        
        # 1秒毎に更新するタイマー
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plot)
        self.timer.start(1000)  # 1000ms 毎に更新

    def update_plot(self):
        with data_lock:
            data = ecg_session_data.copy()
            times = ecg_session_time.copy()
            ble_hr = current_ble_hr

        # 最新10秒間のデータのみ抽出
        window_size = SAMPLING_RATE * 10
        if len(data) > window_size:
            data = data[-window_size:]
            times = times[-window_size:]
        
        self.plot_widget.clear()
        self.plot_widget.plot(times, data, pen='b')
        
        # Rピーク検出（ECG から心拍推定）
        if data:
            data_array = np.array(data)
            peaks, _ = find_peaks(data_array, distance=SAMPLING_RATE * 0.3,
                                  prominence=0.5 * np.std(data_array))
            if len(peaks) > 0:
                peak_times = np.array(times)[peaks]
                peak_values = data_array[peaks]
                self.plot_widget.plot(peak_times, peak_values, pen=None, symbol='o', symbolBrush='r')
        
        # BLE 接続状態に応じた表示
        global ble_connected
        if not ble_connected:
            self.heart_rate_label.setText("未接続")
        else:
            current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.heart_rate_label.setText(
                f"測定 Heart Rate: {ble_hr if ble_hr is not None else '---'} BPM  現在時刻: {current_time_str}"
            )

    def export_csv(self):
        with data_lock:
            ecg_times = ecg_session_time.copy()
            ecg_values = ecg_session_data.copy()
            hr_data_local = list(hr_log)
        
        rows = []
        # ECG サンプルは、セッション開始時刻 + 相対秒数で絶対時刻に変換
        for rel_time, ecg_value in zip(ecg_times, ecg_values):
            abs_time = session_start_time + timedelta(seconds=rel_time)
            rows.append({
                "timestamp": abs_time.strftime("%Y-%m-%d %H:%M:%S.%f"),
                "source": "ECG",
                "value": ecg_value
            })
        # 心拍通知ログを追加
        for ts, hr in hr_data_local:
            rows.append({
                "timestamp": ts,
                "source": "Heart Rate",
                "value": hr
            })
        # タイムスタンプ順にソート
        rows.sort(key=lambda row: pd.to_datetime(row["timestamp"]))
        df = pd.DataFrame(rows)
        
        # ファイル保存ダイアログ
        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getSaveFileName(self, "CSV出力 (統合)", "",
                                                   "CSV Files (*.csv);;All Files (*)",
                                                   options=options)
        if file_path:
            df.to_csv(file_path, index=False)
            print(f"CSVファイルを出力しました: {file_path}")

# ===== メイン処理 =====
if __name__ == "__main__":
    # BLE 通信は別スレッドで実行
    ble_thread = threading.Thread(target=run_ble, daemon=True)
    ble_thread.start()
    
    # PyQt アプリケーションの起動
    app = QApplication(sys.argv)
    main_window = MainWindow()
    main_window.show()
    sys.exit(app.exec_())
