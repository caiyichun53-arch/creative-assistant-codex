"""SenseVoice 样本测试:单条转写看质量(正式批量工作器随后建)。"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess

t0 = time.time()
m = AutoModel(model="iic/SenseVoiceSmall", vad_model="fsmn-vad",
              vad_kwargs={"max_single_segment_time": 30000}, disable_update=True)
print(f"模型加载 {time.time()-t0:.0f}s", file=sys.stderr)

t1 = time.time()
res = m.generate(input=sys.argv[1], language="zh", use_itn=True,
                 batch_size_s=60, merge_vad=True, merge_length_s=15)
text = "".join(rich_transcription_postprocess(r["text"]) for r in res)
print(f"转写 {time.time()-t1:.0f}s, {len(text)}字", file=sys.stderr)
print(text)
