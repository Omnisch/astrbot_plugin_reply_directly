# astrbot_plugin_reply_directly/main.py

import asyncio
import json
from typing import Set

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register

# ----------------------------
# 插件元数据和注册
# ----------------------------
@register(
    "reply_directly",
    "qa296",
    "一个能让LLM决定主动回复，并在Bot回复后思考是否追问的插件。",
    "1.0.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly",
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 用于存储被LLM指定需要主动回复的用户会话ID
        # 会话ID (unified_msg_origin) 是 AstrBot 用来唯一标识一个聊天窗口的字符串
        self.direct_reply_targets: Set[str] = set()
        logger.info("智能追问插件加载成功。")

    # ----------------------------
    # 功能 1: LLM 函数工具 - 激活主动回复
    # ----------------------------
    @filter.llm_tool(name="activate_direct_reply")
    async def activate_direct_reply(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        当您认为与用户的对话非常投机，希望在下一次无需用户@就能主动回复时，调用此函数。
        此函数会将当前用户标记为“直接回复”目标，机器人将在收到该用户的下一条消息时立即响应。
        此效果仅生效一次。

        Args:
            无
        """
        user_session_id = event.unified_msg_origin
        if user_session_id not in self.direct_reply_targets:
            self.direct_reply_targets.add(user_session_id)
            logger.info(f"已激活对 {user_session_id} 的直接回复。")
        
        # 返回一条消息给用户，告知他们LLM的决定
        return event.plain_result("好的，我们接着聊。")

    # ----------------------------
    # 功能 1 的实现: 高优先级事件监听器
    # ----------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def handle_direct_reply(self, event: AstrMessageEvent):
        """
        这个高优先级的处理器会先于默认的LLM处理器执行。
        它检查当前消息的发送者是否在我们的“直接回复”目标列表中。
        """
        user_session_id = event.unified_msg_origin
        if user_session_id in self.direct_reply_targets:
            logger.info(f"检测到来自 {user_session_id} 的直接回复消息，将交由LLM处理。")
            
            # 从目标集合中移除，确保只生效一次
            self.direct_reply_targets.remove(user_session_id)
            
            # 直接将用户的消息请求LLM进行处理
            yield event.request_llm(prompt=event.message_str)
            
            # 停止事件继续传播，防止被其他处理器（如默认的LLM处理器）再次处理
            event.stop_event()

    # ----------------------------
    # 功能 2: 智能追问/补充 - 触发器
    # ----------------------------
    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """
        在机器人发送任何消息之后，此钩子会被触发。
        我们在这里创建一个异步任务，在延迟指定时间后检查是否需要追问。
        """
        # 我们只关心由LLM生成的、对用户消息的回复
        # 如果事件结果是LLM请求，说明是机器人回复用户，可以触发追问逻辑
        if event.has_result() and event.get_result().is_llm_request:
            asyncio.create_task(self.follow_up_task(event))

    # ----------------------------
    # 功能 2 的实现: 追问任务
    # ----------------------------
    async def follow_up_task(self, event: AstrMessageEvent):
        """
        这是一个独立的异步任务，用于执行追问逻辑。
        """
        delay = self.config.get("follow_up_delay", 5)
        logger.info(f"机器人已回复，将在 {delay} 秒后检查是否需要追问...")
        await asyncio.sleep(delay)

        # 获取刚刚机器人发送的消息内容
        bot_last_message_obj = event.get_result()
        if not bot_last_message_obj or not bot_last_message_obj.chain:
            logger.info("追问检查：机器人上一条消息为空，已跳过。")
            return
            
        # 将消息链转换为纯文本
        bot_last_message_text = " ".join([comp.text for comp in bot_last_message_obj.chain if hasattr(comp, 'text')])
        if not bot_last_message_text:
            logger.info("追问检查：机器人上一条消息不含文本，已跳过。")
            return

        # 获取当前对话的上下文
        # 您提到的文件 `astrobot/core/conversation_mgr.py` 中定义了对话管理相关逻辑
        curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        context_history = []
        if curr_cid:
            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
            if conversation and conversation.history:
                context_history = json.loads(conversation.history)
        
        # 准备追问的提示词
        # 我们将历史记录和机器人刚说的话组合起来，让LLM判断
        prompt_for_follow_up = f"这是机器人刚才说的话：\n---\n{bot_last_message_text}\n---\n结合以上信息和完整的对话历史，请判断机器人是否需要立即发送一条补充或追问的消息来使对话更流畅或更有深度。如果不需要，或者用户已经回复，请回答 'no'。如果需要，请仅用以下JSON格式回答，不要包含其他任何解释：\n"
        prompt_for_follow_up += '{"should_reply": true, "content": "你想补充或追问的内容"}'

        try:
            # 使用最底层的 text_chat 方法，避免触发其他钩子或函数调用
            llm_provider = self.context.get_using_provider()
            if not llm_provider:
                logger.warning("追问检查：未找到可用的大语言模型提供商。")
                return

            llm_response = await llm_provider.text_chat(
                prompt=prompt_for_follow_up,
                contexts=context_history, # 传入历史记录
                system_prompt="你是一个对话分析助手，你的任务是判断是否需要追问，并严格按照指定JSON格式输出。"
            )

            # 解析LLM的JSON响应
            response_text = llm_response.completion_text
            if response_text.lower().strip() == 'no':
                 logger.info("追问检查：LLM认为无需追问。")
                 return

            # 尝试解析JSON
            try:
                decision = json.loads(response_text)
                if decision.get("should_reply") and decision.get("content"):
                    follow_up_content = decision["content"]
                    logger.info(f"LLM决定进行追问，内容: {follow_up_content}")
                    # 使用 context.send_message 主动发送消息
                    await self.context.send_message(event.unified_msg_origin, [follow_up_content])
                else:
                    logger.info(f"追问检查：LLM返回了无效的JSON指令: {response_text}")
            except json.JSONDecodeError:
                logger.warning(f"追问检查：无法解析LLM返回的JSON: {response_text}")

        except Exception as e:
            logger.error(f"执行追问任务时发生错误: {e}", exc_info=True)
