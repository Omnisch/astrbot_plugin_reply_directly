import asyncio
import json
import re
from collections import defaultdict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.api.message_components import MessageChain

@register(
    "reply_directly",
    "qa296",
    "提供沉浸式对话和主动插话功能，让机器人更智能地参与群聊。",
    "1.0.0", 
    "https://github.com/qa296/astrbot_plugin_reply_directly"
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.direct_reply_groups = set()
        self.active_timers = {}
        self.group_chat_buffer = defaultdict(list)
        logger.info("ReplyDirectly插件加载成功！")

    def _extract_json_from_text(self, text: str) -> str:
        """
        从可能包含Markdown代码块的文本中稳健地提取纯JSON字符串。
        """
        match = re.search(r'```json\s*([\s\S]*?)\s*```', text, re.DOTALL)
        if match:
            return match.group(1).strip()
        match = re.search(r'```\s*([\s\S]*?)\s*```', text, re.DOTALL)
        if match:
            return match.group(1).strip()
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            return text[start:end+1]
        return text

    # -----------------------------------------------------
    # Feature 1: 沉浸式对话 (Immersive Chat)
    # -----------------------------------------------------

    @filter.llm_tool()
    async def enable_direct_reply_once(self, event: AstrMessageEvent):
        """
        当LLM认为可以开启沉浸式对话时调用此函数。这会让机器人在该群组的下一条消息时直接回复，无需@。此效果仅生效一次。
        """
        if not self.config.get('enable_immersive_chat', True):
            return

        group_id = event.get_group_id()
        if group_id:
            logger.info(f"[沉浸式对话] 已为群 {group_id} 开启单次直接回复模式。")
            self.direct_reply_groups.add(group_id)

    # -----------------------------------------------------
    # Feature 2: 主动插话 (Proactive Interjection)
    # -----------------------------------------------------

    @filter.after_message_sent()
    async def after_bot_message_sent(self, event: AstrMessageEvent):
        """机器人发送消息后，启动主动插话的计时器"""
        if not self.config.get('enable_plugin', True) or not self.config.get('enable_proactive_reply', True):
            return
        if event.is_private_chat():
            return
        group_id = event.get_group_id()
        if not group_id:
            return

        if group_id in self.active_timers:
            self.active_timers[group_id].cancel()
            logger.debug(f"[主动插话] 取消了群 {group_id} 的旧计时器。")

        self.group_chat_buffer[group_id].clear()
        task = asyncio.create_task(self._proactive_check_task(group_id, event.unified_msg_origin))
        self.active_timers[group_id] = task
        logger.info(f"[主动插话] 机器人发言，已为群 {group_id} 启动主动插话计时器。")

    async def _proactive_check_task(self, group_id: str, unified_msg_origin: str):
        """计时器到点后执行的检查任务"""
        try:
            delay = self.config.get('proactive_reply_delay', 8)
            await asyncio.sleep(delay)

            self.active_timers.pop(group_id, None)
            chat_history = self.group_chat_buffer.pop(group_id, [])
            if not chat_history:
                logger.info(f"[主动插话] 群 {group_id} 在 {delay}s 内无新消息，不进行判断。")
                return

            logger.info(f"[主动插话] 群 {group_id} 计时结束，收集到 {len(chat_history)} 条消息，开始请求LLM判断。")
            
            formatted_history = "\n".join(chat_history)
            prompt = (
                f"我在一个群聊里，在我说完话后，群里发生了以下的对话：\n"
                f"--- 对话记录 ---\n{formatted_history}\n--- 对话记录结束 ---\n"
                f"请你判断我是否应该根据以上对话内容进行插话回复。请严格按照以下JSON格式回答，不要添加任何其他说明：\n"
                f'{{"should_reply": 布尔值, "content": "如果should_reply为true，这里是你的回复内容，否则为空字符串"}}'
            )

            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[主动插话] 未找到可用的大语言模型提供商。")
                return
                
            llm_response = await provider.text_chat(prompt=prompt)
            
            json_string = ""
            try:
                json_string = self._extract_json_from_text(llm_response.completion_text)
                if not json_string:
                    logger.warning(f"[主动插话] 从LLM回复中未能提取出有效内容。原始回复: {llm_response.completion_text}")
                    return
                decision_data = json.loads(json_string)
            except json.JSONDecodeError as e:
                logger.error(
                    f"[主动插话] 解析LLM的JSON回复失败: {e}\n"
                    f"原始回复: {llm_response.completion_text}\n"
                    f"清理后尝试解析的文本: '{json_string}'"
                )
                return # 解析失败，直接返回

            # --- 从这里开始，JSON解析已经成功，处理后续逻辑 ---
            should_reply = decision_data.get("should_reply", False)
            content = decision_data.get("content", "")

            if should_reply and content:
                logger.info(f"[主动插话] LLM判断需要回复，内容: {content[:50]}...")
                # 这行代码现在可以正常工作了，因为它依赖于正确的 MessageChain 导入
                message_to_send = MessageChain().message(content)
                await self.context.send_message(unified_msg_origin, message_to_send)
            else:
                logger.info("[主动插话] LLM判断无需回复。")

        except asyncio.CancelledError:
            logger.info(f"[主动插话] 群 {group_id} 的任务被取消。")
        except Exception as e:
            # 这里的日志现在能更准确地捕捉到非JSON解析的错误
            logger.error(f"[主动插话] 任务执行出现未知异常: {e}", exc_info=True)
        finally:
            self.active_timers.pop(group_id, None)
            self.group_chat_buffer.pop(group_id, None)

    # -----------------------------------------------------
    # 统一的消息监听器
    # -----------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """统一处理所有群聊消息"""
        if not self.config.get('enable_plugin', True):
            return

        group_id = event.get_group_id()
        if event.get_sender_id() == event.get_self_id():
            return

        if self.config.get('enable_immersive_chat', True) and group_id in self.direct_reply_groups:
            logger.info(f"[沉浸式对话] 检测到群 {group_id} 的直接回复消息，触发LLM。")
            self.direct_reply_groups.remove(group_id)
            event.stop_event()
            yield event.request_llm(prompt=event.message_str)
            return

        if self.config.get('enable_proactive_reply', True) and group_id in self.active_timers:
            sender_name = event.get_sender_name()
            message_text = event.message_str
            if message_text:
                self.group_chat_buffer[group_id].append(f"{sender_name}: {message_text}")

    # -----------------------------------------------------
    # 插件卸载时的清理工作
    # -----------------------------------------------------
    async def terminate(self):
        """插件被卸载/停用时调用，用于清理"""
        logger.info("正在卸载ReplyDirectly插件，取消所有计时器...")
        for task in self.active_timers.values():
            task.cancel()
        self.active_timers.clear()
        self.group_chat_buffer.clear()
        self.direct_reply_groups.clear()
        logger.info("ReplyDirectly插件所有后台任务已清理。")
