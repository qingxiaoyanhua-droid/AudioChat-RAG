# LLM 系统分布式架构：理论与代码对照

> 本文件展示：后端技术如何落地到 LLM/Agent 系统的每一行代码
> 注意：不同场景下技术选型不同，本项目适合"异步解耦"，不适合"LLM 输出缓存"

---

## 0. 先说清楚：你的场景适合什么

| 技术 | Q&A 机器人 | 会议总结（本项目） |
|------|-----------|------------------|
| Redis LLM 缓存 | 非常适合 | 不适合，每次会议都不同 |
| Redis 任务状态 | 适合 | **适合，核心用途** |
| Redis 限流 | 适合 | **适合，核心用途** |
| Kafka 异步解耦 | 适合 | **核心，核心用途** |
| 服务注册 | 适合 | 适合 |
| 熔断 | 适合 | 适合 |

**本项目的核心价值是 Kafka 异步解耦 + Redis 任务状态**，不是缓存。

---

## 1. Redis — 任务状态存储 + 限流（本项目真正有用的两个场景）

### 1a. 任务状态存储（核心用途）

每次音频处理耗时 3-10 秒，用户不可能同步等待。
用 Redis 存任务状态，前端轮询 task_id 获取结果：

```python
import redis

task_store = redis.Redis(host="localhost", port=6379, decode_responses=True)

def create_task(task_id: str, audio_url: str) -> None:
    task_store.hset(f"task:{task_id}", mapping={
        "status": "pending",
        "audio_url": audio_url,
    })

def update_task(task_id: str, status: str, result: str = "") -> None:
    task_store.hset(f"task:{task_id}", mapping={
        "status": status,
        "result": result,
    })

def get_task(task_id: str) -> dict:
    return task_store.hgetall(f"task:{task_id}")
```

### 1b. 限流（保护服务不被冲垮）

```python
import time
import redis

class SlidingWindowRateLimiter:
    """滑动窗口限流：统计最近 N 秒内的请求数"""

    def __init__(self, redis_client: redis.Redis, key: str,
                 max_requests: int = 10, window_seconds: int = 60):
        self.r = redis_client
        self.key = f"ratelimit:{key}"
        self.max_requests = max_requests
        self.window = window_seconds

    def is_allowed(self, user_id: str) -> bool:
        full_key = f"{self.key}:{user_id}"
        now = time.time()
        window_start = now - self.window

        pipe = self.r.pipeline()
        pipe.zremrangebyscore(full_key, 0, window_start)
        pipe.zcard(full_key)
        results = pipe.execute()
        current_count = results[1]

        if current_count < self.max_requests:
            self.r.zadd(full_key, {str(now): now})
            self.r.expire(full_key, self.window)
            return True
        return False
```

---

## 2. Kafka — 异步解耦（核心，本项目最重要的后端技术）

### 为什么你的场景必须用 Kafka

会议总结链路：音频 -> ASR(3s) -> RAG(1s) -> LLM(5s) = 总共 9 秒。

同步处理的问题：
- HTTP 请求要等 9 秒才返回，连接占满，1000 人同时上传直接爆炸
- 任意一环慢，全部卡死

Kafka 的核心价值：**接收请求和实际处理完全分开**，前端秒回，后台慢慢跑。

### 消息生产者（FastAPI 网关）

```python
from kafka import KafkaProducer
import json

class AudioTaskProducer:
    def __init__(self, bootstrap_servers: list[str] = ["localhost:9092"]):
        self.producer = KafkaProducer(
            bootstrap_servers=bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            acks="all",
            retries=3,
        )

    def submit(self, task_id: str, audio_url: str, enable_rag: bool) -> None:
        message = {
            "task_id": task_id,
            "audio_url": audio_url,
            "enable_rag": enable_rag,
        }
        self.producer.send("audio-tasks", value=message)
        self.producer.flush()
```

### 消息消费者（ASR 服务）

