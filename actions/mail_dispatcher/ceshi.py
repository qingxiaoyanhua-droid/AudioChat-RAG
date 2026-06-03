from actions.mail_dispatcher.dispatch_to_gitea import dispatch_master_issue

meeting_result = {
  "meeting_info": {
    "title": "周会行动项（自动派发）",
    "date": "2026-01-29"
  },
  "action_items": [
    "张三：完成接口联调",
    "李四：整理会议纪要并同步",
    "王五：下周一前输出测试结论",
    "张三：跟进服务部署状态"
  ]
}

# 中文名 -> Gitea 用户名映射
owner_to_gitea = {
    "张三": "zhangsan",
    "李四": "lisi",
    "王五": "wangwu"
}

url = dispatch_master_issue(meeting_result, owner_to_gitea)
print("✅ 群发完成，Issue 链接：", url)