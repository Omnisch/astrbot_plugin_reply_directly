import asyncio
import json
from typing import Set

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# 插件元数据注册
@register(
    "ReplyDirectly",  # 插件名
    "YourName",      # 作者名
    "一个实现主动回复和响应后检查的插件",  # 描述
    "1.0.0",          # 版本
    "https://github.com/qa296/astrbot_plugin_reply_directly"  # 仓库地址
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 用于存储哪些用户进入了“主动聊天”模式
        self.proactive_chat_users: Set[str] = set()
        logger.info("主动回复插件已加载。")
        logger.info(f"主动聊天模式: {'启用' if self.config.get('enable_proactive_chat') else '禁用'}")
        logger.info(f"响应后检查模式: {'启用' if self.config.get('enable_post_response_check') else '禁用'}")

    # --- 功能1: 主动聊天模式 ---

    @filter.llm_tool(name="start_proactive_chat")
    async def start_proactive_chat(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        当与用户聊天聊得非常投机时，调用此工具。调用后，机器人将在下一次无需@或唤醒词的情况下主动回复该用户一次。
        Args:
            # 此函数无需参数，它会从事件上下文中自动获取用户信息。
        """
        if not self.config.get("enable_proactive_chat", False):
            # 如果功能关闭，静默失败
            return

        user_id = event.unified_msg_origin
        self.proactive_chat_users.add(user_id)
        logger.info(f"用户 [{user_id}] 已被标记为主动聊天对象。")
        
        # 向用户发送一个确认消息
        yield event.plain_result("好呀，感觉我们聊得很开心！你接下来可以直接跟我说话，我会回你哦。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def handle_proactive_chat(self, event: AstrMessageEvent):
        """
        监听所有消息，处理被标记为主动聊天的用户。
        高优先级(priority=10)确保它在默认的LLM处理之前运行。
        """
        if not self.config.get("enable_proactive_chat", False):
            return

        # 如果事件已经被唤醒（例如通过@或唤醒词），则不处理，避免重复响应
        if event.is_wake_up():
            return

        user_id = event.unified_msg_origin
        if user_id in self.proactive_chat_users:
            logger.info(f"检测到主动聊天用户 [{user_id}] 的消息，准备请求LLM。")
            
            # 标记后只使用一次，立即移除
            self.proactive_chat_users.remove(user_id)
            
            # 停止事件继续传播，防止其他插件或默认逻辑处理此消息
            event.stop_event()

            # 直接将用户的消息作为prompt请求LLM
            yield event.request_llm(
                prompt=event.get_message_str(),
                image_urls=[img.url for img in event.get_messages() if isinstance(img, Comp.Image)]
            )

    # --- 功能2: 响应后检查 ---

    @filter.after_message_sent()
    async def handle_post_response_check(self, event: AstrMessageEvent):
        """
        在机器人发送消息后触发的钩子。
        """
        if not self.config.get("enable_post_response_check", False):
            return

        # 确保是LLM的有效回复，而不是插件发送的指令性消息或空消息
        result = event.get_result()
        if not result or not result.get_plain_text():
            return
        
        # 避免对自己生成的追问消息再进行检查，防止无限循环
        if event.get_extra("is_follow_up"):
            return

        # 使用 create_task 在后台执行，避免阻塞事件流
        asyncio.create_task(self._check_follow_up(event))

    async def _check_follow_up(self, event: AstrMessageEvent):
        """
        实际执行检查和追问的异步函数。
        """
        try:
            delay = float(self.config.get("post_response_delay_seconds", 5.0))
            await asyncio.sleep(delay)

            # 获取刚才的对话上下文
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            if not curr_cid:
                return
            
            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
            history = json.loads(conversation.history)
            
            # 机器人刚刚的回复
            bot_response_text = event.get_result().get_plain_text()
            history.append({"role": "assistant", "content": bot_response_text})
            
            # 构建询问LLM的prompt
            prompt_for_check = f"""
请回顾以下对话历史:
{json.dumps(history, ensure_ascii=False, indent=2)}

对话刚刚结束，机器人的最后一句回复是：“{bot_response_text}”。
请你判断，机器人是否需要立即进行一次补充说明或追问，以使对话更连贯或更有帮助？
请严格按照以下JSON格式回答，不要添加任何其他解释：
{{
  "answer": boolean,  // true表示需要补充回答，false表示不需要
  "content": "string" // 如果需要回答，这里是补充回答的内容；如果不需要，则为空字符串
}}
"""
            logger.info(f"为用户 [{event.unified_msg_origin}] 执行响应后检查...")
            
            # 使用底层API调用LLM，避免触发其他钩子
            llm_response: LLMResponse = await self.context.get_using_provider().text_chat(
                prompt=prompt_for_check,
                system_prompt="你是一个对话分析助手，你的任务是判断是否需要补充回答，并以JSON格式输出结果。"
            )

            # 解析LLM的JSON回答
            response_text = llm_response.completion_text
            json_part = response_text[response_text.find('{'):response_text.rfind('}')+1]
            
            decision = json.loads(json_part)
            
            if decision.get("answer") and decision.get("content"):
                follow_up_content = decision["content"]
                logger.info(f"LLM决定进行补充回答，内容: {follow_up_content}")
                
                # 使用context.send_message发送主动消息
                # 这种方式不会再次触发after_message_sent钩子，避免了循环
                message_chain = [Comp.Plain(text=follow_up_content)]
                await self.context.send_message(event.unified_msg_origin, message_chain)

        except json.JSONDecodeError:
            logger.warning("响应后检查：LLM返回的不是有效的JSON，已跳过。")
        except Exception as e:
            logger.error(f"响应后检查时发生错误: {e}", exc_info=True)

    async def terminate(self):
        """插件卸载时清理资源"""
        self.proactive_chat_users.clear()
        logger.info("主动回复插件已卸载，清理用户状态。")
