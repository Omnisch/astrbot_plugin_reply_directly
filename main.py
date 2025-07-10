import asyncio
import json
from collections import defaultdict
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.config import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

# 用于存储需要主动回复的目标
# 结构: { "unified_msg_origin": True }
sticky_reply_targets: Dict[str, bool] = {}

# 用于跟踪机器人发言和后续的聊天记录
# 结构: { "unified_msg_origin": [AstrMessageEvent, ...] }
proactive_chat_histories: Dict[str, List[AstrMessageEvent]] = defaultdict(list)


@register(
    "ReplyDirectly",
    "qa296",
    "一个实现沉浸式对话和主动插话的插件",
    "1.0.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly",
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.proactive_tasks: Dict[str, asyncio.Task] = {}
        logger.info("ReplyDirectly 插件已加载。")

    # --- 1. 沉浸式对话功能 (Sticky Reply) ---

    @filter.llm_tool(name="enable_direct_reply_once")
    async def enable_direct_reply_once(
        self, event: AstrMessageEvent
    ) -> Optional[MessageEventResult]:
        """
        当LLM认为当前对话非常投机时调用此函数。
        调用后，机器人将在下一次对话时直接回复（无需@），此效果仅生效一次。
        """
        if not self.config.get("sticky_reply", {}).get("enable", True):
            return None

        session_id = event.unified_msg_origin
        logger.info(f"[{session_id}] 已通过函数工具启用一次性主动回复。")
        sticky_reply_targets[session_id] = True
        # 这个函数工具不需要向用户发送任何消息
        return None

    @filter.on_llm_request(priority=10)
    async def check_and_apply_sticky_reply(self, event: AstrMessageEvent, req):
        """
        在LLM请求前检查是否需要应用一次性主动回复。
        """
        if not self.config.get("sticky_reply", {}).get("enable", True):
            return

        session_id = event.unified_msg_origin
        if sticky_reply_targets.get(session_id):
            logger.info(f"[{session_id}] 应用一次性主动回复, 强制唤醒机器人。")
            event.is_wake = True  # 强制唤醒
            del sticky_reply_targets[session_id]  # 效果仅生效一次

    # --- 2. 主动插话功能 (Proactive Reply) ---

    @filter.after_message_sent(priority=10)
    async def after_bot_sends_message(self, event: AstrMessageEvent):
        """
        当机器人发送消息后触发，用于启动主动插话的监听逻辑。
        """
        if not self.config.get("proactive_reply", {}).get("enable", True):
            return

        # 只在群聊中生效
        if event.is_private_chat():
            return

        session_id = event.unified_msg_origin
        delay = self.config.get("proactive_reply", {}).get("delay_seconds", 5)

        # 如果已有任务在运行，先取消
        if session_id in self.proactive_tasks and not self.proactive_tasks[
            session_id
        ].done():
            self.proactive_tasks[session_id].cancel()
            logger.debug(f"[{session_id}] 取消了旧的主动插话任务。")

        # 重置该群聊的短期历史记录
        proactive_chat_histories[session_id].clear()

        # 创建新的延时任务
        task = asyncio.create_task(self.proactive_check_task(session_id, delay))
        self.proactive_tasks[session_id] = task
        logger.info(f"[{session_id}] 机器人已发言，{delay}秒后将检查是否需要主动插话。")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """
        监听所有群聊消息，如果机器人在最近发言过，则记录下来。
        """
        session_id = event.unified_msg_origin
        # 检查是否有正在等待的主动插话任务
        if (
            session_id in self.proactive_tasks
            and not self.proactive_tasks[session_id].done()
        ):
            logger.debug(f"[{session_id}] 记录到一条群聊消息: {event.message_str}")
            proactive_chat_histories[session_id].append(event)

    async def proactive_check_task(self, session_id: str, delay: int):
        """
        延迟指定时间后，分析收集到的聊天记录，并决定是否插话。
        """
        try:
            await asyncio.sleep(delay)

            history = proactive_chat_histories.get(session_id, [])
            if not history:
                logger.info(f"[{session_id}] {delay}秒内无新消息，不进行主动插话。")
                return

            history_limit = self.config.get("proactive_reply", {}).get(
                "history_limit", 10
            )

            # 格式化聊天记录
            formatted_history = [
                (
                    f"{(msg.get_sender_name() or msg.get_sender_id())}: "
                    f"{msg.message_str}"
                )
                for msg in history[-history_limit:]
            ]
            chat_logs = "\n".join(formatted_history)
            logger.info(
                f"[{session_id}] 开始分析以下聊天记录以决定是否插话:\n---\n{chat_logs}\n---"
            )

            # 准备LLM请求
            system_prompt = f"""
你是一个群聊助手，你的任务是分析一段在你发言之后的聊天记录，并决定是否需要主动插话。
聊天记录如下：
---
{chat_logs}
---
请根据以上内容，判断你是否应该回复。你的回答必须是一个JSON对象，格式如下：
{{
  "should_reply": boolean,
  "reply_content": "string"
}}
- 如果你认为应该插话，请将 "should_reply" 设为 true，并在 "reply_content" 中提供你的回复内容。
- 如果你认为不需要插话，请将 "should_reply" 设为 false，"reply_content" 留空。

记住，只在对话与你相关、或者你能提供有价值的信息时才进行回复。不要轻易打断用户的正常交流。
你的回复：
"""
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("未找到可用的大语言模型提供商，无法执行主动插话分析。")
                return

            llm_response: LLMResponse = await provider.text_chat(
                prompt="", system_prompt=system_prompt
            )

            if not llm_response or not llm_response.completion_text:
                logger.error("主动插话分析时，LLM未返回有效内容。")
                return

            # 解析LLM的JSON输出
            try:
                # 尝试从文本中提取JSON
                json_str = llm_response.completion_text.strip()
                if json_str.startswith("```json"):
                    json_str = json_str[7:]
                if json_str.endswith("```"):
                    json_str = json_str[:-3]

                decision = json.loads(json_str)
                should_reply = decision.get("should_reply", False)
                reply_content = decision.get("reply_content", "")

                if should_reply and reply_content:
                    logger.info(f"[{session_id}] LLM决定主动插话，内容: {reply_content}")
                    message_chain = [Plain(reply_content)]
                    await self.context.send_message(session_id, message_chain)
                else:
                    logger.info(f"[{session_id}] LLM决定不进行主动插话。")

            except (json.JSONDecodeError, AttributeError) as e:
                logger.error(
                    f"[{session_id}] 解析LLM关于主动插话的决策时出错: {e}\n"
                    f"原始返回: {llm_response.completion_text}"
                )

        except asyncio.CancelledError:
            logger.debug(f"[{session_id}] 主动插话任务被取消。")
        finally:
            # 清理任务和历史记录
            if session_id in self.proactive_tasks:
                del self.proactive_tasks[session_id]
            if session_id in proactive_chat_histories:
                del proactive_chat_histories[session_id]

    async def terminate(self):
        """
        插件卸载/停用时，清理所有正在运行的异步任务。
        """
        for task in self.proactive_tasks.values():
            if not task.done():
                task.cancel()
        self.proactive_tasks.clear()
        sticky_reply_targets.clear()
        proactive_chat_histories.clear()
        logger.info("ReplyDirectly 插件已卸载，所有任务已清理。")
