import asyncio
import json
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# 插件元数据注册
@register(
    "ReplyDirectly",
    "YourName",
    "一个实现主动回复和追问/补充回复的插件",
    "1.0.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly"
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 用于存储被标记进行主动回复的用户
        # 键是 event.unified_msg_origin，值是 True
        self.proactive_reply_targets = set()
        logger.info("主动&补充回复插件已加载。")

    # 功能1: 通过LLM函数工具标记用户
    # 这个函数工具会让LLM在认为可以“聊上天”时调用
    @filter.llm_tool(name="mark_user_for_proactive_reply")
    async def mark_user_for_proactive_reply(self, event: AstrMessageEvent) -> MessageEventResult:
        """当LLM认为和用户聊得火热，希望在用户下次发言时主动回应时，调用此工具。这会标记用户，使得机器人下次能主动回复一次。

        Args:
            reason (string): 简要说明为什么要标记用户，例如“用户情绪高涨，适合继续话题”。
        """
        # 检查功能开关
        if not self.config.get("proactive_reply", {}).get("enable", False):
            # 虽然函数工具会被注册，但我们可以在执行时告知LLM功能未开启
            return event.plain_result("The proactive reply feature is currently disabled.")

        umo = event.unified_msg_origin
        self.proactive_reply_targets.add(umo)
        logger.info(f"[主动回复] 已标记用户 {umo} 进行下一次主动回复。")
        
        # 返回给LLM的信息，表示操作成功。这个消息不会发给用户。
        return event.plain_result("OK, I have marked the user for a proactive reply on their next message.")

    # 功能1: 监听所有消息，实现主动回复
    # 使用高优先级(priority>0)确保它在默认LLM请求前执行
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def proactive_reply_handler(self, event: AstrMessageEvent):
        # 检查功能开关
        if not self.config.get("proactive_reply", {}).get("enable", False):
            return

        umo = event.unified_msg_origin
        
        # 核心逻辑：如果用户在我们的目标集合中，并且没有@机器人或使用唤醒词
        if umo in self.proactive_reply_targets and not event.is_at_or_wake_command:
            logger.info(f"[主动回复] 检测到被标记用户 {umo} 发言，将主动回复。")
            
            # 移除标记，确保只主动回复一次
            self.proactive_reply_targets.remove(umo)
            
            # 停止事件继续传播，防止触发其他插件或默认的LLM回复
            event.stop_event()
            
            # 直接请求LLM对用户消息进行响应，并把结果发送出去
            yield event.request_llm(prompt=event.message_str)

    # 功能2: 监听机器人发送消息后的事件
    # 这是实现“追问/补充”功能的入口点
    @filter.after_message_sent()
    async def after_message_sent_handler(self, event: AstrMessageEvent):
        # 检查功能开关
        if not self.config.get("follow_up_reply", {}).get("enable", False):
            return

        # 确保我们只处理由机器人发送的、有实际内容的回复
        if event.get_result() and event.get_result().is_send:
            # 使用 asyncio.create_task 在后台执行，避免阻塞主流程
            asyncio.create_task(self._handle_follow_up(event))

    async def _handle_follow_up(self, event: AstrMessageEvent):
        """
        处理追问/补充回复的后台任务
        """
        try:
            conf = self.config.get("follow_up_reply", {})
            delay = conf.get("delay_seconds", 5)
            
            # 等待配置的秒数
            await asyncio.sleep(delay)

            # 获取刚刚发送的机器人消息
            bot_message_result = event.get_result()
            bot_message_str = "".join([c.text for c in bot_message_result.chain if isinstance(c, Comp.Plain)])
            if not bot_message_str:
                logger.debug("[补充回复] Bot发送的消息不含文本，跳过。")
                return # 如果机器人发的是图片等非文本消息，则不处理

            # 获取用户的原始消息
            user_message_str = event.message_str

            # 获取对话历史上下文
            history = []
            try:
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
                if curr_cid:
                    conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
                    if conversation and conversation.history:
                        history = json.loads(conversation.history)
            except Exception as e:
                logger.warning(f"[补充回复] 获取对话历史失败: {e}")

            # 构造发送给LLM的Prompt
            prompt_template = conf.get("prompt", "")
            final_prompt = prompt_template.format(
                user_message=user_message_str,
                bot_message=bot_message_str,
                history=json.dumps(history, ensure_ascii=False, indent=2)
            )

            # 直接调用LLM Provider，不通过事件请求，因为我们需要原始的、格式化的回复
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[补充回复] 未找到正在使用的LLM Provider。")
                return

            llm_response = await provider.text_chat(prompt=final_prompt)
            response_text = llm_response.completion_text

            # 解析LLM返回的JSON
            # LLM有时会用 "```json\n...\n```" 包裹代码，需要提取出来
            if "```json" in response_text:
                try:
                    response_text = response_text.split("```json")[1].split("```")[0].strip()
                except IndexError:
                    pass # 如果分割失败，继续尝试直接解析
            
            try:
                data = json.loads(response_text)
                if data.get("should_reply") and data.get("content"):
                    logger.info(f"[补充回复] LLM决定进行追问/补充: {data['content']}")
                    # 使用 self.context.send_message 主动发送消息
                    # 您提供的文档中有此API: self.context.send_message(unified_msg_origin, chains)
                    message_chain = [Comp.Plain(text=data['content'])]
                    await self.context.send_message(event.unified_msg_origin, message_chain)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"[补充回复] 解析LLM的JSON响应失败: {e}。响应原文: '{response_text}'")

        except Exception as e:
            logger.error(f"[补充回复] 处理追问/补充时发生未知错误: {e}", exc_info=True)
            
    async def terminate(self):
        """插件卸载时调用，用于清理资源"""
        self.proactive_reply_targets.clear()
        logger.info("主动&补充回复插件已卸载，清理完毕。")
