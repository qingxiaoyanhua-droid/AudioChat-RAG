"""
测试 GRPO 奖励函数
"""

from evaluation.grpo_reward import GRPORewardFunction

reward_fn = GRPORewardFunction()

reward = reward_fn.compute_reward(
    generated="后端 API 已完成 80%",
    reference="后端进度 80%",
    query="后端进度如何？"
)

print(f"奖励分数：{reward:.4f}")
