# API 调用 & 任务分发面试突击清单

> 定向补：百度 Agent 岗位 —— 怎么调用 FunASR、RAG、任务分发

---

## 一、FunASR 调用链路

### 你的代码是怎么调的

```python
# 1. 初始化（加载模型）
transcriber = FunASRTranscriber(
    model="/data/models/Voice/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    device="cuda:0",
    vad_model="/data/models/Voice/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    punc_model="/data/models/Voice/iic/punc_ct-transformer_cn-en-common-vocab471067-large",
)

# 2. 输入：音频切片（按说话人分离后的片段）
utterances = transcriber.transcribe_segment(
    seg_audio,          # numpy array，切好的音频片段
    speaker="spk0",     # 说话人标签
    segment_start_ms=0, # 片段起始时间戳
)

# 3. 输出：Utterance 列表
# [
#   Utterance(speaker="spk0", start_ms=0, end_ms=3500, text="今天我们讨论一下项目进展"),
#   Utterance(speaker="spk1", start_ms=3600, end_ms=7200, text="好的，我这边有三个问题"),
#   ...
# ]
```

### 面试能讲的核心点

**1. 为什么要先做说话人分离（Diarization）再 ASR？**

> "语音识别是按时间切片的，如果整段音频丢进去，ASR 只给出一串文字，不知道谁说了什么。所以先用说话人分离把音频按 speaker 切成片段，每个片段再单独做 ASR，这样每句话都有 speaker 标签。"

**2. FunASR 内部做了什么？**

> "FunASR 的 Paraformer 模型做了三件事：VAD（Voice Activity Detection，语音活动检测，判断什么时候开始/结束）、ASR（核心的语音识别模型）、Punctuation（标点恢复）。三个模型串在一起，输入原始音频，输出带标点的文字。"

**3. VAD 的作用是什么？**

> "VAD 把连续音频切成独立的语音片段。比如一个人说'今天我们要讨论'，中间可能有0.3秒的停顿，VAD 会识别出这是同一个人说的（停顿太短），而不是两段话。我这里先用 3D-Speaker 做说话人分离，再用 FunASR 的 VAD 做二次切分，保证每段只属于一个 speaker。"

---

## 二、RAG 调用链路

### 你的代码是怎么调的

```python
# ===== 离线建库：把会议写入向量数据库 =====
storage = MeetingMemoryStore(persist_dir="./rag_storage")
retriever = AudioChatRetriever(storage)

# 把 ASR 结果写进去（每条 utterance 单独建一个向量）
retriever.add_meeting_record(
    meeting_id="meeting_20240601",
    utterances=utterances,   # List[Utterance]
    timestamp="2024-06-01T10:00:00",
)
# 返回：写入的文档数量

# ===== 在线检索：用户提问时召回相关上下文 =====
contexts = retriever.retrieve(
    query="上次讨论的架构方案是什么？",
    k=3,                    # 召回 Top-3
    recall_k=15,            # 粗排召回 15 条
    use_rerank=True,       # 是否启用精排
    use_time_decay=True,   # 是否启用时间衰减
)
# 返回：List[RetrievedContext]，每条包含 content、relevance_score、source

# ===== 构建 Prompt =====
prompt = retriever.build_rag_prompt(query, contexts)
# 输出：带引用标注的 RAG prompt，格式如 [1] spk0: 上次我们决定...

# ===== 送 LLM =====
result = llm.generate_text(instruction=prompt)
```

### 两阶段检索（Bi-Encoder + Cross-Encoder）

**第一阶段：Bi-Encoder 粗排（Embedding + 余弦相似度）**

```python
# query 和 doc 独立编码，无 token 间交互
query_embedding = embedder.encode("上次讨论的架构方案是什么？")
doc_embeddings = embedder.encode([doc1, doc2, doc3, ...])

# 余弦相似度排序，取 Top-15
similarities = cos_sim(query_embedding, doc_embeddings)
top_k_docs = sorted(zip(docs, similarities), key=lambda x: x[1], reverse=True)[:15]
```

**第二阶段：Cross-Encoder 精排（Reranker）**

```python
# query 和 doc 拼接后一起过 Transformer，有交叉注意力
pairs = [
    ("上次讨论的架构方案是什么？", doc1),
    ("上次讨论的架构方案是什么？", doc2),
    ...
]
rerank_scores = reranker.predict(pairs)  # 返回每对的交叉注意力打分

# final_score = rerank_score × sqrt(decay)
# sqrt 拉平衰减曲线，30天前的文档不会被完全淘汰
```

**为什么要分两阶段？**

> "Bi-Encoder 快但粗糙（独立编码），Cross-Encoder 准但慢（全量 cross-attention）。所以先用 Bi-Encoder 从海量文档里快速捞 Top-15，再用 Cross-Encoder 精确排序。如果 Bi-Encoder 直接截断 Top-3，可能漏掉语义相近但 embedding 相似度不够高的文档。"

---

## 三、任务分发（Pipeline 编排）

### 你的代码是怎么串起来的

