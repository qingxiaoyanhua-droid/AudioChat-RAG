"""
测试 RAG 检索
"""

from audiochat.rag.meeting_retriever import MeetingRetriever, MeetingRecord

retriever = MeetingRetriever()

records = [
    MeetingRecord(
        content="张三：后端 API 已完成 80%",
        speaker="张三",
        meeting_id="test_001",
        timestamp="2024-12-15T10:23:45",
        start_ms=123450,
        end_ms=156780
    ),
]

retriever.add_batch(records)
results = retriever.retrieve("后端进度", k=1)

print(f"检索到 {len(results)} 条结果")
for r in results:
    print(f"  - {r.content}")
