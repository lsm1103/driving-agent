import time
import av
import numpy as np


def decode_opus_file(file_path: str):
    print(f"=== 开始处理文件: {file_path} ===")

    # 1. 记录总任务开始时间（包含文件打开、元数据读取和解码）
    start_total_time = time.perf_counter()

    try:
        container = av.open(file_path)
    except Exception as e:
        print(f"打开文件失败: {e}")
        return

    # 获取首个音频流
    audio_stream = container.streams.audio[0]

    # --- 打印音频元数据信息 ---
    print("\n--- 音频元数据 (Metadata) ---")
    print(f"编码格式 (Codec): {audio_stream.codec_context.name}")
    print(f"采样率 (Sample Rate): {audio_stream.rate} Hz")
    print(f"声道数 (Channels): {audio_stream.channels}")
    print(f"采样格式 (Format): {audio_stream.format.name}")

    # 获取比特率（可能从流中获取，也可能从容器获取）
    bit_rate = audio_stream.bit_rate or container.bit_rate
    if bit_rate:
        print(f"比特率 (Bitrate): {bit_rate / 1000:.2f} kbps")

    # 获取音频时长（单位：秒）
    duration_sec = None
    if audio_stream.duration and audio_stream.time_base:
        duration_sec = float(audio_stream.duration * audio_stream.time_base)
    elif container.duration:
        duration_sec = float(container.duration / av.time_base)

    if duration_sec:
        print(f"音频时长 (Duration): {duration_sec:.2f} 秒")
    else:
        print("音频时长 (Duration): 未知 (流中未包含精确时长)")

    print("-----------------------------\n")

    # --- 开始解码并计时 ---
    start_decode_time = time.perf_counter()

    pcm_chunks = []
    total_samples = 0

    # container.decode(audio=0) 底层由 FFmpeg C 库直接批量解包并解码
    for frame in container.decode(audio=0):
        # frame.to_ndarray() 转化为 NumPy 数组，零拷贝/极低开销
        chunk = frame.to_ndarray()
        pcm_chunks.append(chunk)
        total_samples += frame.samples

    # 拼接所有 PCM 帧为一个连续的 NumPy 数组
    if pcm_chunks:
        full_pcm_data = np.concatenate(pcm_chunks, axis=1 if pcm_chunks[0].ndim > 1 else 0)
    else:
        full_pcm_data = np.array([])

    decode_end_time = time.perf_counter()

    # --- 计算耗时与性能指标 ---
    decode_cost_ms = (decode_end_time - start_decode_time) * 1000
    total_cost_ms = (decode_end_time - start_total_time) * 1000

    print("--- 性能统计 (Performance) ---")
    print(f"纯解码耗时 (Decode Time): {decode_cost_ms:.2f} ms")
    print(f"总处理耗时 (Total Time):  {total_cost_ms:.2f} ms")
    print(f"解码总采样点数 (Samples): {total_samples}")

    # 计算实时率 (Real-time Factor)
    if duration_sec and duration_sec > 0:
        rtf = duration_sec / (decode_cost_ms / 1000)
        print(f"解码倍速 (Speedup):      {rtf:.2f}x (即 1 秒内可解码 {rtf:.2f} 秒音频)")
    print("-----------------------------\n")

    container.close()
    return full_pcm_data


# --- 运行测试 ---
if __name__ == "__main__":
    pcm_output = decode_opus_file("/Users/xm/Downloads/data/音频/1541858678698713088510985e1ed18450ab9c6a5dc837ebb09.opus")