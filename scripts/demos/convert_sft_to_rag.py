"""
将 SFT 数据转换为 RAG 数据库数据

用途：把 SFT 训练数据转换成 RAG 知识库可以存储的格式

使用方法：
    python3 convert_sft_to_rag.py
"""

import json
import re
from pathlib import Path
from datetime import datetime


def parse_meeting_text(text: str):
    """
    从 SFT 数据的会议记录中提取说话人 + 内容
    
    输入：
        "张三：后端 API 完成 80%\n李四：前端页面完成 60%"
    
    输出：
        [
            {"speaker": "张三", "content": "后端 API 完成 80%"},
            {"speaker": "李四", "content": "前端页面完成 60%"}
        ]
    """
    records = []
    
    # 按行分割
    lines = text.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # 匹配说话人格式："张三：..." 或 "张三 : ..."
        match = re.match(r'^(.+?)[:：]\s*(.+)$', line)
        if match:
            speaker = match.group(1).strip()
            content = match.group(2).strip()
            records.append({
                "speaker": speaker,
                "content": content
            })
    
    return records


def convert_sft_to_rag(sft_data_path: str, output_dir: str):
    """
    将 SFT 数据转换为 RAG 数据
    
    参数：
        sft_data_path: SFT 数据文件路径
        output_dir: RAG 数据输出目录
    """
    print("=" * 60)
    print("SFT 数据 → RAG 数据转换")
    print("=" * 60)
    
    # 1. 读取 SFT 数据
    print("\n读取 SFT 数据...")
    sft_data = []
    with open(sft_data_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                sft_data.append(json.loads(line))
    
    print("读取了 {} 条 SFT 数据".format(len(sft_data)))
    
    # 2. 创建输出目录
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # 3. 转换每条 SFT 数据
    print("\n开始转换...")
    rag_documents = []
    
    for i, sft_item in enumerate(sft_data, 1):
        conversation = sft_item.get("conversation", [])
        
        if len(conversation) < 2:
            continue
        
        # 提取 user 内容中的会议记录
        user_content = conversation[0].get("content", "")
        
        # 去掉"请总结以下会议记录："等前缀
        meeting_text = user_content
        for prefix in ["请总结以下会议记录：", "总结会议：", "会议记录："]:
            meeting_text = meeting_text.replace(prefix, "").strip()
        
        # 解析说话人 + 内容
        records = parse_meeting_text(meeting_text)
        
        if not records:
            continue
        
        # 为每条记录创建 RAG 文档
        for j, record in enumerate(records):
            rag_doc = {
                "content": record["content"],
                "meeting_id": "meeting_{:04d}".format(i),
                "speaker": record["speaker"],
                "timestamp": datetime.now().isoformat(),
                "start_ms": j * 300000,  # 模拟时间戳
                "end_ms": (j + 1) * 300000
            }
            rag_documents.append(rag_doc)
        
        if i % 20 == 0:
            print("已转换 {} 条...".format(i))
    
    # 4. 保存 RAG 数据
    print("\n保存 RAG 数据...")
    
    # 方式 1: 保存为单个 JSON 文件
    all_file = output_path / "all_rag_documents.json"
    with open(all_file, 'w', encoding='utf-8') as f:
        json.dump(rag_documents, f, ensure_ascii=False, indent=2)
    print("已保存：{}".format(all_file))
    
    # 方式 2: 按会议保存为多个 JSON 文件
    meeting_docs = {}
    for doc in rag_documents:
        meeting_id = doc["meeting_id"]
        if meeting_id not in meeting_docs:
            meeting_docs[meeting_id] = []
        meeting_docs[meeting_id].append(doc)
    
    for meeting_id, docs in meeting_docs.items():
        meeting_file = output_path / "{}.json".format(meeting_id)
        with open(meeting_file, 'w', encoding='utf-8') as f:
            json.dump({
                "meeting_id": meeting_id,
                "records": docs
            }, f, ensure_ascii=False, indent=2)
    
    print("已保存 {} 个会议文件到：{}".format(len(meeting_docs), output_path))
    
    # 5. 统计信息
    print("\n" + "=" * 60)
    print("转换完成！")
    print("=" * 60)
    print("SFT 数据：{} 条".format(len(sft_data)))
    print("RAG 文档：{} 条".format(len(rag_documents)))
    print("会议数量：{} 个".format(len(meeting_docs)))
    print("平均每个会议：{:.1f} 条记录".format(len(rag_documents) / len(meeting_docs)))
    print("=" * 60)


if __name__ == "__main__":
    # 转换 SFT 数据到 RAG 数据
    convert_sft_to_rag(
        sft_data_path="data/sft_train_data.jsonl",
        output_dir="data/rag_from_sft"
    )
    
    # 查看转换结果
    print("\n查看转换结果示例:")
    with open("data/rag_from_sft/meeting_0001.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
        print(json.dumps(data, ensure_ascii=False, indent=2)[:500])
