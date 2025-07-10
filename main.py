# /data/plugins/astrbot_plugin_smart_reply/main.py

import asyncio
import json
import time
from collections import defaultdict, deque

from astrbot.api import (
    Context,
    Star,
    register,
    logger,
    AstrMessageEvent,
    MessageEventResult,
    AstrBotConfig,
)
from astrbot.api.event import filter
import astrbot.api.message_components as Comp

@register(
    "SmartReply",
    "YourName",
    "一个实现沉浸式对话和主动插话的智能回复插件",
    "1.0.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly"
)
class SmartReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 用于沉浸式对话，存储 {group_id: {user_id1, user_id2}}
        self.sticky_targets = defaultdict(set)
        
        # 用于主动插话，存储 {group_id: deque([(timestamp, user_id, user_name, text)])}
        # 使用deque可以自动管理历史记录的长度
        self.proactive_history_limit = self.config.get("proactive_reply", {}).get("history_limit", 10)
        self.proactive_history = defaultdict(lambda: deque(maxlen=self.proactive_history_limit))

        logger.info("智能回复插件已加载。")

    async def terminate(self):
        logger.info("智能回复插件已卸载。")

    # --- 功能1: 沉浸式对话 (Sticky Reply) ---

    @filter.llm_tool(name="enable_direct_reply_once")
    async def enable_direct_reply_once(self, event: AstrMessageEvent, user_id: str) -> MessageEventResult:
        """
        让机器人下一次主动回复指定用户的发言，无需@。调用此函数后，机器人会在该用户下次发言时自动响应一次。
        Args:
            user_id (string): 需要主动回复的用户的ID。
        """
        if not self.config.get("enable_plugin") or not self.config.get("sticky_reply", {}).get("enable"):
            return event.plain_result("沉浸式对话功能未开启。")

        group_id = event.get_group_id()
        if not group_id:
            return event.plain_result("该功能仅在群聊中可用。")

        self.sticky_targets[group_id].add(user_id)
        logger.info(f"[沉浸式对话] 已为群 {group_id} 的用户 {user_id} 设置下一次主动回复。")
        # 这个回复是给LLM的，通常不会直接发给用户
        return event.plain_result(f"OK, I will reply to user {user_id} next time.")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def sticky_reply_handler(self, event: AstrMessageEvent):
        """
        高优先级处理器，检查消息是否来自被标记的用户
        """
        if (
            not event.is_group_chat()
            or not self.config.get("enable_plugin")
            or not self.config.get("sticky_reply", {}).get("enable")
        ):
            return

        group_id = event.get_group_id()
        sender_id = event.get_sender_id()

        if group_id in self.sticky_targets and sender_id in self.sticky_targets[group_id]:
            logger.info(f"[沉浸式对话] 捕获到目标用户 {sender_id} 在群 {group_id} 的发言，将唤醒机器人。")
            
            # 核心：将事件标记为唤醒状态，后续流程会认为机器人被@了
            event.is_wake = True
            
            # 用完一次后立即移除
            self.sticky_targets[group_id].remove(sender_id)
            if not self.sticky_targets[group_id]:
                del self.sticky_targets[group_id]

    # --- 功能2: 主动插话 (Proactive Reply) ---

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-10)
    async def log_group_messages(self, event: AstrMessageEvent):
        """
        低优先级处理器，默默记录群聊消息历史
        """
        if not self.config.get("enable_plugin") or not self.config.get("proactive_reply", {}).get("enable"):
            return
        
        # 只记录用户消息，忽略机器人自己的消息
        if event.get_sender_id() != event.get_self_id():
            group_id = event.get_group_id()
            self.proactive_history[group_id].append(
                (
                    time.time(),
                    event.get_sender_id(),
                    event.get_sender_name(),
                    event.message_str
                )
            )

    @filter.after_message_sent()
    async def schedule_proactive_check(self, event: AstrMessageEvent):
        """
        当机器人发送消息后，安排一个检查任务
        """
        if (
            not event.is_group_chat()
            or not self.config.get("enable_plugin")
            or not self.config.get("proactive_reply", {}).get("enable")
        ):
            return
        
        # 机器人自己发的消息，其sender_id是自己的id
        if event.get_sender_id() == event.get_self_id():
            logger.info(f"[主动插话] 机器人已在群 {event.get_group_id()} 发言，将启动主动插话检查任务。")
            asyncio.create_task(self.proactive_check_task(event))

    async def proactive_check_task(self, event: AstrMessageEvent):
        try:
            delay = self.config.get("proactive_reply", {}).get("delay_seconds", 5)
            await asyncio.sleep(delay)

            bot_message_timestamp = event.message_obj.timestamp
            group_id = event.get_group_id()

            if group_id not in self.proactive_history:
                return

            # 筛选出机器人发言后的新消息
            recent_messages = [
                msg for msg in self.proactive_history[group_id] 
                if msg[0] > bot_message_timestamp
            ]

            if not recent_messages:
                logger.info(f"[主动插话] 在 {delay} 秒内，群 {group_id} 没有新消息，任务结束。")
                return

            # 格式化历史记录以供LLM分析
            history_str = "\n".join([f'{name}({uid}): {text}' for ts, uid, name, text in recent_messages])
            logger.info(f"[主动插话] 群 {group_id} 的近期聊天记录:\n{history_str}")
            
            # 构造给LLM的Prompt
            prompt = f"""
你是一个群聊观察助手。请分析以下在机器人发言后的一段聊天记录，判断机器人是否应该主动插话参与讨论。

聊天记录:
---
{history_str}
---

请根据以上内容，严格按照以下JSON格式返回你的决定，不要添加任何额外的解释或文字：
{{
  "should_reply": boolean,
  "reply_content": "如果should_reply为true，这里是你的回复内容"
}}
"""
            
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[主动插话] 未找到可用的大语言模型提供商。")
                return

            llm_response = await provider.text_chat(prompt=prompt, system_prompt="你是一个群聊观察助手，请严格遵守格式要求。")

            if llm_response and llm_response.completion_text:
                try:
                    decision = json.loads(llm_response.completion_text.strip())
                    if decision.get("should_reply") and decision.get("reply_content"):
                        logger.info(f"[主动插话] LLM决定插话，内容: {decision['reply_content']}")
                        
                        # @最后一位发言者
                        last_speaker_id = recent_messages[-1][1]
                        
                        message_chain = [
                            Comp.At(qq=last_speaker_id),
                            Comp.Plain(text=f" {decision['reply_content']}")
                        ]
                        
                        await self.context.send_message(event.unified_msg_origin, message_chain)

                except json.JSONDecodeError:
                    logger.error(f"[主动插话] LLM返回的不是有效的JSON: {llm_response.completion_text}")
                except Exception as e:
                    logger.error(f"[主动插话] 处理LLM响应时出错: {e}")

        except Exception as e:
            logger.error(f"[主动插话] 检查任务执行失败: {e}")
