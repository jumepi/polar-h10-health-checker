import asyncio
import threading
from bleak import BleakClient
import numpy as np
import plotly.express as px
from dash import Dash, dcc, html
from dash.dependencies import Input, Output
from scipy.signal import find_peaks
import pandas as pd
from datetime import datetime, timedelta

# ===== Polar H10 の設定 =====
POLAR_H10_ADDRESS = "F219825A-C785-E17A-BB0A-313638FF73B2"

PMD_CONTROL_UUID = "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
PMD_DATA_UUID = "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"
ECG_WRITE = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
HEART_RATE_MEASUREMENT_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# ===== グローバル変数 =====
# ECG サンプル（値）とサンプルごとの相対時刻（秒）を保存
ecg_session_data = []
ecg_session_time = []
sample_counter = 0           # サンプル番号（サンプル数）
SAMPLING_RATE = 100          # 仮のサンプリングレート（Hz）

# BLE から取得した Heart Rate 値（最新値）
current_ble_hr = None

# 心拍通知時のログ (timestamp, heart_rate) を記録
hr_log = []

# 複数スレッドからのアクセス対策用ロック
data_lock = threading.Lock()

# セッション開始時刻（絶対時刻）を記録（これを用いて ECG の相対時刻から絶対時刻を算出）
session_start_time = datetime.now()


# ===== BLE 通知ハンドラ =====
def pmd_data_handler(sender: str, data: bytearray):
    """
    Polar H10 の PMD_DATA_UUID (ECG) 通知受信ハンドラ
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
    ※ 通知を受けるたびに現在時刻と心拍値を hr_log に記録
    """
    global current_ble_hr
    hr = parse_heart_rate_measurement(data)
    with data_lock:
        current_ble_hr = hr
        hr_log.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), hr))


# ===== BLE 通信メイン =====
async def ble_main():
    print(f"=== Polar H10 ({POLAR_H10_ADDRESS}) への接続を試みます ===")
    async with BleakClient(POLAR_H10_ADDRESS, timeout=30.0) as client:
        print("+++ 接続中 +++")
        await client.connect(timeout=20.0)
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
    print("+++ 切断完了 +++")


def run_ble():
    asyncio.run(ble_main())


# ===== Dash アプリの設定 =====
app = Dash(__name__)
app.layout = html.Div([
    html.H1("リアルタイム ECG, R-R間隔 & Heart Rate 可視化"),
    dcc.Graph(id='ecg-graph'),
    html.Div(id='hr-display', style={'fontSize': 24, 'marginTop': 20}),
    # 統合 CSV 出力用のボタンと Download コンポーネント
    html.Button("CSV出力 (統合)", id="btn-combined-csv", n_clicks=0, style={'marginTop': 20}),
    dcc.Download(id="download-combined-csv"),
    dcc.Interval(
        id='interval-component',
        interval=1000,  # 1秒ごとに更新
        n_intervals=0
    )
])


@app.callback(
    [Output('ecg-graph', 'figure'),
     Output('hr-display', 'children')],
    [Input('interval-component', 'n_intervals')]
)
def update_graph(n):
    with data_lock:
        data = ecg_session_data.copy()
        times = ecg_session_time.copy()
        ble_hr = current_ble_hr
    # 最新10秒間のデータのみプロット
    window_size = SAMPLING_RATE * 10
    if len(data) > window_size:
        data = data[-window_size:]
        times = times[-window_size:]
    # DataFrame に変換して Plotly Express に渡す
    df = pd.DataFrame({"time": times, "ecg": data})
    fig = px.line(df, x="time", y="ecg", title="リアルタイム ECG (最新10秒)")
    fig.update_layout(xaxis_title="Time (s)", yaxis_title="ECG Value")
    # Rピーク検出（ECG から心拍数推定）
    if len(data) > 0:
        data_array = np.array(data)
        peaks, _ = find_peaks(data_array, distance=SAMPLING_RATE * 0.3,
                                prominence=0.5 * np.std(data_array))
        fig.add_scatter(x=np.array(times)[peaks],
                        y=data_array[peaks],
                        mode='markers',
                        marker=dict(color='red', size=8),
                        name='R Peaks')

    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    display_text = (
        f"測定 Heart Rate: {ble_hr if ble_hr is not None else '---'} BPM, "
        f"現在時刻: {current_time_str}"
    )
    return fig, display_text


@app.callback(
    Output("download-combined-csv", "data"),
    Input("btn-combined-csv", "n_clicks"),
    prevent_initial_call=True,
)
def generate_combined_csv(n_clicks):
    with data_lock:
        ecg_times = ecg_session_time.copy()
        ecg_values = ecg_session_data.copy()
        hr_data_local = list(hr_log)
    rows = []
    # ECG サンプルは、セッション開始時刻 + 相対秒数 で絶対時刻を算出
    for rel_time, ecg_value in zip(ecg_times, ecg_values):
        abs_time = session_start_time + timedelta(seconds=rel_time)
        rows.append({
            "timestamp": abs_time.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "source": "ECG",
            "value": ecg_value
        })
    # 心拍通知ログは既に絶対時刻（文字列）で記録しているのでそのまま追加
    for ts, hr in hr_data_local:
        rows.append({
            "timestamp": ts,
            "source": "Heart Rate",
            "value": hr
        })
    # タイムスタンプ順にソート
    rows.sort(key=lambda row: pd.to_datetime(row["timestamp"]))
    df = pd.DataFrame(rows)
    return dcc.send_data_frame(df.to_csv, "combined_data.csv", index=False)


# ===== メイン処理 =====
if __name__ == "__main__":
    # BLE 通信は別スレッドで実行（Dash サーバはメインスレッド）
    ble_thread = threading.Thread(target=run_ble, daemon=True)
    ble_thread.start()

    # Dash サーバ起動（use_reloader=False に注意）
    app.run_server(debug=False, use_reloader=False)

# ※ macOS の場合は環境変数 OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES の設定を推奨
