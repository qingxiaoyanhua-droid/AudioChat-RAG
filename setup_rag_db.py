"""
RAG 数据库搭建脚本

初始化 ChromaDB 向量数据库，导入会议记录数据

用法:
    # 从零开始搭建
    python setup_rag_db.py \
        --storage_dir ./rag_storage \
        --data_dir data/meeting_records

    # 添加数据到已有数据库
    python setup_rag_db.py \
        --storage_dir ./rag_storage \
        --data_dir data/meeting_records \
        --mode append
"""

import os
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

from audiochat.rag.storage import MeetingMemoryStore, MeetingDocument
from audiochat.rag.retriever import AudioChatRetriever


def load_meeting_data(data_dir: str) -> List[Dict]:
    """
    加载会议数据

    支持格式:
    1. JSON 文件：每个文件一个会议
    2. JSONL 文件：每行一个会议记录
    3. TXT 文件：简单文本格式
    """
    data_dir = Path(data_dir)
    all_records = []

    # 查找所有数据文件
    json_files = list(data_dir.glob("*.json"))
    jsonl_files = list(data_dir.glob("*.jsonl"))
    txt_files = list(data_dir.glob("*.txt"))

    print(f"找到 {len(json_files)} 个 JSON 文件")
    print(f"找到 {len(jsonl_files)} 个 JSONL 文件")
    print(f"找到 {len(txt_files)} 个 TXT 文件")

    # 加载 JSON 文件
    for json_file in json_files:
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                all_records.extend(data)
            else:
                all_records.append(data)
        print(f"  已加载：{json_file.name}")

    # 加载 JSONL 文件
    for jsonl_file in jsonl_files:
        with open(jsonl_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    record = json.loads(line)
                    all_records.append(record)
        print(f"  已加载：{jsonl_file.name}")

    # 加载 TXT 文件（简单格式）
    for txt_file in txt_files:
        records = parse_txt_file(txt_file)
        all_records.extend(records)
        print(f"  已加载：{txt_file.name}")

    return all_records


def parse_txt_file(txt_file: Path) -> List[Dict]:
    """解析 TXT 格式的会议记录"""
    records = []
    meeting_id = txt_file.stem

    with open(txt_file, "r", encoding="utf-8") as f:
        current_speaker = None
        current_text = []
        current_timestamp = None

        for line in f:
            line = line.strip()
            if not line:
                continue

            # 检测说话人格式："张三：..." 或 "张三："
            if "：" in line or ":" in line:
                # 保存之前的记录
                if current_speaker and current_text:
                    records.append({
                        "meeting_id": meeting_id,
                        "speaker": current_speaker,
                        "content": " ".join(current_text),
                        "timestamp": current_timestamp or datetime.now().isoformat()
                    })

                # 解析新的说话人
                parts = line.split(":", 1) if ":" in line else line.split(":", 1)
                if len(parts) == 2:
                    current_speaker = parts[0].strip()
                    current_text = [parts[1].strip()]
                    current_timestamp = datetime.now().isoformat()
            else:
                # 继续当前说话人的内容
                if current_speaker:
                    current_text.append(line)

        # 保存最后一条记录
        if current_speaker and current_text:
            records.append({
                "meeting_id": meeting_id,
                "speaker": current_speaker,
                "content": " ".join(current_text),
                "timestamp": current_timestamp or datetime.now().isoformat()
            })

    return records


def build_rag_database(
    storage_dir: str,
    data_dir: str,
    mode: str = "create"
) -> dict:
    """
    构建 RAG 数据库

    Args:
        storage_dir: 存储目录
        data_dir: 数据目录
        mode: create（新建）或 append（追加）

    Returns:
        统计信息
    """
    print("=" * 60)
    print("RAG 数据库搭建")
    print("=" * 60)
    print(f"存储目录：{storage_dir}")
    print(f"数据目录：{data_dir}")
    print(f"模式：{mode}")
    print("=" * 60)

    # 初始化存储
    if mode == "create":
        print("\n初始化新的存储...")
        # 如果已存在则删除
        import shutil
        if os.path.exists(storage_dir):
            shutil.rmtree(storage_dir)
            print(f"  已删除旧目录：{storage_dir}")

    store = MeetingMemoryStore(persist_dir=storage_dir)
    print(f"  存储已初始化：{storage_dir}")

    # 加载数据
    print("\n加载会议数据...")
    records = load_meeting_data(data_dir)
    print(f"共加载 {len(records)} 条会议记录")

    if not records:
        print("警告：未找到任何数据")
        return {"total_documents": 0}

    # 转换为 MeetingDocument
    print("\n转换为文档格式...")
    documents = []
    for record in records:
        doc = MeetingDocument(
            content=record.get("content", ""),
            meeting_id=record.get("meeting_id", "unknown"),
            speaker=record.get("speaker"),
            timestamp=record.get("timestamp", datetime.now().isoformat()),
            metadata=record.get("metadata", {})
        )
        documents.append(doc)

    # 批量添加到存储
    print("\n添加到 ChromaDB...")
    doc_ids = store.add_batch(documents)
    print(f"  已添加 {len(doc_ids)} 个文档")

    # 获取统计信息
    stats = store.get_stats()

    print("\n" + "=" * 60)
    print("RAG 数据库搭建完成！")
    print("=" * 60)
    print(f"文档总数：{stats['total_documents']}")
    print(f"存储目录：{stats['persist_dir']}")
    print("=" * 60)

    return stats


def test_retrieval(storage_dir: str, test_queries: List[str]):
    """测试检索功能"""
    print("\n测试检索功能...")
    print("-" * 60)

    store = MeetingMemoryStore(persist_dir=storage_dir)
    retriever = AudioChatRetriever(store)

    for query in test_queries:
        print(f"\n查询：{query}")
        results = retriever.retrieve(query, k=3)

        for i, result in enumerate(results, 1):
            print(f"  [{i}] 说话人：{result.speaker}")
            print(f"      内容：{result.content[:100]}...")
            print(f"      分数：{result.relevance_score:.4f}")

    print("-" * 60)


def main():
    parser = argparse.ArgumentParser(description="RAG 数据库搭建")
    parser.add_argument(
        "--storage_dir",
        type=str,
        default="./rag_storage",
        help="RAG 存储目录"
    )
    parser.add_argument(
        "--data_dir",
        type=str,
        default="data/meeting_records",
        help="会议数据目录"
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["create", "append"],
        default="create",
        help="模式：create（新建）或 append（追加）"
    )
    parser.add_argument(
        "--test_queries",
        type=str,
        nargs="+",
        default=["后端进度如何？", "前端完成情况", "测试覆盖率目标"],
        help="测试查询列表"
    )
    parser.add_argument(
        "--skip_test",
        action="store_true",
        help="跳过检索测试"
    )

    args = parser.parse_args()

    # 构建数据库
    stats = build_rag_database(
        storage_dir=args.storage_dir,
        data_dir=args.data_dir,
        mode=args.mode
    )

    # 测试检索
    if not args.skip_test and stats["total_documents"] > 0:
        test_retrieval(args.storage_dir, args.test_queries)

    print("\n✅ RAG 数据库搭建完成！")
    print(f"\n后续使用:")
    print(f"  python scripts/offline_pipeline.py --audio <audio_path> --enable-rag --meeting-id <meeting_id>")


if __name__ == "__main__":
    main()
