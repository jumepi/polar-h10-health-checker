import asyncio
from bleak import BleakClient
import numpy as np
import plotly.express as px

# すでに判明している Polar H10 のアドレス (macOS では UUID 形式)
POLAR_H10_ADDRESS = "7A8C6159-C50B-0651-3075-5411D72CA0E9"

# Polar H10 の PMD (Physiological Measurement Data) サービス関連 UUID
PMD_CONTROL_UUID = "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
PMD_DATA_UUID    = "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"

# Polar 独自コマンド: ECG取得モードを開始
ECG_WRITE = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])

# 取得したECGデータ (生波形) を蓄積
ecg_session_data = []
ecg_session_time = []

def pmd_data_handler(sender: str, data: bytearray):
    """
    Polar H10 の PMD_DATA_UUID (ECG) 通知を受け取るハンドラ。
    data[0] == 0x00 (ECG) のパケットなら、後続にタイムスタンプとサンプル列が含まれる。
    """
    if len(data) == 0:
        return

    # 先頭バイトでタイプ判別 (0x00 = ECG)
    if data[0] == 0x00:
        # data[1..8]: タイムスタンプ (unsigned long, little endian)
        timestamp = convert_to_unsigned_long(data, 1, 8)
        samples = data[10:]  # data[10..] が生ECGサンプル群
        step = 3  # 1サンプルあたり3バイト

        offset = 0
        while offset + step <= len(samples):
            ecg_value = convert_array_to_signed_int(samples, offset, step)
            offset += step

            ecg_session_data.append(ecg_value)
            ecg_session_time.append(timestamp)
            # 本来はサンプル毎に (1/130秒 なり1/200秒なり) 加算してもよいが簡略化

def convert_array_to_signed_int(data: bytearray, offset: int, length: int) -> int:
    """オフセット～lengthバイト分を符号付きリトルエンディアンで読み取る"""
    return int.from_bytes(
        data[offset : offset + length], byteorder="little", signed=True
    )

def convert_to_unsigned_long(data: bytearray, offset: int, length: int) -> int:
    """オフセット～lengthバイト分を符号なしリトルエンディアンで読み取る"""
    return int.from_bytes(
        data[offset : offset + length], byteorder="little", signed=False
    )

async def main():
    print(f"=== Attempting to connect to {POLAR_H10_ADDRESS} ===")
    # 1) 直接アドレス指定で接続
    async with BleakClient(POLAR_H10_ADDRESS, timeout=30.0) as client:
        # 明示的に connect() しなくても `async with` ブロックで接続されるが、
        # 場合によっては以下のように書いてもよい:
        #   await client.connect(timeout=30.0)
        print("+++ Connected +++")

        # 2) Polar H10 に ECG開始コマンドを書き込む (PMD_CONTROL_UUID へ Write)
        print("=== Write ECG start command ===")
        await client.write_gatt_char(PMD_CONTROL_UUID, ECG_WRITE)

        # 3) PMD_DATA_UUID に Notify を設定して生ECGデータを受信
        print("=== Start Notify on PMD_DATA_UUID ===")
        await client.start_notify(PMD_DATA_UUID, pmd_data_handler)

        # 4) 10秒間待機しながらデータ収集
        print("=== Collecting ECG for 10 seconds... ===")
        await asyncio.sleep(10.0)

        # 5) Notify 停止
        print("=== Stop Notify ===")
        await client.stop_notify(PMD_DATA_UUID)

    print("+++ Disconnected +++")

    # 6) CSV に保存 & Plotly で可視化
    if ecg_session_data:
        print(f"Total {len(ecg_session_data)} ECG samples were collected.")

        # CSV 保存
        np.savetxt("ecg_session_data.csv", ecg_session_data, delimiter=",", fmt="%d")
        np.savetxt("ecg_session_time.csv", ecg_session_time, delimiter=",", fmt="%d")
        print("ECG data saved to CSV.")

        # Plotly で簡易グラフ
        fig = px.line(
            x=list(range(len(ecg_session_data))),
            y=ecg_session_data,
            title="Polar H10 ECG (raw)",
            labels={"x": "Sample Index", "y": "ECG Value"}
        )
        fig.show()
    else:
        print("No ECG data collected.")

if __name__ == "__main__":
    # macOS + Bleak の場合は環境変数設定推奨:
    # OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python health-rate-temp.py
    asyncio.run(main())