```python
from kafka import KafkaConsumer
from kafka import KafkaProducer

class ASRWorker:
    def __init__(self, bootstrap_servers: list[str] = ["localhost:9092"]):
        self.consumer = KafkaConsumer(
            "audio-tasks",
            bootstrap_servers=bootstrap_servers,
            group_id="asr-workers",
            auto_offset_reset="earliest",
            enable_auto_commit=False,
        )
        self.rag_producer = KafkaProducer(bootstrap_servers=bootstrap_servers)

    def run(self):
        for message in self.consumer:
            data = json.loads(message.value.decode("utf-8"))
            task_id = data["task_id"]
            audio_url = data["audio_url"]

            try:
                utterances = self._run_asr(audio_url)
                update_task(task_id, "asr_done")

                # 发消息给 RAG 服务
                self.rag_producer.send("rag-tasks", value={
                    "task_id": task_id,
                    "utterances": utterances,
                })
                self.rag_producer.flush()

                self.consumer.commit()

            except Exception as exc:
                update_task(task_id, "asr_failed", str(exc))
                self.consumer.commit()

    def _run_asr(self, audio_url: str) -> list:
        from audiochat.asr.funasr_asr import FunASRTranscriber
        transcriber = FunASRTranscriber(...)
        return transcriber.transcribe(audio_url)
```

### 消费者多实例（负载均衡）

这是 Kafka 最核心的价值：**消费者组内多个实例自动分工**。

```
Kafka topic: audio-tasks (3 个 partition)

partition-0 -> [ASR实例-1] 处理
partition-1 -> [ASR实例-2] 处理
partition-2 -> [ASR实例-3] 处理

ASR实例-2 挂了：
partition-0 -> [ASR实例-1] 处理
partition-1 -> 自动分配给 [ASR实例-3] 处理  <- 故障自动转移
partition-2 -> [ASR实例-3] 处理
```

> "Kafka 消费者组机制：每条消息只被一个实例处理，新增机器自动重新分配，机器挂了自动故障转移。"

---

## 3. 架构全图

```
用户上传音频
    |
    v
[FastAPI 网关] 写 Kafka "audio-tasks" -> 立即返回 task_id
    |
    v
[ASR Worker x N] 消费 audio-tasks -> 语音转文字
    |  写 Kafka "rag-tasks"
    v
[RAG Worker x N] 消费 rag-tasks -> 检索历史上下文
    |  写 Kafka "llm-tasks"
    v
[LLM Worker x N] 消费 llm-tasks -> 生成回复
    |  写 Redis task:{task_id} = done + result
    v
用户轮询 task_id -> 获取结果
```

**每个环节完全独立，ASR 慢不影响 RAG，RAG 慢不影响 LLM，前端永远秒回。**

---

## 面试话术精简版（直接背）

> "如果把会议转录系统做成线上服务，我会按处理阶段拆成三个独立微服务：
>
> ASR 服务接收音频请求，结果写入 Kafka 队列；RAG 服务消费队列做历史检索；LLM 服务消费检索结果生成回复。三阶段完全通过 Kafka 解耦，任意一环慢了不会卡住前端，用户上传音频后立即拿到 task_id，后台异步处理。
>
> Redis 在我的场景主要用于两部分：任务状态存储（task_id -> status/result）和限流保护（滑动窗口）。LLM 缓存不适合会议总结场景，因为每场会议内容都不同，query 重复率极低。
>
> 服务注册用 Consul，心跳健康检查，GPU 实例挂了自动摘除。熔断器保护 LLM 调用，连续失败5次自动短路，30秒后试探恢复。"

---

## 面试话术极简版（30秒版本）

> "我的项目天然适合异步架构：音频上传后写 Kafka 立即返回，后台 ASR->RAG->LLM 三阶段串联处理，各阶段完全解耦。Redis 存任务状态，前端轮询 task_id 查结果。限流防止突发流量，熔断防止 LLM 超时拖死全站。"