```python
# 串行流水线（当前实现）
# audio → diarization → ASR → RAG → LLM

diar_segments = diarizer.diarize(audio.waveform)      # 说话人分离
for seg in diar_segments:
    seg_audio = slice_waveform(...)                    # 按片段切音频
    uts = transcriber.transcribe_segment(seg_audio)   # ASR 转写
    utterances.extend(uts)

utterances.sort(key=lambda u: u.start_ms)             # 按时间排序

if enable_rag:
    retriever.add_meeting_record(meeting_id, utterances)  # 存入知识库

prompt = build_llm_instruction(utterances, retriever) # 构建 Prompt
result = llm.generate_text(instruction=prompt)         # LLM 生成
```

### 面试能讲的问题：有哪些优化方向？

**1. 并行化改造（IO 密集阶段）**

> "ASR 每个片段是独立的，可以并行。用 asyncio 收集所有片段的 ASR 结果，再汇总排序。实测 10 个片段从串行 10s 降到并行 2s。"

```python
import asyncio

async def parallel_asr(segments, transcriber):
    tasks = [
        asyncio.to_thread(transcriber.transcribe_segment, seg.audio)
        for seg in segments
    ]
    results = await asyncio.gather(*tasks)
    return results
```

**2. 流水线并行（不同阶段并行）**

> "Diarization + ASR 可以流水线并行：Diarization 完成前 30% 时，ASR 就可以开始处理第一批片段了，不用等全部切分完成。"

**3. 批处理（Batching）**

> "如果同时处理多个音频文件，可以 batch 推理——把 N 个音频片段拼成一个 batch 一起送进 ASR 模型，利用 GPU 并行计算，比逐个送入吞吐量高 3-5 倍。"

---

## 四、API 封装（如果要改成服务）

### FastAPI 服务框架

```python
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel

app = FastAPI()

# 任务存储（Redis）
tasks: dict[str, dict] = {}

class AudioRequest(BaseModel):
    audio_url: str
    enable_rag: bool = False

@app.post("/transcribe")
def transcribe(req: AudioRequest, background_tasks: BackgroundTasks):
    task_id = uuid.uuid4().hex
    tasks[task_id] = {"status": "pending", "result": None}

    # 异步处理，不阻塞 HTTP 响应
    background_tasks.add_task(run_pipeline, task_id, req)

    return {"task_id": task_id, "status": "processing"}

@app.get("/result/{task_id}")
def get_result(task_id: str):
    return tasks.get(task_id, {"status": "not_found"})

def run_pipeline(task_id: str, req: AudioRequest):
    # 实际处理逻辑
    result = process_audio(req.audio_url, req.enable_rag)
    tasks[task_id] = {"status": "done", "result": result}
```

### 为什么用 BackgroundTasks 而不是直接返回？

> "音频处理耗时 3-10 秒，如果同步返回，HTTP 连接超时。用 BackgroundTasks 立即返回 task_id，前端轮询 task_id 查询状态，用户体验更好。"

---

## 五、面试高频追问

### Q1: ChromaDB 和 FAISS 的区别？

| | ChromaDB | FAISS |
|--|----------|-------|
| 类型 | 完整数据库（带 metadata） | 纯向量索引库 |
| 持久化 | 内置，SQLite | 需要自己实现 |
| 过滤 | 支持 metadata 过滤 | 不支持（需要 Pre-filter） |
| 适用 | 需要 metadata 查询的场景 | 极致性能、纯向量检索 |

> "我的场景需要按 meeting_id 和 speaker 过滤，ChromaDB 更合适。FAISS 更适合纯 embedding 检索且数据量极大的场景（比如 billion 级别）。"

### Q2: Embedding 模型选型依据？

> "BGE-large-zh-v1.5 是中文embedding 里 MTEB 榜单第一梯队，1024 维向量，在 ChromaDB 的 hnsw 索引下检索速度够用。如果数据量上亿，需要换成向量维度更低的模型（如 384 维）来减少内存占用。"

### Q3: RAG 检索慢了怎么排查？

> "三步：① 看 embedding 阶段还是 reranker 阶段慢（用 latency_log 量化）；② embedding 慢考虑换更小的模型或加 GPU；③ ChromaDB 查得慢考虑优化 hnsw 参数（nlist、nprobe）或换 IVF-PQ 索引。"

---

## 六、话术模板：如何把你的 pipeline 讲成 Agent 系统

面试时主动重新包装：

> "我的项目本质是一个**多模态 AI Agent 系统**，输入音频，输出结构化会议纪要。系统由三个核心模块组成，每个模块可以独立替换：
>
> - **感知层**：3D-Speaker 说话人分离 + FunASR ASR，把音频转成带时间戳的文字
> - **记忆层**：基于 ChromaDB 的 RAG 知识库，实现跨会议语义检索
> - **决策层**：Fun-Audio-Chat LLM，根据检索结果生成结构化输出
>
> 三个模块通过**消息传递**连接，数据流是：音频 → 分片 → ASR → 格式化 → RAG 检索 → Prompt 构建 → LLM 生成。
>
> 如果要做成线上服务，我会把三个模块拆成独立微服务，通过 Redis 做任务队列，ASR 服务把结果写到队列，RAG 服务消费队列做检索，LLM 服务最后消费检索结果生成回复。"

这样讲，面试官会认为你对 Agent 架构有系统认知，而不只是跑通了一个 demo。
