# astrbot_plugin_reply_directly/main.py
import asyncio
import json
import time
from collections import defaultdict
from typing import Dict, List, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import (MessageEventResult, AstrMessageEvent, filter)
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

# 用于存储功能一的目标用户
# 结构: { "会话ID": "目标用户ID" }
# 例如: { "aiocqhttp:group:123456": "987654" }
direct_reply_targets: Dict[str, str] = {}

# 用于存储功能二的聊天记录
# 结构: { "会话ID": [(timestamp, user_name, content), ...] }
chat_history: Dict[str, List[Tuple[int, str, str]]] = defaultdict(list)

# 用于防止并发任务
analysis_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@register("DirectReply", "AI", "智能直接回复和主动聊天分析", "1.0.0")
class DirectReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.lock = asyncio.Lock()  # 保护 direct_reply_targets 的并发访问
        logger.info("DirectReply 插件已加载。")

    # --- 功能一: 智能直接回复 ---

    @filter.llm_tool(name="activate_direct_reply")
    async def activate_direct_reply(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        当你认为和用户的对话非常流畅，且你期望在用户下一次发言时能直接回复他（即使他没有@你）时，可以调用此函数。
        这会建立一个临时的直接回复关系，仅生效一次。
        """
        if not self.config.get("enable_smart_direct_reply", False):
            return

        sender_id = event.get_sender_id()
        session_id = event.unified_msg_origin

        async with self.lock:
            direct_reply_targets[session_id] = sender_id
        
        logger.info(f"[功能一] 已为会话 {session_id} 的用户 {sender_id} 设置下一次直接回复。")
        # 这个工具本身不产生对用户的可见回复，只是一个状态设置
        # 但为了符合函数调用规范，我们返回一个空结果，让LLM继续生成文本
        return event.make_result()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def handle_direct_reply(self, event: AstrMessageEvent):
        """
        监听所有消息，检查是否命中直接回复目标
        """
        if not self.config.get("enable_smart_direct_reply", False):
            return
        
        session_id = event.unified_msg_origin
        sender_id = event.get_sender_id()

        async with self.lock:
            target_user = direct_reply_targets.get(session_id)

        # 如果当前消息的发送者是目标用户，并且不是命令或@消息
        if target_user == sender_id and not event.is_at_or_wake_command:
            logger.info(f"[功能一] 命中直接回复目标！用户: {sender_id}，内容: {event.message_str}")
            
            # 清除目标，确保只生效一次
            async with self.lock:
                if session_id in direct_reply_targets:
                    del direct_reply_targets[session_id]

            # 阻止事件继续传播，防止默认的LLM回复（如果它也被唤醒的话）
            event.stop_event()
            
            # 将用户的消息发给LLM处理并回复
            yield event.request_llm(prompt=event.message_str)

    # --- 功能二: 主动聊天分析 ---

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def record_chat_history(self, event: AstrMessageEvent):
        """
        记录所有消息，为主动分析做准备
        priority设得很高（数字越大优先级越低），确保它在最后执行
        """
        if not self.config.get("enable_proactive_analysis", False):
            return

        session_id = event.unified_msg_origin
        current_time = int(time.time())
        
        # 记录消息
        chat_history[session_id].append(
            (current_time, event.get_sender_name(), event.message_str)
        )

        # 清理旧消息，只保留设定窗口内的记录
        window_size = self.config.get("proactive_analysis_window", 10) + 5 # 加一点buffer
        chat_history[session_id] = [
            msg for msg in chat_history[session_id] if current_time - msg[0] <= window_size
        ]

    @filter.after_message_sent()
    async def trigger_proactive_analysis(self, event: AstrMessageEvent):
        """
        当机器人发送消息后，触发一个延时任务去分析后续聊天
        """
        if not self.config.get("enable_proactive_analysis", False):
            return
            
        delay = self.config.get("proactive_analysis_delay", 5)
        session_id = event.unified_msg_origin

        logger.info(f"[功能二] Bot在 {session_id} 发言，将在 {delay} 秒后触发主动分析。")
        
        # 使用create_task创建后台任务，不会阻塞当前流程
        asyncio.create_task(
            self.proactive_analysis_task(session_id, delay)
        )

    async def proactive_analysis_task(self, session_id: str, delay: int):
        # 等待指定时间
        await asyncio.sleep(delay)
        
        # 尝试获取锁，如果已有任务在分析此会话，则直接返回
        lock = analysis_locks[session_id]
        if lock.locked():
            logger.info(f"[功能二] 会话 {session_id} 已有分析任务在运行，本次跳过。")
            return

        async with lock:
            logger.info(f"[功能二] 开始分析会话 {session_id} 的聊天记录。")
            
            # 获取最近的聊天记录
            window_size = self.config.get("proactive_analysis_window", 10)
            current_time = int(time.time())
            
            relevant_history = [
                f"{name}: {content}" 
                for ts, name, content in chat_history[session_id] 
                if current_time - ts <= window_size
            ]
            
            if not relevant_history:
                logger.info(f"[功能二] 会话 {session_id} 在时间窗口内无聊天记录，分析结束。")
                return

            history_str = "\n".join(relevant_history)
            
            # 构建prompt
            prompt = f"""You are a chat analysis assistant. Your task is to analyze a short chat history that occurs right after I (the bot) have spoken. Decide if I should make a follow-up comment to keep the conversation going or clarify something.

Respond ONLY in valid JSON format with two keys:
1. "should_reply": A boolean (true or false). Set to true only if a follow-up is truly necessary, helpful, or natural. Do not reply to simple acknowledgements like "好的" or "收到".
2. "reply_content": A string containing the exact message I should send. If "should_reply" is false, this can be an empty string.

Here is the recent chat history (most recent at the bottom):
---
{history_str}
---
Analyze the above and provide your JSON response."""

            try:
                # 使用底层LLM接口，因为我们需要解析返回的JSON
                provider = self.context.get_using_provider()
                if not provider:
                    logger.warning("[功能二] 未找到可用的大语言模型提供商。")
                    return

                response: LLMResponse = await provider.text_chat(prompt=prompt)
                
                # 清理和解析JSON
                content = response.completion_text.strip()
                if content.startswith("```json"):
                    content = content[7:]
                if content.endswith("```"):
                    content = content[:-3]
                
                analysis_result = json.loads(content)

                if analysis_result.get("should_reply") and analysis_result.get("reply_content"):
                    reply_text = analysis_result["reply_content"]
                    logger.info(f"[功能二] LLM决定主动回复: {reply_text}")
                    # 主动发送消息
                    from astrbot.api.message_components import Plain
                    await self.context.send_message(session_id, [Plain(reply_text)])
                else:
                    logger.info(f"[功能二] LLM决定不进行主动回复。")

            except json.JSONDecodeError as e:
                logger.error(f"[功能二] LLM返回的不是有效的JSON: {response.completion_text}, 错误: {e}")
            except Exception as e:
                logger.error(f"[功能二] 主动分析任务出现未知错误: {e}")


    async def terminate(self):
        """插件卸载/停用时调用"""
        logger.info("DirectReply 插件已卸载。")
        # 清理状态
        global direct_reply_targets, chat_history, analysis_locks
        direct_reply_targets.clear()
        chat_history.clear()
        analysis_locks.clear()
