import asyncio
import json
import time
from collections import deque

from astrbot.api import logger, AstrBotConfig, MessageType
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

# 用于主动插话的系统提示词
PROACTIVE_REPLY_PROMPT = """You are an observer in a group chat. Your goal is to analyze the recent conversation that happened right after your last message and decide if you should interject.

The following is the recent chat history, formatted as "Speaker: Message".
---
{chat_history}
---
Based on this history, should you reply? Your response MUST be a valid JSON object with two keys:
1. "should_reply": A boolean value (true or false).
2. "reply_content": A string containing what you would say. If "should_reply" is false, this should be an empty string.

Example of a valid response:
{"should_reply": true, "reply_content": "看你们聊得这么开心，我也来插一句！"}

Example of another valid response:
{"should_reply": false, "reply_content": ""}

Now, analyze the provided chat history and give your JSON response. Do not include any other text, markdown formatting, or explanations.
"""


@register("reply_directly", "qa296", "沉浸式对话与主动插话插件", "1.0.0", "https://github.com/qa296/astrbot_plugin_reply_directly")
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 1. 沉浸式对话功能
        # 用于存储被LLM指定可以进行一次免@回复的用户ID
        self.sticky_reply_users = set()

        # 2. 主动插话功能
        # 用于存储群聊消息记录，key为group_id，value为deque
        self.group_chat_history = {}
        # 用于防止多线程/协程访问历史记录时出现问题
        self.history_lock = asyncio.Lock()
        # 记录正在进行主动检查的群组，防止重复触发
        self.proactive_check_locks = set()

    # 功能1：LLM函数工具，用于开启沉浸式对话
    @filter.llm_tool(name="enable_direct_reply_once")
    async def enable_direct_reply_once(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        当你认为和一个用户的对话非常连贯，希望在下一次回复他时，即使用户没有@你，你也能主动回复时，可以调用此工具。
        调用后，该用户下一次发言将直接触发你的回复，此效果仅生效一次。
        """
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable", False):
            return event.make_result() # 插件关闭时，函数静默失败

        sender_id = event.get_sender_id()
        if sender_id:
            logger.info(f"[ReplyDirectly] 用户 {sender_id} 已被标记为下次直接回复。")
            self.sticky_reply_users.add(sender_id)
        
        # 返回一个空结果，这样机器人不会在调用函数后发送任何消息
        return event.make_result()

    # 功能1：监听所有消息，处理沉浸式对话
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def handle_sticky_reply(self, event: AstrMessageEvent):
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable", False):
            return

        sender_id = event.get_sender_id()
        # 如果消息发送者在我们的“沉浸式”集合中，并且不是在请求LLM（避免双重回复）
        if sender_id in self.sticky_reply_users and not event.is_at_or_wake_command:
            logger.info(f"[ReplyDirectly] 检测到被标记用户 {sender_id} 的消息，将直接调用LLM。")
            # 从集合中移除，确保只生效一次
            self.sticky_reply_users.remove(sender_id)

            # 直接请求LLM进行回复
            yield event.request_llm(prompt=event.get_message_str())
            
            # 停止事件继续传播，防止其他插件或默认的LLM逻辑再次处理
            event.stop_event()

    # 功能2：监听所有群聊消息，用于记录历史
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def log_group_message(self, event: AstrMessageEvent):
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable", False):
            return

        group_id = event.get_group_id()
        sender_name = event.get_sender_name() or event.get_sender_id()
        message_str = event.get_message_str()

        if not group_id or not message_str:
            return

        async with self.history_lock:
            if group_id not in self.group_chat_history:
                # 使用deque可以高效地在两端添加/删除元素，并可设置最大长度
                self.group_chat_history[group_id] = deque(maxlen=self.config["proactive_reply"]["history_limit"] * 2) # 留一些冗余
            
            record = {
                "timestamp": time.time(),
                "sender": sender_name,
                "message": message_str
            }
            self.group_chat_history[group_id].append(record)
            logger.debug(f"[ReplyDirectly] 记录群 {group_id} 消息: {sender_name}: {message_str}")

    # 功能2：在机器人发送消息后触发，准备进行主动插话检查
    @filter.after_message_sent()
    async def after_bot_reply(self, event: AstrMessageEvent):
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable", False):
            return
        
        # 只在群聊中生效
        if event.message_obj.type != MessageType.GROUP_MESSAGE:
            return
            
        group_id = event.get_group_id()
        # 如果该群组已有一个检查任务在运行，则不重复创建
        if group_id in self.proactive_check_locks:
            logger.debug(f"[ReplyDirectly] 群 {group_id} 已有主动检查任务，本次跳过。")
            return

        # 确认机器人确实发送了消息
        result = event.get_result()
        if result and result.chain:
            logger.info(f"[ReplyDirectly] 机器人在群 {group_id} 发言，将在 {self.config['proactive_reply']['delay_seconds']} 秒后检查是否需要插话。")
            asyncio.create_task(self._proactive_check_task(group_id, event.unified_msg_origin))


    async def _proactive_check_task(self, group_id: str, unified_msg_origin: str):
        # 添加锁，防止并发
        self.proactive_check_locks.add(group_id)
        
        try:
            delay = self.config["proactive_reply"]["delay_seconds"]
            await asyncio.sleep(delay)

            start_time = time.time() - delay
            history_to_check = []

            async with self.history_lock:
                if group_id in self.group_chat_history:
                    # 筛选出在延迟时间内的新消息
                    for record in self.group_chat_history[group_id]:
                        if record["timestamp"] >= start_time:
                            history_to_check.append(f"{record['sender']}: {record['message']}")
            
            # 限制分析的消息数量
            history_limit = self.config["proactive_reply"]["history_limit"]
            history_to_check = history_to_check[-history_limit:]
            
            if not history_to_check:
                logger.info(f"[ReplyDirectly] 群 {group_id} 在 {delay} 秒内无新消息，取消插话检查。")
                return

            chat_history_str = "\n".join(history_to_check)
            logger.info(f"[ReplyDirectly] 群 {group_id} 准备分析以下聊天记录：\n{chat_history_str}")

            # 调用LLM进行分析
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[ReplyDirectly] 未找到可用的大语言模型提供商，无法执行主动插话。")
                return
            
            llm_response: LLMResponse = await provider.text_chat(
                prompt=chat_history_str,
                system_prompt=PROACTIVE_REPLY_PROMPT.format(chat_history=chat_history_str)
            )

            if not llm_response or not llm_response.completion_text:
                logger.error("[ReplyDirectly] LLM调用失败或未返回任何内容。")
                return

            try:
                # 解析LLM返回的JSON
                decision = json.loads(llm_response.completion_text.strip())
                should_reply = decision.get("should_reply", False)
                reply_content = decision.get("reply_content", "")

                if should_reply and reply_content:
                    logger.info(f"[ReplyDirectly] LLM决定主动插话，内容：{reply_content}")
                    # 使用 context.send_message 主动发送消息
                    await self.context.send_message(unified_msg_origin, [Plain(text=reply_content)])
                else:
                    logger.info("[ReplyDirectly] LLM决定不插话。")

            except json.JSONDecodeError:
                logger.error(f"[ReplyDirectly] LLM返回的不是有效的JSON格式: {llm_response.completion_text}")
            except Exception as e:
                logger.error(f"[ReplyDirectly] 处理LLM响应时发生未知错误: {e}")

        finally:
            # 任务结束，解除锁定
            self.proactive_check_locks.remove(group_id)

    async def terminate(self):
        """插件卸载或停用时调用的清理函数"""
        logger.info("[ReplyDirectly] 插件正在终止，清空所有状态。")
        self.sticky_reply_users.clear()
        self.group_chat_history.clear()
        self.proactive_check_locks.clear()
