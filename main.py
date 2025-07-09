import asyncio
import json
from typing import Set

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import (MessageEventResult, AstrMessageEvent, filter)
from astrbot.api.message_components import MessageChain
from astrbot.api.star import Context, Star, register


@register(
    "reply_directly", # 插件内部名称
    "qa296", # 作者
    "一个智能回复插件，可以让LLM决定在特定情境下主动回复用户，或在自己发言后进行反思追问。", # 描述
    "1.0.0", # 版本
    "https://github.com/qa296/astrbot_plugin_reply_directly" # 仓库地址
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 用于存储被LLM标记，需要主动回复的用户 session id
        self.direct_reply_targets: Set[str] = set()

    # ======================================================
    # 功能一：主动回复 (代码无问题，保持原样)
    # ======================================================

    @filter.llm_tool(name="start_direct_reply")
    async def start_direct_reply(self, event: AstrMessageEvent) -> MessageEventResult:
        '''当与用户深入聊天，且你认为下一次TA发言时无论是否@你，你都应该回复时，调用此工具。调用后，机器人将在该用户的下一条消息时主动进行回复。此效果仅生效一次。'''
        if not self.config.get("enable_direct_reply"):
            logger.warning("“主动回复”功能已禁用，但LLM尝试调用。")
            return event.plain_result("（指令失败：主动回复功能当前已禁用。）")

        user_id = event.unified_msg_origin
        self.direct_reply_targets.add(user_id)
        logger.info(f"已为用户 {user_id} 设置下一次主动回复。")
        return event.plain_result(f"（操作成功：已设定，我将在{event.get_sender_name()}下次发言时主动回复。）")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def proactive_reply_handler(self, event: AstrMessageEvent):
        """
        高优先级事件监听器，用于处理被标记用户的消息。
        """
        if not self.config.get("enable_direct_reply"):
            return

        user_id = event.unified_msg_origin
        if user_id in self.direct_reply_targets:
            logger.info(f"检测到目标用户 {user_id} 的消息，将进行主动回复。")
            self.direct_reply_targets.remove(user_id)
            yield event.request_llm(
                prompt=event.message_str,
                image_urls=event.get_image_urls()
            )
            event.stop_event()

    # ======================================================
    # 功能二：反思追问 (代码有优化)
    # ======================================================

    @filter.after_message_sent()
    async def follow_up_handler(self, event: AstrMessageEvent):
        """
        在机器人发送消息后触发的钩子。(代码无问题，保持原样)
        """
        if not self.config.get("enable_follow_up"):
            return

        result = event.get_result()
        if not result or not result.source.startswith("llm"):
            return
            
        bot_message_chain = result.chain
        bot_message_text = "".join(
            c.text for c in bot_message_chain if hasattr(c, "text") and c.text
        )

        if not bot_message_text:
            return

        delay = self.config.get("follow_up_delay", 5)
        logger.info(f"Bot已发言，将在 {delay} 秒后进行反思追问。")
        asyncio.create_task(self._perform_follow_up(event, bot_message_text))


    async def _perform_follow_up(self, event: AstrMessageEvent, bot_message_text: str):
        """
        实际执行反思追问的异步函数。(此部分已优化)
        """
        delay = self.config.get("follow_up_delay", 5)
        await asyncio.sleep(delay)

        prompt = f"""
[背景]
你是一个名为AstrBot的AI助手。你刚刚对用户说了以下内容：
"{bot_message_text}"

[任务]
请反思你刚才的回复。判断是否需要进行补充说明或追问，以引导对话、澄清观点或提供更多价值。请以JSON格式输出你的决定。JSON结构必须如下：
{{
  "should_reply": boolean,
  "reply_content": "如果should_reply为true，这里是你的补充回答内容"
}}

[要求]
- 如果你认为无需补充，将 "should_reply" 设为 false。
- 如果需要补充，将 "should_reply" 设为 true，并在 "reply_content" 中提供具体内容。
- 你的补充内容应该是简洁、有价值的，而不是简单的重复或客套。
- 直接输出JSON，不要包含任何其他解释文字或代码块标记。
"""
        try:
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("反思追问失败：未找到正在使用的LLM提供商。")
                return

            response = await provider.text_chat(prompt=prompt)
            
            # --- START: 优化的JSON解析逻辑 ---
            raw_text = response.completion_text.strip()
            
            # 尝试从Markdown代码块中提取JSON
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
                if raw_text.endswith("```"):
                    raw_text = raw_text[:-3]
                raw_text = raw_text.strip()
            # 找到第一个 "{" 和最后一个 "}" 来提取可能的JSON对象
            elif '{' in raw_text and '}' in raw_text:
                 start_index = raw_text.find('{')
                 end_index = raw_text.rfind('}') + 1
                 raw_text = raw_text[start_index:end_index]
            
            decision_json = json.loads(raw_text)
            # --- END: 优化的JSON解析逻辑 ---
            
            if decision_json.get("should_reply"):
                reply_content = decision_json.get("reply_content")
                if reply_content and isinstance(reply_content, str):
                    logger.info(f"反思追问结果：需要回复。内容：{reply_content}")
                    message_chain = MessageChain().message(reply_content)
                    await self.context.send_message(event.unified_msg_origin, message_chain)
                else:
                    logger.warning("反思追问决定回复，但内容为空或格式不正确。")
            else:
                logger.info("反思追问结果：无需回复。")

        except json.JSONDecodeError:
            logger.error(f"反思追问失败：LLM返回的不是有效的JSON。原始返回: {response.completion_text}")
        except Exception as e:
            logger.error(f"反思追问任务发生未知错误: {e}", exc_info=True)
