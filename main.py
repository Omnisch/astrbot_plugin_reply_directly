import asyncio
import json
import time
from collections import defaultdict, deque
from typing import Dict

from astrbot.api import logger
from astrbot.api.config import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp


# 使用 register 装饰器注册插件
# 名称、作者、描述、版本号、仓库地址
@register(
    "reply_directly",
    "qa296",
    "实现沉浸式对话和主动插话功能",
    "1.0.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly",
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        """
        插件初始化
        """
        super().__init__(context)
        self.config = config
        
        # 沉浸式回复的目标。 key: unified_msg_origin, value: target_user_id
        self.sticky_reply_targets: Dict[str, str] = {}
        
        # 主动插话的聊天记录收集器。
        # key: unified_msg_origin, value: deque of chat messages
        # 使用 defaultdict 和 deque 可以方便地管理有限长度的聊天记录
        proactive_conf = self.config.get("proactive_reply", {})
        history_limit = proactive_conf.get("history_limit", 10)
        self.chat_history_collector = defaultdict(lambda: deque(maxlen=history_limit))

    # --- 1. 沉浸式对话 (Sticky Reply) ---

    @filter.llm_tool(name="enable_direct_reply_once")
    async def enable_direct_reply_once(self, event: AstrMessageEvent, user_id: str) -> MessageEventResult:
        """
        让机器人下次直接回复指定用户ID的消息，无需@。此效果仅生效一次。

        Args:
            user_id(string): 需要直接回复的用户的ID。
        """
        # 检查总开关和功能开关
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return

        umo = event.unified_msg_origin
        self.sticky_reply_targets[umo] = user_id
        logger.info(f"[ReplyDirectly] 已设置在会话 {umo} 中下次直接回复用户 {user_id}。")
        # 这个函数工具是给LLM用的，通常不需要对用户有明确的回复，后台记录即可
        # 如果需要，可以取消下面的注释
        # yield event.plain_result(f"[调试] 已设置下次对用户 {user_id} 的消息直接回复。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def handle_sticky_reply(self, event: AstrMessageEvent):
        """
        监听所有消息，检查是否触发了沉浸式回复
        使用高优先级(priority=1)确保在默认LLM处理前执行
        """
        # 检查总开关和功能开关
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return

        # 如果机器人已经被唤醒（例如被@），则不处理，走正常流程
        if event.is_wake_up():
            return

        umo = event.unified_msg_origin
        target_user_id = self.sticky_reply_targets.get(umo)
        
        # 检查当前消息的发送者是否是目标用户
        if target_user_id and target_user_id == event.get_sender_id():
            logger.info(f"[ReplyDirectly] 在会话 {umo} 中检测到目标用户 {target_user_id} 发言，触发沉浸式回复。")
            
            # 唤醒机器人，让后续的LLM流程处理这条消息
            event.is_wake = True
            
            # 效果仅生效一次，使用后立即移除目标
            del self.sticky_reply_targets[umo]

    # --- 2. 主动插话 (Proactive Reply) ---
    
    @filter.event_message_type(filter.EventMessageType.ALL, priority=-1)
    async def _collect_chat_history(self, event: AstrMessageEvent):
        """
        用低优先级收集所有聊天记录，用于主动插话分析
        """
        # 仅在功能开启时收集
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable"):
            return

        # 只收集群聊消息
        if event.is_private_chat():
            return
        
        umo = event.unified_msg_origin
        message_info = {
            "timestamp": time.time(),
            "sender_name": event.get_sender_name(),
            "sender_id": event.get_sender_id(),
            "content": event.message_str,
        }
        self.chat_history_collector[umo].append(message_info)

    @filter.after_message_sent()
    async def on_bot_message_sent(self, event: AstrMessageEvent):
        """
        当机器人发送消息后，触发此钩子，准备进行主动插话判断
        """
        # 检查总开关和功能开关
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable"):
            return
        
        # 主动插话功能仅在群聊中生效
        if event.is_private_chat() or not event.get_group_id():
            return

        # 创建一个异步任务去执行检查，避免阻塞
        asyncio.create_task(self._check_and_reply_proactively(event))

    async def _check_and_reply_proactively(self, event: AstrMessageEvent):
        """
        核心逻辑：等待一段时间，收集聊天记录，询问LLM是否插话
        """
        try:
            proactive_conf = self.config.get("proactive_reply", {})
            delay = proactive_conf.get("delay_seconds", 5)
            umo = event.unified_msg_origin
            bot_message_time = time.time()
            
            logger.info(f"[ReplyDirectly] 机器人已发言，将在 {delay} 秒后检查会话 {umo} 是否需要主动插话。")
            await asyncio.sleep(delay)

            history_deque = self.chat_history_collector.get(umo)
            if not history_deque:
                return

            # 筛选出机器人发言后的新消息
            recent_messages = [
                msg for msg in history_deque if msg["timestamp"] > bot_message_time
            ]

            if not recent_messages:
                logger.info(f"[ReplyDirectly] {delay} 秒内无新消息，取消主动插话检查。")
                return
            
            # 格式化历史记录以供LLM分析
            formatted_history = "\n".join(
                [f"{msg['sender_name']}({msg['sender_id']}): {msg['content']}" for msg in recent_messages]
            )

            # 构建给LLM的Prompt
            prompt_for_llm = f"""You are a chat assistant in a group chat. Your role is to observe the conversation and decide if it's appropriate for you to interject.
A snippet of conversation that occurred right after you sent a message is provided below.
Based ONLY on this snippet, decide if you should make a proactive reply.
Your response MUST be a single, raw JSON object with two keys:
1. "should_reply": a boolean (true if you should reply, false otherwise).
2. "reply_content": a string containing your reply, or an empty string if "should_reply" is false.
Do not add any explanations or markdown formatting around the JSON.

--- CONVERSATION SNIPPET ---
{formatted_history}
------------------------------
"""
            logger.info(f"[ReplyDirectly] 准备请求LLM判断是否插话，分析内容：\n{formatted_history}")

            # 使用底层API调用LLM，避免触发其他钩子
            llm_response: LLMResponse = await self.context.get_using_provider().text_chat(
                prompt=prompt_for_llm,
                contexts=[], # 不使用历史上下文，仅依赖当前prompt
            )

            if llm_response.role == "assistant" and llm_response.completion_text:
                try:
                    # 解析LLM返回的JSON
                    decision_json = json.loads(llm_response.completion_text)
                    should_reply = decision_json.get("should_reply", False)
                    reply_content = decision_json.get("reply_content", "")

                    if should_reply and reply_content:
                        logger.info(f"[ReplyDirectly] LLM决定主动插话，内容：{reply_content}")
                        # 使用 context.send_message 主动发送消息
                        message_chain = [Comp.Plain(text=reply_content)]
                        await self.context.send_message(umo, message_chain)
                    else:
                        logger.info("[ReplyDirectly] LLM决定不进行主动插话。")

                except (json.JSONDecodeError, TypeError) as e:
                    logger.error(f"[ReplyDirectly] 解析LLM的JSON响应失败: {e}. 响应内容: {llm_response.completion_text}")

        except Exception as e:
            logger.error(f"[ReplyDirectly] 主动插话检查过程中发生错误: {e}", exc_info=True)


    async def terminate(self):
        """
        插件停用或重载时调用，用于清理资源
        """
        self.sticky_reply_targets.clear()
        self.chat_history_collector.clear()
        logger.info("[ReplyDirectly] 插件已停用，相关数据已清理。")
