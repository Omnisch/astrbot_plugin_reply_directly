import asyncio
import time
import json
from typing import Set, Dict, List

# AstrBot 核心 API
from astrbot.api import logger, register, Star, Context, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.provider import LLMResponse
import astrbot.api.message_components as Comp

# 注册插件
@register(
    "ReplyDirectly",  # 插件名
    "qa296",  # 作者
    "一个智能回复插件，可以实现免@直接回复和对话后反思追问功能。",  # 描述
    "1.0.0",  # 版本
    "https://github.com/qa296/astrbot_plugin_reply_directly"  # 仓库地址
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 用于存储哪些用户（在哪个群）被授权了直接回复
        # 格式: {"{group_id}_{user_id}"}
        self.direct_reply_users: Set[str] = set()

        # 用于记录最近的消息，为“反思追问”功能提供上下文
        # 格式: [{"ts": timestamp, "gid": group_id, "uid": user_id, "name": user_name, "msg": content}]
        self.message_history: List[Dict] = []
        
        logger.info("智能直接回复插件已加载。")
        logger.info(f"直接回复模式: {'启用' if self.config.get('enable_direct_reply_mode', True) else '禁用'}")
        logger.info(f"反思追问模式: {'启用' if self.config.get('enable_follow_up_mode', True) else '禁用'}")


    # --- 功能1: 直接回复模式 ---

    # 1.1 LLM 函数工具：授权直接回复
    @filter.llm_tool(name="enable_direct_reply")
    async def enable_direct_reply(self, event: AstrMessageEvent, reason: str) -> MessageEventResult:
        """
        当你认为和一个用户的对话已经非常自然流畅（聊上天了），可以调用此工具来直接回复该用户而无需等待@。
        
        Args:
            reason(string): 你为什么要调用这个工具的理由，例如“与用户xxx的对话非常投机”。
        """
        if not self.config.get('enable_direct_reply_mode', True):
            yield event.plain_result("（管理员禁用了直接回复功能，无法开启。）")
            return
            
        group_id = event.get_group_id() or "private"
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        
        key = f"{group_id}_{user_id}"
        self.direct_reply_users.add(key)
        
        logger.info(f"已为群'{group_id}'中的用户'{user_name}({user_id})'开启直接回复。原因: {reason}")
        yield event.plain_result(f"（已开启与 {user_name} 的直接畅聊模式！）")

    # 1.2 LLM 函数工具：取消直接回复
    @filter.llm_tool(name="disable_direct_reply")
    async def disable_direct_reply(self, event: AstrMessageEvent, reason: str) -> MessageEventResult:
        """
        当你认为与用户的直接对话应该结束时，调用此工具来取消直接回复模式。

        Args:
            reason(string): 你为什么要结束直接对话的理由，例如“当前话题已结束”。
        """
        group_id = event.get_group_id() or "private"
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        key = f"{group_id}_{user_id}"
        if key in self.direct_reply_users:
            self.direct_reply_users.discard(key)
            logger.info(f"已为群'{group_id}'中的用户'{user_name}({user_id})'关闭直接回复。原因: {reason}")
            yield event.plain_result(f"（已关闭与 {user_name} 的直接畅聊模式。）")
        else:
            yield event.plain_result("（当前未处于直接畅聊模式中。）")

    # 1.3 核心逻辑：拦截消息并判断是否需要直接回复
    # 使用高优先级，确保在其他处理器（尤其是默认的LLM处理器）之前执行
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def direct_reply_handler(self, event: AstrMessageEvent):
        if not self.config.get('enable_direct_reply_mode', True):
            return
            
        # 如果消息已经@了bot，则无需处理，交由默认流程
        if event.is_at_me():
            return
            
        group_id = event.get_group_id() or "private"
        user_id = event.get_sender_id()
        key = f"{group_id}_{user_id}"
        
        if key in self.direct_reply_users:
            logger.info(f"检测到直接回复授权用户 '{event.get_sender_name()}' 的消息，直接交由 LLM 处理。")
            # 将消息请求发送给LLM
            yield event.request_llm(prompt=event.message_str)
            # 停止事件传播，防止消息被其他插件或默认处理器再次处理
            event.stop_event()


    # --- 功能2: 反思追问模式 ---

    # 2.1 记录所有消息，用于后续分析
    # 使用低优先级，确保在所有功能性处理器之后执行
    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10)
    async def message_logger(self, event: AstrMessageEvent):
        # 清理旧消息（例如，只保留最近5分钟的）
        current_time = time.time()
        self.message_history = [
            msg for msg in self.message_history if current_time - msg["ts"] < 300
        ]
        
        self.message_history.append({
            "ts": current_time,
            "gid": event.get_group_id() or "private",
            "uid": event.get_sender_id(),
            "name": event.get_sender_name(),
            "msg": event.message_str,
        })

    # 2.2 监听LLM的回复，触发追问任务
    @filter.on_llm_response()
    async def on_bot_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self.config.get('enable_follow_up_mode', True):
            return

        # 只处理助手的有效回复
        if resp.role == "assistant" and resp.completion_text:
            bot_message = resp.completion_text
            # 创建一个后台任务来执行追问逻辑，避免阻塞当前流程
            asyncio.create_task(self._follow_up_task(event, bot_message))

    # 2.3 追问任务的具体实现
    async def _follow_up_task(self, original_event: AstrMessageEvent, bot_message: str):
        try:
            delay = self.config.get('follow_up_delay', 5)
            await asyncio.sleep(delay)

            # 获取Bot说话之后、现在之前的新消息
            start_time = time.time() - delay
            group_id = original_event.get_group_id() or "private"
            
            recent_chats = [
                f'{msg["name"]}: {msg["msg"]}'
                for msg in self.message_history
                if msg["ts"] > start_time and msg["gid"] == group_id
            ]

            # 如果没有新消息，就不需要反思
            if not recent_chats:
                return

            recent_chats_str = "\n".join(recent_chats)
            
            # 构建发送给LLM的Prompt
            prompt = f"""
            背景：你是一个聊天机器人。
            你刚才说了："{bot_message}"
            在你说话后的这 {delay} 秒内，群里有如下新消息：
            ---
            {recent_chats_str}
            ---
            任务：请你判断，基于你刚才的回复和这些新消息，你是否需要主动进行追问、澄清或补充回答？
            请严格按照以下JSON格式回答，不要添加任何额外解释：
            {{
                "should_reply": boolean,
                "content": "如果你认为需要回复，这里是你的回复内容。如果不需要，则留空。"
            }}
            """

            logger.info(f"为群'{group_id}'触发反思追问任务。")
            
            # 调用LLM进行判断
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("反思追问失败：未找到可用的大语言模型提供商。")
                return

            response = await provider.text_chat(prompt=prompt)
            
            # 解析LLM的JSON响应
            try:
                # 尝试修复不规范的JSON
                content_str = response.completion_text.strip()
                if content_str.startswith("```json"):
                    content_str = content_str[7:]
                if content_str.endswith("```"):
                    content_str = content_str[:-3]
                
                decision = json.loads(content_str)
                should_reply = decision.get("should_reply", False)
                content_to_send = decision.get("content", "").strip()

                if should_reply and content_to_send:
                    logger.info(f"反思追问结果：需要回复。内容：{content_to_send}")
                    # 发送追问消息
                    message_chain = [Comp.Plain(content_to_send)]
                    await self.context.send_message(original_event.unified_msg_origin, message_chain)

            except (json.JSONDecodeError, AttributeError, KeyError) as e:
                logger.error(f"解析反思追问LLM的响应失败: {e}\n原始响应: {response.completion_text}")

        except Exception as e:
            logger.error(f"执行反思追问任务时发生未知错误: {e}")
